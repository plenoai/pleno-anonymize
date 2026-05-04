"""JiraConnector — Cloud + Data Center `SourceConnector`.

Single connector kind (`jira`) backed by two REST flavors selected at
construction time. The wire-level differences (URL prefix, body format
for issue descriptions/comments, throttle status code) live inside
`api.py` and the body converters; the `SourceConnector` contract the
scheduler sees is identical to every other ADR-0007 §13 connector.

Pipeline:

    1. /project/search  -> enumerate every project the principal can read
    2. JQL `project = X AND updated >= cursor`  -> issues touched since
       the last incremental cursor (full walk on first run)
    3. /issue/{key}/comment  -> all comments per issue (paginated)
    4. ADF (Cloud) or storage XHTML (DC) -> plain text
    5. attachments rendered as `attachment={name}, url={url}` lines —
       we never download attachment bodies (operator-controlled cost)

Each issue becomes one Document carrying:

    key=PROJ-123
    summary=...
    status=...
    assignee=...
    reporter=...
    description=...
    comment[<id>]=...
    attachment=name, url=...

The `discover()` cursor is a JSON-encoded `{ "highest_updated":
"<iso8601>" }`. On each scan the JQL `updated >= cursor` resumes
incrementally; malformed cursor values are silently ignored so a
forward-incompatible cursor never crashes a scan.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from pleno_pii_scanner.sources.base import (
    Capabilities,
    Cursor,
    Document,
    DocumentChunk,  # noqa: F401 — referenced in fetch return-type annotation
    DocumentRef,
    SourceConnector,
    SourceFilter,
)
from pleno_pii_scanner.sources.registry import ConnectorSpec

from .adf import adf_to_text
from .api import (
    DEFAULT_TIMEOUT,
    AuthMode,
    BasicAuth,
    BearerAuth,
    Flavor,
    JiraApi,
)
from .storage import storage_to_text


# Connector kind exported via the `pleno_pii_scanner.connectors` entry
# point group (see pyproject.toml). One kind covers both flavors; the
# wire flavor is selected by config.
KIND = "jira"


# Page size for /search and /project/search. Jira Cloud caps `/search`
# at 100; DC defaults to 50 with a configurable max. 100 stays under
# both — going higher trades latency for marginal request-count savings
# and risks tripping admin-configured query timeouts.
_PAGE_SIZE = 100


# Comment endpoint page size. /issue/{key}/comment caps at 100 on Cloud
# and DC; we use the max because comment bodies are typically small and
# we want one paginated round-trip per typical issue.
_COMMENT_PAGE_SIZE = 100


# Maximum total pages we will walk per paginated endpoint without the
# server signalling completion. Defends against a buggy upstream that
# always claims more data exists. 10_000 × 100 = a million records,
# well above any realistic single-project size.
_MAX_PAGINATION_DEPTH = 10_000


@dataclass(frozen=True, slots=True)
class JiraConfig:
    """Construction config for `JiraConnector`.

    `flavor` selects the wire protocol. `base_url` is the site root
    (`https://acme.atlassian.net` for Cloud, `https://jira.acme.internal`
    for DC) — the connector appends `/rest/api/3` or `/rest/api/2`.

    Auth: pick exactly one of:
      - `email` + `api_token` (Cloud Basic)
      - `access_token` (Cloud OAuth 2.0 OR DC PAT — both are Bearer)
      - `username` + `password` (DC HTTP Basic)

    `projects` allow-lists projects by key (`("ENG", "OPS")`); empty
    means every readable project. `include_comments` toggles the comment
    endpoint walk; `include_attachments` toggles the `attachment=` line
    serialisation (we never download attachment bodies regardless).
    """

    flavor: Flavor
    base_url: str
    email: str | None = None
    api_token: str | None = None
    access_token: str | None = None
    username: str | None = None
    password: str | None = None
    projects: tuple[str, ...] = ()
    include_comments: bool = True
    include_attachments: bool = True
    request_timeout: float = DEFAULT_TIMEOUT
    id: str | None = None

    def __post_init__(self) -> None:
        if self.flavor not in ("cloud", "datacenter"):
            raise ValueError(
                f"JiraConfig.flavor must be 'cloud' or 'datacenter'; "
                f"got {self.flavor!r}"
            )
        if not self.base_url:
            raise ValueError("JiraConfig.base_url must be a non-empty URL")
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError(
                f"JiraConfig.base_url must start with http:// or https://; "
                f"got {self.base_url!r}"
            )
        # Validate auth shape upfront so a misconfigured profile fails
        # at construction rather than mid-discover. We accept exactly
        # one of the four supported modes.
        modes = self._present_auth_modes()
        if not modes:
            raise ValueError(
                "JiraConfig requires one of: (email + api_token) [cloud basic], "
                "access_token [cloud OAuth or DC PAT], "
                "or (username + password) [DC basic]"
            )
        if len(modes) > 1:
            raise ValueError(
                f"JiraConfig accepts exactly one auth mode; "
                f"received: {sorted(modes)}"
            )

    def _present_auth_modes(self) -> set[str]:
        modes: set[str] = set()
        if self.email and self.api_token:
            modes.add("email+api_token")
        if self.access_token:
            modes.add("access_token")
        if self.username and self.password:
            modes.add("username+password")
        return modes

    def resolved_id(self) -> str:
        """Stable identifier safe to surface in logs / findings.

        Critically: must not embed `api_token` / `access_token` /
        `password`. We derive the id from the host portion of the
        `base_url` plus the flavor — both are non-secret.
        """
        if self.id is not None:
            return self.id
        host = _host_only(self.base_url)
        return f"jira-{self.flavor}:{host}"

    def build_auth(self) -> AuthMode:
        """Construct the AuthMode for the configured credential.

        Order matches `__post_init__`'s validation. The check for
        exactly-one-mode happens there, so by the time we reach here
        we have one and only one set of fields populated.
        """
        if self.access_token:
            return BearerAuth(token=self.access_token)
        if self.email and self.api_token:
            # Cloud Basic uses the user's email as the username and the
            # API token as the password (Atlassian's documented form).
            return BasicAuth(username=self.email, password=self.api_token)
        # The remaining branch is `username + password` (DC Basic);
        # __post_init__ guarantees these are both set.
        assert self.username is not None and self.password is not None
        return BasicAuth(username=self.username, password=self.password)


class JiraConnector:
    """`SourceConnector` for Jira Cloud + Jira Data Center.

    Owns one `JiraApi` (HTTP session) for the connector's lifetime.
    `discover()` enumerates issues; `fetch()` materialises an issue
    into one Document. Issue payloads observed during discover are
    cached in-memory so fetch() avoids re-issuing `/issue/{key}`.
    """

    kind = KIND

    def __init__(
        self,
        config: JiraConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Any | None = None,
    ) -> None:
        self._config = config
        self.id = config.resolved_id()
        self._api = JiraApi(
            flavor=config.flavor,
            base_url=config.base_url,
            auth=config.build_auth(),
            transport=transport,
            timeout=config.request_timeout,
            sleep=sleep,
        )
        # Map of issue key -> {"issue": dict, "comments": list[dict]}
        # populated during discover() so fetch() does not re-issue
        # /issue/{key}; cleared on close().
        self._issue_cache: dict[str, dict[str, Any]] = {}
        # Highest `updated` timestamp seen across the run, in raw Jira
        # ISO-8601 form. Surfaced via cursor_after_run() for the next
        # incremental scan.
        self._high_water: str | None = None
        # Lock guarding _issue_cache + _high_water mutation so concurrent
        # fetch() calls (max_concurrent_fetches > 1) cannot race.
        self._lock = asyncio.Lock()

    @property
    def api(self) -> JiraApi:
        # Exposed for tests + advanced operators that want to issue raw
        # endpoints (e.g. /myself for credential probing).
        return self._api

    @property
    def config(self) -> JiraConfig:
        return self._config

    def capabilities(self) -> Capabilities:
        # `incremental=True` because we round-trip the highest-updated
        # cursor between runs. `binary=False` — we never download
        # attachment bodies (operators wire the `attachment=` URL into
        # a separate http connector if they want body scans).
        return Capabilities(
            incremental=True,
            binary=False,
            content_hash_delta=False,
            max_concurrent_fetches=4,
            streaming=False,
        )

    # ------------------------------------------------------------------
    # discover
    # ------------------------------------------------------------------

    async def discover(
        self,
        filter: SourceFilter,
        cursor: Cursor | None,
    ) -> AsyncIterator[DocumentRef]:
        """Yield one DocumentRef per issue updated since `cursor`.

        The cursor is a JSON object carrying `highest_updated`. JQL is
        built as `project = X AND updated >= "<ts>" ORDER BY updated
        ASC`; ASC ordering means the last issue we see has the highest
        `updated`, which we persist as the next cursor.
        """
        prior_high_water = _decode_cursor(cursor)
        # `filter.since` overrides the cursor when both are present;
        # operator-supplied `--since` is the authoritative knob (tests
        # also rely on this for deterministic JQL assertions).
        since = (
            filter.since.isoformat()
            if filter.since is not None
            else prior_high_water
        )
        projects = await self._enumerate_projects(filter)
        for project_key in projects:
            async for issue in self._iter_issues(project_key, since=since):
                key = issue.get("key")
                if not isinstance(key, str) or not key:
                    # Defensive: Jira's response schema is stable but
                    # third-party Jira-compatible servers occasionally
                    # emit issues without a key. Skip rather than crash.
                    continue
                comments: list[Mapping[str, Any]] = []
                if self._config.include_comments:
                    comments = await self._fetch_comments(key)
                async with self._lock:
                    self._issue_cache[key] = {
                        "issue": issue,
                        "comments": comments,
                    }
                    updated = _issue_updated(issue)
                    if updated and (
                        self._high_water is None or updated > self._high_water
                    ):
                        self._high_water = updated
                yield self._issue_to_ref(project_key, issue, comments)

    async def fetch(
        self,
        ref: DocumentRef,
    ) -> AsyncIterator[Document | DocumentChunk]:
        """Materialise an issue into one Document."""
        key = ref.metadata.get("key")
        if not key:
            return
        async with self._lock:
            cached = self._issue_cache.get(key)
        if cached is None:
            # The ref was emitted by a different connector instance, or
            # the cache was already drained. Fall back to a live fetch
            # so the operator can hand-craft a DocumentRef and still
            # pull the body.
            issue = await self._api.get(f"/issue/{key}")
            comments: list[Mapping[str, Any]] = []
            if self._config.include_comments and issue:
                comments = await self._fetch_comments(key)
        else:
            issue = cached["issue"]
            comments = cached["comments"]
        if not issue:
            return
        text = self._serialise_issue(issue, comments)
        if not text:
            return
        yield Document(
            ref=ref,
            text=text,
            fetched_at=datetime.now(UTC),
            content_hash=str(key),
        )

    def cursor_after_run(self) -> Cursor | None:
        """Return the JSON-encoded cursor for the next incremental run.

        Returns `None` (not an empty JSON object) when nothing was
        observed; callers persist `None` as "no resume token" rather
        than emitting a placeholder cursor that would later need to be
        special-cased.
        """
        if self._high_water is None:
            return None
        return json.dumps({"highest_updated": self._high_water}, sort_keys=True)

    async def close(self) -> None:
        """Release the HTTP client + drop in-memory caches.

        `close()` must be idempotent — the scheduler invokes it from a
        finally clause and may also call it explicitly on shutdown.
        Clearing the caches first means a double-call cannot leak
        references after the second invocation.
        """
        async with self._lock:
            self._issue_cache.clear()
            self._high_water = None
        await self._api.aclose()

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    async def _enumerate_projects(
        self, filter: SourceFilter
    ) -> list[str]:
        """Return project keys to scan, applying allow-list + filter."""
        if self._config.projects:
            # Operator-supplied allow-list short-circuits enumeration —
            # fewer API calls + the operator's intent is authoritative.
            allow = list(self._config.projects)
        else:
            allow = await self._list_all_projects()
        out: list[str] = []
        for project_key in allow:
            if filter.include and not _matches_any(project_key, filter.include):
                continue
            if filter.exclude and _matches_any(project_key, filter.exclude):
                continue
            out.append(project_key)
        return out

    async def _list_all_projects(self) -> list[str]:
        """Page through `/project/search` and return every project key."""
        keys: list[str] = []
        start_at = 0
        depth = 0
        while True:
            depth += 1
            if depth > _MAX_PAGINATION_DEPTH:
                # See `_MAX_PAGINATION_DEPTH`: defensive against a
                # buggy upstream that never sets `isLast`. Surfacing as
                # an explicit failure beats spinning the discover loop.
                raise RuntimeError(  # pragma: no cover
                    f"jira /project/search exceeded {_MAX_PAGINATION_DEPTH} pages"
                )
            body = await self._api.get(
                "/project/search",
                params={"startAt": start_at, "maxResults": _PAGE_SIZE},
            )
            if not body:
                return keys
            values = body.get("values") or []
            for project in values:
                key = (
                    project.get("key") if isinstance(project, Mapping) else None
                )
                if isinstance(key, str) and key:
                    keys.append(key)
            if body.get("isLast", True):
                return keys
            if not values:
                # No values on this page but isLast=false — break to
                # avoid an infinite loop. Treat as end-of-list.
                return keys
            start_at += len(values)

    async def _iter_issues(
        self, project_key: str, *, since: str | None
    ) -> AsyncIterator[Mapping[str, Any]]:
        """Paginate `/search` issues for `project_key`, applying `since`."""
        jql = _build_jql(project_key, since)
        # Field selection: keep the response payload small but include
        # everything the serialiser needs. `*all` would balloon the
        # response with every custom field; the explicit list keeps the
        # surface stable for snapshot tests.
        fields = ",".join(
            (
                "summary",
                "status",
                "assignee",
                "reporter",
                "description",
                "updated",
                "attachment",
                "issuetype",
                "priority",
            )
        )
        start_at = 0
        depth = 0
        while True:
            depth += 1
            if depth > _MAX_PAGINATION_DEPTH:
                raise RuntimeError(  # pragma: no cover
                    f"jira /search exceeded {_MAX_PAGINATION_DEPTH} pages"
                )
            body = await self._api.get(
                "/search",
                params={
                    "jql": jql,
                    "startAt": start_at,
                    "maxResults": _PAGE_SIZE,
                    "fields": fields,
                },
            )
            if not body:
                return
            issues = body.get("issues") or []
            for issue in issues:
                if isinstance(issue, Mapping):
                    yield issue
            total = body.get("total")
            new_start = start_at + len(issues)
            if not issues:
                return
            # End-of-list when we've consumed every issue Jira reports.
            # `total` is best-effort (Jira documents it as approximate)
            # so we also stop when a page is shorter than maxResults.
            if isinstance(total, int) and new_start >= total:
                return
            if len(issues) < _PAGE_SIZE:
                return
            start_at = new_start

    async def _fetch_comments(
        self, issue_key: str
    ) -> list[Mapping[str, Any]]:
        """Paginate `/issue/{key}/comment` and return every comment."""
        out: list[Mapping[str, Any]] = []
        start_at = 0
        depth = 0
        while True:
            depth += 1
            if depth > _MAX_PAGINATION_DEPTH:
                raise RuntimeError(  # pragma: no cover
                    f"jira /issue/{issue_key}/comment exceeded "
                    f"{_MAX_PAGINATION_DEPTH} pages"
                )
            body = await self._api.get(
                f"/issue/{issue_key}/comment",
                params={
                    "startAt": start_at,
                    "maxResults": _COMMENT_PAGE_SIZE,
                },
            )
            if not body:
                return out
            comments = body.get("comments") or []
            for comment in comments:
                if isinstance(comment, Mapping):
                    out.append(comment)
            total = body.get("total")
            new_start = start_at + len(comments)
            if not comments:
                return out
            if isinstance(total, int) and new_start >= total:
                return out
            if len(comments) < _COMMENT_PAGE_SIZE:
                return out
            start_at = new_start

    # --- ref + serialisation -----------------------------------------

    def _issue_to_ref(
        self,
        project_key: str,
        issue: Mapping[str, Any],
        comments: Sequence[Mapping[str, Any]],
    ) -> DocumentRef:
        key = str(issue.get("key", ""))
        fields = issue.get("fields") or {}
        summary = fields.get("summary") if isinstance(fields, Mapping) else None
        last_modified = _parse_iso(_issue_updated(issue))
        # `etag` keys cache short-circuit: the issue's own `updated`
        # changes monotonically, so the scheduler can use it to skip
        # unchanged refs even when the cursor pulls them in.
        etag = _issue_updated(issue)
        size = len(summary) if isinstance(summary, str) else None
        host = _host_only(self._config.base_url)
        native_url = f"https://{host}/browse/{key}" if key else None
        return DocumentRef(
            source_id=self.id,
            source_kind=self.kind,
            path=f"jira://{project_key}/{key}",
            native_url=native_url,
            parent_chain=(f"jira://{project_key}",),
            content_type="text/plain",
            size=size,
            etag=etag,
            last_modified=last_modified,
            metadata={
                "key": key,
                "project": project_key,
                "flavor": self._config.flavor,
                "comment_count": str(len(comments)),
            },
        )

    def _serialise_issue(
        self,
        issue: Mapping[str, Any],
        comments: Sequence[Mapping[str, Any]],
    ) -> str:
        """Render an issue + its comments + attachments as one text block."""
        fields = issue.get("fields")
        if not isinstance(fields, Mapping):
            fields = {}
        parts: list[str] = []
        key = issue.get("key")
        if isinstance(key, str) and key:
            parts.append(f"key={key}")
        summary = fields.get("summary")
        if isinstance(summary, str) and summary:
            parts.append(f"summary={summary}")
        status = _named(fields.get("status"))
        if status:
            parts.append(f"status={status}")
        assignee = _display_name(fields.get("assignee"))
        if assignee:
            parts.append(f"assignee={assignee}")
        reporter = _display_name(fields.get("reporter"))
        if reporter:
            parts.append(f"reporter={reporter}")
        # Body conversion differs between flavors: Cloud is ADF JSON,
        # DC is storage-XHTML string. The dispatcher picks the right
        # converter based on the connector's flavor.
        description_text = self._convert_body(fields.get("description"))
        if description_text:
            parts.append(f"description={description_text}")
        for comment in comments:
            comment_id = comment.get("id")
            author = _display_name(
                comment.get("author") or comment.get("updateAuthor")
            )
            body_text = self._convert_body(comment.get("body"))
            if not body_text:
                continue
            label = f"comment[{comment_id}]" if comment_id else "comment"
            if author:
                parts.append(f"{label}={author}: {body_text}")
            else:
                parts.append(f"{label}={body_text}")
        if self._config.include_attachments:
            for attachment in fields.get("attachment") or []:
                if not isinstance(attachment, Mapping):
                    continue
                name = (
                    attachment.get("filename") or attachment.get("name") or ""
                )
                content_url = (
                    attachment.get("content")
                    or attachment.get("contentUrl")
                    or ""
                )
                if name or content_url:
                    parts.append(f"attachment={name}, url={content_url}")
        return "\n".join(parts)

    def _convert_body(self, body: Any) -> str:
        """Dispatch body conversion based on the configured flavor.

        Cloud bodies are ADF JSON (`{"type": "doc", "content": [...]}`);
        DC bodies are storage-XHTML strings (or, for some custom-field
        shapes, a `{"value": "...", "representation": "..."}` wrapper).
        """
        if body is None or body == "":
            return ""
        if self._config.flavor == "cloud":
            # Cloud sometimes returns a raw string for legacy custom
            # fields configured to use "text" representation; fall back
            # to the storage stripper which handles plain strings too.
            if isinstance(body, str):
                return storage_to_text(body)
            return adf_to_text(body)
        return storage_to_text(body)


# ---------------------------------------------------------------------
# JQL + parsing helpers
# ---------------------------------------------------------------------


def _build_jql(project_key: str, since: str | None) -> str:
    """Render the JQL string for incremental issue enumeration.

    `project = "X"` is quoted so a project key containing reserved
    characters (legal in DC for legacy projects) does not produce a
    syntax error. `updated >= "..."` uses Jira's documented timestamp
    form. ASC ordering is required so the last issue we see is the
    highest-updated, which feeds the next cursor.
    """
    clauses = [f'project = "{project_key}"']
    if since:
        clauses.append(f'updated >= "{since}"')
    return " AND ".join(clauses) + " ORDER BY updated ASC"


def _decode_cursor(cursor: Cursor | None) -> str | None:
    """Parse a JSON cursor and return `highest_updated` if present.

    Cursor is `str` in the core API. We persist a JSON object and
    silently fall back to None on any decode failure — the caller
    sees a fresh full scan, never a crash on a stale cursor format.
    Empty string also falls back to None (rather than treating "" as
    a literal JQL value, which Jira would reject).
    """
    if not cursor:
        return None
    try:
        decoded = json.loads(cursor)
    except (ValueError, TypeError):
        return None
    if not isinstance(decoded, Mapping):
        return None
    value = decoded.get("highest_updated")
    if isinstance(value, str) and value:
        return value
    return None


def _parse_iso(value: Any) -> datetime | None:
    """Best-effort ISO-8601 parse; None if value is missing or malformed.

    Jira emits `2026-05-04T12:34:56.789+0000` — Python's
    `datetime.fromisoformat` accepts the colon-bearing offset form
    starting in 3.11; we normalise the offset to be safe across runtimes.
    """
    if not isinstance(value, str) or not value:
        return None
    normalised = value
    if "+" in normalised[19:] or "-" in normalised[19:]:
        # Insert a colon into the offset if missing (`+0000` -> `+00:00`).
        head, sep, tail = (
            normalised.rpartition("+")
            if "+" in normalised[19:]
            else normalised.rpartition("-")
        )
        if sep and len(tail) == 4 and tail.isdigit():
            normalised = f"{head}{sep}{tail[:2]}:{tail[2:]}"
    try:
        return datetime.fromisoformat(normalised.replace("Z", "+00:00"))
    except ValueError:
        return None


def _issue_updated(issue: Mapping[str, Any]) -> str | None:
    """Return the issue's `fields.updated` ISO timestamp, if present."""
    fields = issue.get("fields")
    if not isinstance(fields, Mapping):
        return None
    updated = fields.get("updated")
    if isinstance(updated, str) and updated:
        return updated
    return None


