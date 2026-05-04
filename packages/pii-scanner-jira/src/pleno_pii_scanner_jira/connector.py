"""Atlassian Jira SourceConnector — Cloud + DC, ADF→text, JQL incremental.

Pipeline:

  1. Build JQL: `(project in (P1,P2)) AND updated >= "<cursor>" ORDER BY updated ASC`
     — `project in (...)` clause omitted when no allowlist is configured;
     `updated >=` clause omitted on a fresh scan (cursor is None).
  2. Paginate `/rest/api/3/search`:
       - Cloud: `nextPageToken` (opaque server token)
       - DC:    `startAt` / `maxResults` (classic offset paging)
  3. For each issue, yield one DocumentRef (path = `<project>/<issue-key>`).
  4. If `include_comments`, fetch `/rest/api/3/issue/{key}/comment` and
     yield one DocumentRef per comment (path = `<project>/<issue-key>/comments/<id>`).
  5. fetch() yields the cached body verbatim — discover() pre-renders
     summary + ADF→text description / ADF→text comment body.

ADF (Atlassian Document Format) is a JSON tree of nodes. The walker
descends unconditionally, accumulating every `text` node and emitting
a newline at structural boundaries (`paragraph`, `heading`, `listItem`).
A purpose-built walker beats pulling a heavy `atlaskit` dependency in.

Cursor: ISO-8601 string of the latest `updated` timestamp seen in the
run. Persisted verbatim by the scheduler and round-tripped through
`discover(..., cursor=...)` — `Cursor` is a type alias for `str` in
the core API, never a class.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from fnmatch import fnmatch
from typing import Any, Literal

import httpx

from pleno_pii_scanner.sources.base import (
    Capabilities,
    Cursor,
    Document,
    DocumentChunk,
    DocumentRef,
    SourceConnector,
    SourceFilter,
)
from pleno_pii_scanner.sources.registry import ConnectorSpec


_SEARCH_PATH = "/rest/api/3/search"
_ISSUE_PATH_TPL = "/rest/api/3/issue/{key}/comment"
# Defensive cap on JQL pagination — protects against a misconfigured
# server returning the same nextPageToken indefinitely.
_MAX_PAGES = 10_000


@dataclass(frozen=True, slots=True)
class JiraConfig:
    """Construction config for `JiraConnector`."""

    base_url: str
    email: str
    api_token: str
    projects: tuple[str, ...] = ()
    include_comments: bool = True
    deployment: Literal["cloud", "dc"] = "cloud"
    id: str | None = None

    def __post_init__(self) -> None:
        if not self.base_url:
            raise ValueError("base_url must be non-empty")
        if not self.api_token:
            raise ValueError("api_token must be non-empty")
        if self.deployment not in ("cloud", "dc"):
            raise ValueError(
                f"deployment must be 'cloud' or 'dc'; got {self.deployment!r}"
            )

    def resolved_id(self) -> str:
        if self.id is not None:
            return self.id
        # Token is sensitive; identify by base_url + hashed token + project set.
        import hashlib

        h = hashlib.sha256()
        h.update(self.base_url.encode())
        h.update(b"\0")
        h.update(self.api_token.encode())
        for p in sorted(self.projects):
            h.update(b"\0")
            h.update(p.encode())
        return f"jira:{h.hexdigest()[:16]}"


class JiraConnector:
    """Read-only SourceConnector for Atlassian Jira (Cloud + DC)."""

    kind = "jira"

    def __init__(
        self,
        config: JiraConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self.id = config.resolved_id()
        if client is None:
            self._client = httpx.AsyncClient(
                base_url=config.base_url.rstrip("/"),
                timeout=30.0,
            )
            self._owns_client = True
        else:
            self._client = client
            self._owns_client = False
        self._auth_headers = _build_auth_headers(config)
        # Cache of pre-rendered bodies keyed by ref.path so fetch()
        # doesn't re-walk the JSON tree.
        self._documents: dict[str, str] = {}
        # Latest `updated` timestamp seen this run; drives cursor_after_run().
        self._high_water: str | None = None

    def capabilities(self) -> Capabilities:
        return Capabilities(
            incremental=True,
            binary=False,
            content_hash_delta=False,
            max_concurrent_fetches=4,
            streaming=False,
        )

    async def discover(
        self,
        filter: SourceFilter,
        cursor: Cursor | None,
    ) -> AsyncIterator[DocumentRef]:
        jql = _build_jql(self._config.projects, cursor)
        async for issue in self._iter_issues(jql):
            project_key = (
                issue.get("fields", {}).get("project", {}).get("key")
                or issue.get("key", "").split("-", 1)[0]
            )
            issue_key = issue.get("key", "")
            full = f"{project_key}/{issue_key}"
            if filter.include and not _matches_any(full, filter.include):
                continue
            if filter.exclude and _matches_any(full, filter.exclude):
                continue
            updated = issue.get("fields", {}).get("updated")
            if isinstance(updated, str):
                if self._high_water is None or updated > self._high_water:
                    self._high_water = updated
            text = _serialise_issue(issue)
            self._documents[full] = text
            yield DocumentRef(
                source_id=self.id,
                source_kind=self.kind,
                path=full,
                native_url=_issue_url(self._config.base_url, issue_key),
                content_type="text/plain",
                size=len(text),
                etag=issue.get("id"),
                last_modified=_parse_iso(updated),
                metadata={
                    "project_key": str(project_key or ""),
                    "issue_key": issue_key,
                    "issue_id": str(issue.get("id", "")),
                    "kind": "issue",
                    "_cursor": self._high_water or "",
                },
            )
            if self._config.include_comments:
                async for comment in self._iter_comments(issue_key):
                    cid = str(comment.get("id", ""))
                    cpath = f"{full}/comments/{cid}"
                    if filter.include and not _matches_any(cpath, filter.include):
                        continue
                    if filter.exclude and _matches_any(cpath, filter.exclude):
                        continue
                    ctext = _serialise_comment(comment)
                    self._documents[cpath] = ctext
                    cupdated = comment.get("updated") or comment.get("created")
                    yield DocumentRef(
                        source_id=self.id,
                        source_kind=self.kind,
                        path=cpath,
                        native_url=_issue_url(
                            self._config.base_url, issue_key, comment_id=cid
                        ),
                        content_type="text/plain",
                        size=len(ctext),
                        etag=cid,
                        last_modified=_parse_iso(cupdated),
                        metadata={
                            "project_key": str(project_key or ""),
                            "issue_key": issue_key,
                            "comment_id": cid,
                            "kind": "comment",
                        },
                    )

    async def fetch(
        self,
        ref: DocumentRef,
    ) -> AsyncIterator[Document | DocumentChunk]:
        text = self._documents.get(ref.path)
        if text is None:
            return
        yield Document(
            ref=ref,
            text=text,
            fetched_at=datetime.now(UTC),
            extra=dict(ref.metadata),
        )

    def cursor_after_run(self) -> Cursor | None:
        """ISO-8601 timestamp of the newest issue seen this run, or None."""
        return self._high_water

    async def close(self) -> None:
        self._documents.clear()
        if self._owns_client:
            await self._client.aclose()

    # --- internals ------------------------------------------------

    async def _iter_issues(self, jql: str) -> AsyncIterator[dict[str, Any]]:
        if self._config.deployment == "cloud":
            async for issue in self._iter_issues_cloud(jql):
                yield issue
        else:
            async for issue in self._iter_issues_dc(jql):
                yield issue

    async def _iter_issues_cloud(
        self, jql: str
    ) -> AsyncIterator[dict[str, Any]]:
        next_token: str | None = None
        pages = 0
        while pages < _MAX_PAGES:  # pragma: no branch
            pages += 1
            params: dict[str, Any] = {
                "jql": jql,
                "fields": "summary,description,updated,project",
            }
            if next_token is not None:
                params["nextPageToken"] = next_token
            body = await self._get_json(_SEARCH_PATH, params=params)
            issues = body.get("issues", []) or []
            for issue in issues:
                yield issue
            next_token = body.get("nextPageToken")
            if not next_token or not issues:
                return

    async def _iter_issues_dc(self, jql: str) -> AsyncIterator[dict[str, Any]]:
        start_at = 0
        max_results = 50
        pages = 0
        while pages < _MAX_PAGES:  # pragma: no branch
            pages += 1
            params: dict[str, Any] = {
                "jql": jql,
                "fields": "summary,description,updated,project",
                "startAt": start_at,
                "maxResults": max_results,
            }
            body = await self._get_json(_SEARCH_PATH, params=params)
            issues = body.get("issues", []) or []
            for issue in issues:
                yield issue
            if not issues:
                return
            total = body.get("total")
            start_at += len(issues)
            # Use server-reported max when present (lets the server
            # cap pages without us guessing wrong).
            srv_max = body.get("maxResults")
            if isinstance(srv_max, int) and srv_max > 0:
                max_results = srv_max
            if isinstance(total, int) and start_at >= total:
                return

    async def _iter_comments(
        self, issue_key: str
    ) -> AsyncIterator[dict[str, Any]]:
        start_at = 0
        pages = 0
        while pages < _MAX_PAGES:  # pragma: no branch
            pages += 1
            body = await self._get_json(
                _ISSUE_PATH_TPL.format(key=issue_key),
                params={"startAt": start_at, "maxResults": 100},
            )
            comments = body.get("comments", []) or []
            for c in comments:
                yield c
            if not comments:
                return
            total = body.get("total")
            start_at += len(comments)
            if isinstance(total, int) and start_at >= total:
                return

    async def _get_json(
        self, path: str, *, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        resp = await self._client.get(
            path, params=params, headers=self._auth_headers
        )
        resp.raise_for_status()
        return resp.json()


# --- auth ---------------------------------------------------------


def _build_auth_headers(cfg: JiraConfig) -> dict[str, str]:
    if cfg.deployment == "dc":
        return {"Authorization": f"Bearer {cfg.api_token}"}
    # Cloud: HTTP basic auth `email:api_token`.
    raw = f"{cfg.email}:{cfg.api_token}".encode()
    return {"Authorization": f"Basic {base64.b64encode(raw).decode()}"}


# --- JQL ----------------------------------------------------------


def _build_jql(projects: tuple[str, ...], cursor: Cursor | None) -> str:
    clauses: list[str] = []
    if projects:
        rendered = ", ".join(f'"{p}"' for p in projects)
        clauses.append(f"project in ({rendered})")
    if cursor:
        # Quote the timestamp so JQL accepts it verbatim.
        clauses.append(f'updated >= "{cursor}"')
    where = " AND ".join(clauses)
    if where:
        return f"{where} ORDER BY updated ASC"
    return "ORDER BY updated ASC"


# --- ADF → text ---------------------------------------------------


# Node types whose end implies a structural newline.
_BLOCK_NODES: frozenset[str] = frozenset(
    {"paragraph", "heading", "listItem", "blockquote", "codeBlock"}
)


def adf_to_text(node: Any) -> str:
    """Walk an ADF tree and emit plain text.

    Concatenates every `text` node verbatim. Inserts a newline after
    each block-level node (`paragraph`, `heading`, `listItem`, ...).
    Unknown node types are descended unconditionally — this means a
    new ADF node introduced by Atlassian still has its `text` children
    extracted, even before this walker is updated.
    """
    out: list[str] = []
    _walk_adf(node, out)
    # Collapse repeated blank lines so the output stays readable.
    text = "".join(out)
    # Trim trailing whitespace per line, drop trailing newlines.
    return text.rstrip()


def _walk_adf(node: Any, out: list[str]) -> None:
    if node is None:
        return
    if isinstance(node, list):
        for child in node:
            _walk_adf(child, out)
        return
    if not isinstance(node, Mapping):
        # Strings or other scalars at a non-text position — surface
        # them verbatim. Defensive against malformed wire data.
        if isinstance(node, str):
            out.append(node)
        return
    ntype = node.get("type")
    if ntype == "text":
        text = node.get("text", "")
        if isinstance(text, str):
            out.append(text)
        return
    # Descend into any "content" array (the canonical ADF child slot).
    content = node.get("content")
    if content is not None:
        _walk_adf(content, out)
    if ntype in _BLOCK_NODES:
        out.append("\n")


# --- serialisation ------------------------------------------------


def _serialise_issue(issue: Mapping[str, Any]) -> str:
    fields = issue.get("fields", {}) or {}
    summary = fields.get("summary", "") or ""
    desc = fields.get("description")
    desc_text = adf_to_text(desc) if desc else ""
    parts: list[str] = []
    parts.append(f"key={issue.get('key', '')}")
    parts.append(f"summary={summary}")
    if desc_text:
        parts.append(f"description={desc_text}")
    return "\n".join(parts)


def _serialise_comment(comment: Mapping[str, Any]) -> str:
    body = comment.get("body")
    body_text = adf_to_text(body) if body else ""
    author = comment.get("author") or {}
    author_name = author.get("displayName") if isinstance(author, Mapping) else ""
    parts: list[str] = []
    parts.append(f"id={comment.get('id', '')}")
    if author_name:
        parts.append(f"author={author_name}")
    if body_text:
        parts.append(f"body={body_text}")
    return "\n".join(parts)


# --- helpers ------------------------------------------------------


def _matches_any(s: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch(s, p) for p in patterns)


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    # Jira Cloud emits ISO-8601 with `Z` suffix; DC emits a numeric
    # offset without colon (`+0000`). Both round-trip through
    # `fromisoformat` after the `Z`→`+00:00` rewrite on Py3.12, with
    # `strptime` as a last-ditch fallback for the older format.
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        pass
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")
    except ValueError:
        return None


def _issue_url(base_url: str, issue_key: str, *, comment_id: str | None = None) -> str:
    base = base_url.rstrip("/")
    if comment_id:
        return f"{base}/browse/{issue_key}?focusedCommentId={comment_id}"
    return f"{base}/browse/{issue_key}"


# --- factory / spec -----------------------------------------------


def _factory(config: Mapping[str, Any]) -> SourceConnector:
    if "base_url" not in config:
        raise ValueError("jira connector config requires 'base_url'")
    if "api_token" not in config:
        raise ValueError("jira connector config requires 'api_token'")
    return JiraConnector(
        JiraConfig(
            base_url=str(config["base_url"]),
            email=str(config.get("email", "")),
            api_token=str(config["api_token"]),
            projects=tuple(str(p) for p in config.get("projects", ())),
            include_comments=bool(config.get("include_comments", True)),
            deployment=str(config.get("deployment", "cloud")),  # type: ignore[arg-type]
            id=str(config["id"]) if config.get("id") is not None else None,
        )
    )


SPEC = ConnectorSpec(
    kind="jira",
    version="0.1.0",
    factory=_factory,
    capabilities=Capabilities(
        incremental=True,
        binary=False,
        content_hash_delta=False,
        max_concurrent_fetches=4,
        streaming=False,
    ),
    required_scopes=("jira:read",),
    description=(
        "Atlassian Jira SourceConnector. Cloud + Data Center; paginates "
        "/rest/api/3/search with JQL `updated >= <cursor>` for incremental "
        "scans; walks ADF descriptions and comments into plain text."
    ),
)


__all__ = ["JiraConfig", "JiraConnector", "SPEC", "adf_to_text"]