def _named(field: Any) -> str:
    """Pull `name` out of a `{name: ...}` Jira sub-object."""
    if isinstance(field, Mapping):
        name = field.get("name")
        if isinstance(name, str):
            return name
    return ""


def _display_name(field: Any) -> str:
    """Pull `displayName` (Cloud + DC) out of a user sub-object.

    Falls back to `name` (DC-only username) when displayName is
    unavailable; Cloud removed `name` in 2019 for GDPR but DC still
    exposes it.
    """
    if isinstance(field, Mapping):
        for key in ("displayName", "name", "emailAddress", "accountId"):
            v = field.get(key)
            if isinstance(v, str) and v:
                return v
    return ""


def _host_only(base_url: str) -> str:
    """Extract the host portion of a base URL, stripping scheme + path."""
    # Walk a tiny state machine rather than pulling in urllib for one
    # field; keeps the package free of stdlib import cycles.
    s = base_url
    for prefix in ("https://", "http://"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    if "/" in s:
        s = s.split("/", 1)[0]
    return s


def _matches_any(s: str, patterns: tuple[str, ...]) -> bool:
    from fnmatch import fnmatch

    return any(fnmatch(s, p) for p in patterns)


# ---------------------------------------------------------------------
# Factory + Spec
# ---------------------------------------------------------------------


def _factory(config: Mapping[str, Any]) -> SourceConnector:
    """Build a connector from a plain config mapping.

    Required keys: `flavor`, `base_url`, plus exactly one auth mode.
    All other keys map to JiraConfig defaults.
    """
    flavor_raw = config.get("flavor")
    if flavor_raw not in ("cloud", "datacenter"):
        raise ValueError(
            f"jira connector config['flavor'] must be 'cloud' or "
            f"'datacenter'; got {flavor_raw!r}"
        )
    if "base_url" not in config:
        raise ValueError("jira connector config requires 'base_url'")
    return JiraConnector(
        JiraConfig(
            flavor=flavor_raw,
            base_url=str(config["base_url"]),
            email=_opt_str(config.get("email")),
            api_token=_opt_str(config.get("api_token")),
            access_token=_opt_str(config.get("access_token")),
            username=_opt_str(config.get("username")),
            password=_opt_str(config.get("password")),
            projects=_string_tuple(config.get("projects")),
            include_comments=bool(config.get("include_comments", True)),
            include_attachments=bool(config.get("include_attachments", True)),
            request_timeout=float(
                config.get("request_timeout", DEFAULT_TIMEOUT)
            ),
            id=_opt_str(config.get("id")),
        )
    )


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value)
    return s if s else None


def _string_tuple(value: Any) -> tuple[str, ...]:
    """Accept list/tuple of strings; reject everything else loudly."""
    if value is None:
        return ()
    if isinstance(value, str):
        # A bare string is almost certainly an operator typo (one key
        # instead of a list); reject so they catch it before scan launch.
        raise ValueError(
            "jira connector list-typed configs (projects) must be a list, "
            "not a bare string"
        )
    if not isinstance(value, Iterable):
        raise ValueError(
            "jira connector list-typed configs must be iterable"
        )
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise ValueError(
                "jira connector list-typed configs must contain non-empty strings"
            )
        out.append(item)
    return tuple(out)


SPEC = ConnectorSpec(
    kind=KIND,
    version="0.1.0",
    factory=_factory,
    capabilities=Capabilities(
        incremental=True,
        binary=False,
        content_hash_delta=False,
        max_concurrent_fetches=4,
        streaming=False,
    ),
    required_scopes=(
        # Cloud OAuth scope; DC PATs are scoped per-token in the admin
        # UI rather than via API surface so this is an aspirational
        # documentation hint for `connectors describe jira`.
        "read:jira-work",
        "read:jira-user",
    ),
    description=(
        "Atlassian Jira (Cloud + Data Center) connector. Single kind, two "
        "wire flavors selected by config (`flavor=cloud|datacenter`). "
        "Cloud: /rest/api/3 + ADF body conversion + 429 backoff. "
        "DC: /rest/api/2 + storage-XHTML body conversion + 503 backoff. "
        "Project enumeration with allow-list; JQL `updated >= cursor` "
        "for incremental scan; comments + attachment refs included by "
        "default; never downloads attachment bodies. ADR-0007 §13."
    ),
)


__all__ = [
    "KIND",
    "SPEC",
    "JiraConfig",
    "JiraConnector",
    "adf_to_text",
]
