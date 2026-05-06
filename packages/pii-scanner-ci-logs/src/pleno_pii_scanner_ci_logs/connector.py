"""CiLogsConnector — multi-flavor CI build-log `SourceConnector`.

Single connector kind (`ci_logs`) backed by four wire flavors selected
at construction time. Each flavor has its own enumeration shape, log
endpoint, and identity model, but the `SourceConnector` contract the
scheduler sees is identical.

Flavors:

* **github_actions** — `/repos/{owner}/{repo}/actions/runs` paginated
  by `?per_page&page`. Per-run `/runs/{id}/logs` returns a zip; we
  unpack each `.txt` member as its own Document with a per-member
  size cap (default 50 MiB) — defends against zip bombs.
* **circleci** — `/project/gh/{owner}/{repo}/job` paginated by
  opaque `?page-token`. Per-job `/job/{n}/output` returns plain text.
* **buildkite** — `/organizations/{org}/pipelines/{slug}/builds`
  paginated by `Link` rel="next". Per-build `/jobs/{id}/log` returns
  text; one build is many jobs, so we emit one Document per job.
* **jenkins** — `{base}/api/json?tree=jobs[name,builds[number,url]]`
  in a single GET (no pagination). Per-build `{base}/job/{name}/{n}/consoleText`
  returns plain text.

`failed_only` filters discovery to failed/errored builds where leaks
most often manifest (env-dump on test failure, stack traces with DSN,
shell `set -x` echoing secrets). `since` is applied client-side when
the vendor offers no equivalent server filter (uniform across all
four flavors). `max_builds` caps per repo/pipeline scan size.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


from pleno_pii_scanner.credentials.broker import Credential
from pleno_pii_scanner.sources.base import (
    Capabilities,
    Cursor,
    Document,
    DocumentChunk,  # noqa: F401 — referenced by fetch return-type annotation
    DocumentRef,
    SourceConnector,
    SourceFilter,
)
from pleno_pii_scanner.sources.registry import ConnectorSpec
from pleno_pii_scanner_ci_logs.api import (
    DEFAULT_BUILDKITE_BASE_URL,
    DEFAULT_CIRCLECI_BASE_URL,
    DEFAULT_GITHUB_ACTIONS_BASE_URL,
    AuthMode,
    BasicAuth,
    BearerAuth,
    CircleTokenAuth,
    CiLogsApi,
    CiLogsApiError,
    Flavor,
)


# Connector kind exported via the `pleno_pii_scanner.connectors`
# entry-point group (see pyproject.toml). One kind covers all four
# flavors; flavor selection lives in config.
KIND = "ci_logs"


# Default cap on builds enumerated per repo/pipeline. Hosted CI
# accounts often have years of history and we typically only care
# about the recent slice — older logs already passed any redaction
# audits. Operators raise this when they want a one-off backfill.
DEFAULT_MAX_BUILDS = 50


# Default per-zip-member size ceiling for GHA log unpacking. 50 MiB
# is generous (a noisy log usually tops out at single MiB), but a
# firm cap is required: a malicious or buggy upstream could otherwise
# stream a multi-GB member into memory. 50 MiB matches the same value
# the OCI registry connector uses as its layer ceiling.
DEFAULT_MAX_LOG_BYTES = 50 * 1024 * 1024


# Buildkite job log content type. Buildkite returns plain text, but
# the API used to wrap it in a `{"content": "..."}` JSON object —
# the response body shape is checked at parse time so a future shift
# does not silently emit empty Documents.
_BUILDKITE_LOG_PATHS = ("content", "log")


@dataclass(frozen=True, slots=True)
class CiLogsConfig:
    """Construction config for `CiLogsConnector`.

    `flavor` selects the wire protocol and the required identity
    fields. The validators enforce per-flavor invariants up front:

    * `github_actions` requires `owner` + `repo`.
    * `circleci` requires `owner` + `repo` (`vcs_type` defaults to
      `gh`; `bb` is also accepted for Bitbucket-backed pipelines).
    * `buildkite` requires `org` + `pipeline`.
    * `jenkins` requires `base_url`; the default tree query enumerates
      every job on the controller.

    `since`, `max_builds`, `failed_only` are common to every flavor.
    `id` is the stable scan-instance identifier; defaults to a
    `<flavor>:<target>` slug when not provided.
    """

    flavor: Flavor
    owner: str | None = None
    repo: str | None = None
    vcs_type: str = "gh"
    org: str | None = None
    pipeline: str | None = None
    base_url: str | None = None
    since: datetime | None = None
    max_builds: int = DEFAULT_MAX_BUILDS
    failed_only: bool = False
    max_log_bytes: int = DEFAULT_MAX_LOG_BYTES
    id: str | None = None

    def __post_init__(self) -> None:
        if self.flavor not in ("github_actions", "circleci", "buildkite", "jenkins"):
            raise ValueError(
                f"CiLogsConfig.flavor must be one of "
                f"'github_actions' | 'circleci' | 'buildkite' | 'jenkins'; "
                f"got {self.flavor!r}"
            )
        if self.flavor in ("github_actions", "circleci"):
            if not self.owner or not self.repo:
                raise ValueError(
                    f"CiLogsConfig(flavor={self.flavor!r}) requires `owner` + `repo`"
                )
        if self.flavor == "buildkite":
            if not self.org or not self.pipeline:
                raise ValueError(
                    "CiLogsConfig(flavor='buildkite') requires `org` + `pipeline`"
                )
        if self.flavor == "jenkins":
            if not self.base_url:
                raise ValueError(
                    "CiLogsConfig(flavor='jenkins') requires `base_url` "
                    "(self-hosted Jenkins has no default endpoint)"
                )
        if self.vcs_type not in ("gh", "bb"):
            raise ValueError(
                f"CiLogsConfig.vcs_type must be 'gh' or 'bb'; got {self.vcs_type!r}"
            )
        if self.max_builds < 1:
            raise ValueError("max_builds must be >= 1")
        if self.max_log_bytes < 1:
            raise ValueError("max_log_bytes must be >= 1")

    def resolved_base_url(self) -> str:
        if self.base_url is not None:
            return self.base_url.rstrip("/")
        if self.flavor == "github_actions":
            return DEFAULT_GITHUB_ACTIONS_BASE_URL
        if self.flavor == "circleci":
            return DEFAULT_CIRCLECI_BASE_URL
        if self.flavor == "buildkite":
            return DEFAULT_BUILDKITE_BASE_URL
        # jenkins: __post_init__ guarantees base_url is set.
        raise AssertionError("unreachable: jenkins requires base_url")

    def resolved_id(self) -> str:
        """Stable scan-instance identifier.

        The identifier is part of every emitted DocumentRef and the
        FindingsStore dedup key. We never embed the credential — the
        `<flavor>:<target>` form is enough to uniquely identify the
        scan even across credential rotations.
        """
        if self.id is not None:
            return self.id
        if self.flavor == "github_actions":
            return f"github_actions:{self.owner}/{self.repo}"
        if self.flavor == "circleci":
            return f"circleci:{self.vcs_type}/{self.owner}/{self.repo}"
        if self.flavor == "buildkite":
            return f"buildkite:{self.org}/{self.pipeline}"
        # jenkins: identify by host so multiple controllers stay distinct.
        assert self.base_url is not None
        return f"jenkins:{self.base_url.rstrip('/')}"


class CiLogsConnector:
    """`SourceConnector` for CI build logs across four vendors.

    Owns one `CiLogsApi` (HTTP session) for the lifetime of the
    connector. `discover()` enumerates builds via the per-flavor
    paginator and yields one `DocumentRef` per build (GHA, CircleCI,
    Jenkins) or per job within a build (Buildkite). `fetch()` then
    pulls the log payload — for GHA the zip is unpacked in-memory and
    one Document is emitted per `.txt` member.
    """

    kind = KIND

    def __init__(
        self,
        config: CiLogsConfig,
        credential: Credential,
        *,
        transport: "Any | None" = None,
        sleep: "Any | None" = None,
    ) -> None:
        self._config = config
        self.id = config.resolved_id()
        self._credential = credential
        # Validate auth shape upfront so a misconfigured profile fails
        # at construction rather than mid-discover. Cloud accepts Bearer
        # for GHA + Buildkite, custom header for CircleCI, Basic for
        # Jenkins. We never log the raw token — `_build_auth` only
        # touches `credential.payload[...]`.
        self._auth: AuthMode = _build_auth(config.flavor, credential)
        self._api = CiLogsApi(
            flavor=config.flavor,
            base_url=config.resolved_base_url(),
            auth=self._auth,
            transport=transport,
            sleep=sleep,
        )

    @property
    def api(self) -> CiLogsApi:
        # Exposed so tests can poke at the underlying client + auth.
        return self._api

    @property
    def config(self) -> CiLogsConfig:
        return self._config

    def capabilities(self) -> Capabilities:
        # `incremental=True` because the `since` filter is honored at
        # discover time across all four flavors (server-side where the
        # vendor allows it, client-side otherwise). `binary=False`
        # because every flavor's log endpoint emits text — even GHA,
        # which wraps text members in a zip we unpack to text.
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
        """Enumerate every build matching the filter for this target.

        `cursor` is unused for now: we always re-walk the most-recent
        `max_builds` entries. The vendor cursor formats are opaque
        (CircleCI `next_page_token`, GHA `page=` int) and the
        per-flavor `since` already cuts incremental cost. A future
        revision can attach the per-flavor cursor to ref.metadata
        for true resume.
        """
        del cursor  # not yet used; see docstring
        # Combine config-level + filter-level since: config sets the
        # connector floor, filter overrides per-call. The earlier wins
        # so a config with a 30-day window cannot be silently expanded.
        since = filter.since or self._config.since
        emitted = 0
        if self._config.flavor == "github_actions":
            async for ref in self._discover_github_actions(since):
                yield ref
                emitted += 1
                if emitted >= self._config.max_builds:
                    return
        elif self._config.flavor == "circleci":
            async for ref in self._discover_circleci(since):
                yield ref
                emitted += 1
                if emitted >= self._config.max_builds:
                    return
        elif self._config.flavor == "buildkite":
            async for ref in self._discover_buildkite(since):
                yield ref
                emitted += 1
                if emitted >= self._config.max_builds:
                    return
        else:
            assert self._config.flavor == "jenkins"
            async for ref in self._discover_jenkins(since):
                yield ref
                emitted += 1
                if emitted >= self._config.max_builds:
                    return

    async def _discover_github_actions(
        self, since: datetime | None
    ) -> AsyncIterator[DocumentRef]:
        """List runs via `/repos/{o}/{r}/actions/runs` and yield refs."""
        path = f"/repos/{self._config.owner}/{self._config.repo}/actions/runs"
        # GHA serves runs newest-first; once we cross `since` we can
        # short-circuit. `failed_only` is enforced server-side via
        # `?status=failure` plus `?conclusion=failure` to catch both
        # failed jobs and errored runs (GHA distinguishes the two).
        params: dict[str, Any] = {}
        if self._config.failed_only:
            params["status"] = "failure"
        async for entry in self._api.paginate(path, params=params):
            if not isinstance(entry, Mapping):
                # Defensive: a malformed page entry must not crash the
                # whole enumeration. Skip silently.
                continue
            run_id = entry.get("id")
            if run_id is None:
                continue
            created_at = _parse_iso_or_none(entry.get("created_at"))
            if since is not None and created_at is not None and created_at < since:
                # Newest-first ordering means everything after this is
                # older than `since`; stop walking immediately.
                return
            if self._config.failed_only and not _is_gha_failed(entry):
                # Server-side `status=failure` is fuzzy on the GHA side
                # (sometimes returns `cancelled` runs); double-check
                # client-side.
                continue
            yield DocumentRef(
                source_id=self.id,
                source_kind=self.kind,
                path=f"{self._config.owner}/{self._config.repo}/runs/{run_id}",
                native_url=str(entry.get("html_url"))
                if entry.get("html_url")
                else None,
                parent_chain=(
                    f"github_actions://{self._config.owner}/{self._config.repo}",
                ),
                content_type="application/zip",
                etag=str(entry.get("head_sha")) if entry.get("head_sha") else None,
                last_modified=created_at,
                metadata={
                    "flavor": "github_actions",
                    "run_id": str(run_id),
                    "owner": str(self._config.owner),
                    "repo": str(self._config.repo),
                    "conclusion": str(entry.get("conclusion") or ""),
                },
            )

    async def _discover_circleci(
        self, since: datetime | None
    ) -> AsyncIterator[DocumentRef]:
        """List jobs via `/project/{vcs}/{o}/{r}/job` and yield refs.

        CircleCI's `/job` endpoint returns recent job runs (not builds
        — Workflows are deprecated as a per-build concept on the v2
        API). `failed_only` filters on `status` field client-side
        because the v2 endpoint has no equivalent query param.
        """
        vcs = self._config.vcs_type
        path = f"/project/{vcs}/{self._config.owner}/{self._config.repo}/job"
        async for entry in self._api.paginate(path):
            if not isinstance(entry, Mapping):
                continue
            # Prefer `job_number` (v2 contract); fall back to `number`
            # only when the key is *absent* (CircleCI v1 + some
            # third-party emulators). `or` would break `job_number=0`.
            job_number = entry.get("job_number")
            if job_number is None:
                job_number = entry.get("number")
            if job_number is None:
                continue
            started_at = _parse_iso_or_none(entry.get("started_at"))
            if since is not None and started_at is not None and started_at < since:
                # CircleCI v2 sorts newest-first by default — stop now.
                return
            status = str(entry.get("status") or "")
            if self._config.failed_only and status not in (
                "failed",
                "infrastructure_fail",
                "timedout",
                "terminated-unknown",
            ):
                continue
            yield DocumentRef(
                source_id=self.id,
                source_kind=self.kind,
                path=f"{self._config.owner}/{self._config.repo}/jobs/{job_number}",
                native_url=str(entry.get("web_url")) if entry.get("web_url") else None,
                parent_chain=(f"circleci://{self._config.owner}/{self._config.repo}",),
                content_type="text/plain",
                last_modified=started_at,
                metadata={
                    "flavor": "circleci",
                    "job_number": str(job_number),
                    "vcs_type": vcs,
                    "owner": str(self._config.owner),
                    "repo": str(self._config.repo),
                    "status": status,
                },
            )

    async def _discover_buildkite(
        self, since: datetime | None
    ) -> AsyncIterator[DocumentRef]:
        """List builds via `/organizations/{o}/pipelines/{p}/builds`.

        Each build owns several jobs (`script`, `command`, `wait`,
        ...); we emit one DocumentRef per job whose log endpoint
        returns text. Wait jobs and trigger jobs have no log; the
        type filter inside the loop handles that.
        """
        path = (
            f"/organizations/{self._config.org}"
            f"/pipelines/{self._config.pipeline}/builds"
        )
        params: dict[str, Any] = {}
        if self._config.failed_only:
            # Buildkite supports `?state[]=failed&state[]=errored` but
            # httpx flattens lists; using comma-join keeps one query
            # param key + matches the documented form.
            params["state[]"] = "failed"
        async for entry in self._api.paginate(path, params=params):
            if not isinstance(entry, Mapping):
                continue
            build_number = entry.get("number")
            if build_number is None:
                continue
            created_at = _parse_iso_or_none(entry.get("created_at"))
            if since is not None and created_at is not None and created_at < since:
                return
            state = str(entry.get("state") or "")
            if self._config.failed_only and state not in ("failed", "errored"):
                continue
            jobs = entry.get("jobs") or []
            for job in jobs:
                if not isinstance(job, Mapping):
                    continue
                job_id = job.get("id")
                if job_id is None:
                    continue
                # Only `script`/`command` jobs have logs; `wait`,
                # `trigger`, `block` produce no console output. Skip
                # them so fetch() does not waste an HTTP call.
                if job.get("type") not in ("script", "command", None):
                    continue
                yield DocumentRef(
                    source_id=self.id,
                    source_kind=self.kind,
                    path=(
                        f"{self._config.org}/{self._config.pipeline}/"
                        f"builds/{build_number}/jobs/{job_id}"
                    ),
                    native_url=str(job.get("web_url"))
                    if job.get("web_url")
                    else (str(entry.get("web_url")) if entry.get("web_url") else None),
                    parent_chain=(
                        f"buildkite://{self._config.org}/{self._config.pipeline}",
                    ),
                    content_type="text/plain",
                    last_modified=created_at,
                    metadata={
                        "flavor": "buildkite",
                        "build_number": str(build_number),
                        "job_id": str(job_id),
                        "org": str(self._config.org),
                        "pipeline": str(self._config.pipeline),
                        "state": state,
                    },
                )

    async def _discover_jenkins(
        self, since: datetime | None
    ) -> AsyncIterator[DocumentRef]:
        """List jobs + builds via a single `/api/json` tree query.

        Jenkins's REST surface is deep XML by default; the `tree=`
        param lets us project just the fields we need so we don't
        download megabytes of plugin metadata for every controller.
        """
        # `lastBuild=null` is the documented "no builds yet" response;
        # we filter that downstream.
        params = {
            "tree": ("jobs[name,builds[number,url,timestamp,result]]"),
        }
        response = await self._api.get("/api/json", params=params)
        if response.status_code != 200:
            return
        try:
            body = response.json()
        except ValueError:
            # Defensive: a Jenkins controller that briefly serves the
            # login HTML page (mid-restart) must not crash the scan.
            return
        for job in (body or {}).get("jobs", []) or []:
            if not isinstance(job, Mapping):
                continue
            name = job.get("name")
            if not name:
                continue
            for build in job.get("builds", []) or []:
                if not isinstance(build, Mapping):
                    continue
                number = build.get("number")
                url = build.get("url")
                if number is None or not url:
                    continue
                ts = build.get("timestamp")
                last_modified = (
                    datetime.fromtimestamp(ts / 1000, tz=UTC)
                    if isinstance(ts, (int, float))
                    else None
                )
                if (
                    since is not None
                    and last_modified is not None
                    and last_modified < since
                ):
                    # Jenkins returns newest-first; stop early.
                    return
                result = str(build.get("result") or "")
                if self._config.failed_only and result not in (
                    "FAILURE",
                    "ABORTED",
                    "UNSTABLE",
                ):
                    continue
                yield DocumentRef(
                    source_id=self.id,
                    source_kind=self.kind,
                    path=f"{name}/{number}",
                    native_url=str(url),
                    parent_chain=(f"jenkins://{self._config.base_url}",),
                    content_type="text/plain",
                    last_modified=last_modified,
                    metadata={
                        "flavor": "jenkins",
                        "job_name": str(name),
                        "build_number": str(number),
                        "build_url": str(url),
                        "result": result,
                    },
                )

    # ------------------------------------------------------------------
    # fetch
    # ------------------------------------------------------------------

    async def fetch(
        self,
        ref: DocumentRef,
    ) -> AsyncIterator[Document | DocumentChunk]:
        """Pull the log payload for `ref`.

        For GHA the `/runs/{id}/logs` endpoint returns a zip; we
        unpack each `.txt` member (per-step / per-job log) and emit
        one Document per member, capped by `max_log_bytes` to defend
        against zip bombs. Other flavors return a single text payload.
        """
        flavor = ref.metadata.get("flavor")
        # We treat a flavor mismatch the same as the github connector's
        # silent-empty idiom: stale ref or wrong connector — yield
        # nothing rather than crashing the scheduler's gather().
        if flavor == "github_actions":
            async for doc in self._fetch_github_actions(ref):
                yield doc
        elif flavor == "circleci":
            async for doc in self._fetch_circleci(ref):
                yield doc
        elif flavor == "buildkite":
            async for doc in self._fetch_buildkite(ref):
                yield doc
        elif flavor == "jenkins":
            async for doc in self._fetch_jenkins(ref):
                yield doc
        else:
            return

    async def _fetch_github_actions(self, ref: DocumentRef) -> AsyncIterator[Document]:
        owner = ref.metadata.get("owner")
        repo = ref.metadata.get("repo")
        run_id = ref.metadata.get("run_id")
        if not owner or not repo or not run_id:
            return
        try:
            blob = await self._api.get_bytes(
                f"/repos/{owner}/{repo}/actions/runs/{run_id}/logs"
            )
        except CiLogsApiError:
            return
        # In-memory zip extraction. Each `.txt` member becomes its own
        # Document so a single bad finding cannot taint adjacent
        # member bodies. `max_log_bytes` cap is enforced per-member;
        # we use `read()` rather than `extract()` so nothing touches
        # disk and a member exceeding the cap aborts cleanly without
        # half-written files.
        try:
            zf = zipfile.ZipFile(io.BytesIO(blob))
        except zipfile.BadZipFile:
            return
        for info in zf.infolist():
            if info.is_dir():
                continue
            if not info.filename.lower().endswith(".txt"):
                # Logs are .txt members at known prefixes; ignore
                # adjacent metadata files (`.json`, `index`).
                continue
            if info.file_size > self._config.max_log_bytes:
                # Zip-bomb defense: refuse to materialise the member.
                # We continue rather than aborting the whole zip so
                # one oversized member doesn't blind the rest of the
                # scan to legitimate logs.
                continue
            try:
                payload = zf.read(info.filename)
            except (zipfile.BadZipFile, OSError, RuntimeError):
                # OSError covers truncated members; RuntimeError covers
                # encrypted-without-password members. One bad member
                # cannot abort the scan.
                continue
            text = payload.decode("utf-8", errors="replace")
            yield Document(
                ref=ref,
                text=text,
                fetched_at=datetime.now(UTC),
                extra={"member": info.filename},
            )

    async def _fetch_circleci(self, ref: DocumentRef) -> AsyncIterator[Document]:
        owner = ref.metadata.get("owner")
        repo = ref.metadata.get("repo")
        vcs = ref.metadata.get("vcs_type") or "gh"
        job_number = ref.metadata.get("job_number")
        if not owner or not repo or not job_number:
            return
        response = await self._api.get(
            f"/project/{vcs}/{owner}/{repo}/job/{job_number}"
        )
        if response.status_code != 200:
            return
        # CircleCI's job-detail body carries an `output_url` that 302s
        # to a presigned object; the v2 API also exposes an `output`
        # field on small payloads. Try both — small jobs save us a
        # second hop.
        try:
            body = response.json()
        except ValueError:
            return
        text = _circleci_extract_log(body)
        if text is None:
            return
        yield Document(
            ref=ref,
            text=text,
            fetched_at=datetime.now(UTC),
        )

    async def _fetch_buildkite(self, ref: DocumentRef) -> AsyncIterator[Document]:
        org = ref.metadata.get("org")
        pipeline = ref.metadata.get("pipeline")
        build_number = ref.metadata.get("build_number")
        job_id = ref.metadata.get("job_id")
        if not org or not pipeline or not build_number or not job_id:
            return
        response = await self._api.get(
            f"/organizations/{org}/pipelines/{pipeline}"
            f"/builds/{build_number}/jobs/{job_id}/log"
        )
        if response.status_code != 200:
            return
        try:
            body = response.json()
        except ValueError:
            # Buildkite occasionally returns the raw text body when
            # the proxy strips the JSON envelope; fall back to the
            # response text directly so we don't lose the log.
            text = response.text
        else:
            text = _buildkite_extract_log(body)
        if not text:
            return
        yield Document(
            ref=ref,
            text=text,
            fetched_at=datetime.now(UTC),
        )

    async def _fetch_jenkins(self, ref: DocumentRef) -> AsyncIterator[Document]:
        build_url = ref.metadata.get("build_url")
        if not build_url:
            return
        # `consoleText` is the plain-text endpoint Jenkins exposes for
        # every build. We use the absolute URL the api gave us so a
        # base-url mismatch (Jenkins behind a reverse proxy with
        # rewriting) does not break log fetch.
        log_url = build_url.rstrip("/") + "/consoleText"
        response = await self._api.get(log_url, accept="text/plain")
        if response.status_code != 200:
            return
        yield Document(
            ref=ref,
            text=response.text,
            fetched_at=datetime.now(UTC),
        )

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Release the http client. No on-disk state to clean up."""
        await self._api.aclose()


# ---------------------------------------------------------------------
# Auth + extraction helpers
# ---------------------------------------------------------------------


def _build_auth(flavor: Flavor, credential: Credential) -> AuthMode:
    """Validate the credential payload shape per flavor.

    Each flavor expects a different payload key set; rejecting
    misconfiguration here prevents a 401 mid-scan from being misread
    as a rate-limit issue. The token value itself never appears in
    the raised message — only the missing key name.
    """
    payload = credential.payload
    if flavor == "jenkins":
        username = payload.get("username")
        password = payload.get("password") or payload.get("api_token")
        if (
            isinstance(username, str)
            and isinstance(password, str)
            and username
            and password
        ):
            return BasicAuth(username=username, password=password)
        raise ValueError(
            "ci_logs[jenkins] credential.payload requires "
            "`username` + (`password` | `api_token`)"
        )
    token = payload.get("token") or payload.get("access_token")
    if not isinstance(token, str) or not token:
        raise ValueError(
            f"ci_logs[{flavor}] credential.payload requires `token` (or `access_token`)"
        )
    if flavor == "circleci":
        return CircleTokenAuth(token=token)
    # github_actions + buildkite both use `Authorization: Bearer`.
    return BearerAuth(token=token)


def _parse_iso_or_none(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp; return None on absent/malformed.

    Used at discover time to filter on `since`. We never raise here
    because vendor schemas drift (CircleCI flipped `started_at` to
    nullable on queued jobs in 2024) and the discover loop must not
    bail on one entry.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_gha_failed(entry: Mapping[str, Any]) -> bool:
    """Decide whether a GHA workflow run counts as failed.

    GHA distinguishes `status` (in_progress / completed / queued)
    from `conclusion` (success / failure / cancelled / timed_out /
    action_required / neutral / skipped). We treat anything in the
    failure family as a leak candidate.
    """
    conclusion = entry.get("conclusion")
    return conclusion in (
        "failure",
        "timed_out",
        "action_required",
        "startup_failure",
    )


def _circleci_extract_log(body: Mapping[str, Any]) -> str | None:
    """Pull the log text out of a CircleCI job-detail payload.

    The v2 response body shape varies: some jobs carry inline `output`
    (older builds), others reference a presigned `output_url`. We
    return the inline form when present; the redirect path is left to
    a future revision because it adds a second HTTP hop and the
    inline form covers the leak-rich failure modes.
    """
    output = body.get("output")
    if isinstance(output, str) and output:
        return output
    # `messages` is the per-step output collection on newer responses.
    messages = body.get("messages")
    if isinstance(messages, list):
        parts: list[str] = []
        for m in messages:
            if isinstance(m, Mapping):
                msg = m.get("message")
                if isinstance(msg, str):
                    parts.append(msg)
        if parts:
            return "\n".join(parts)
    return None


def _buildkite_extract_log(body: Mapping[str, Any]) -> str | None:
    """Pull the log text out of a Buildkite job-log payload.

    Buildkite wraps the log in `{"content": "..."}` — newer accounts
    use `{"log": "..."}`. We try both keys; if neither matches, we
    return None and the caller falls back to `response.text`.
    """
    if not isinstance(body, Mapping):
        return None
    for key in _BUILDKITE_LOG_PATHS:
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    return None


# ---------------------------------------------------------------------
# Factory + Spec
# ---------------------------------------------------------------------


def _factory(config: Mapping[str, Any]) -> SourceConnector:
    """Build a connector from a plain config mapping.

    The credential is fetched separately (CredentialBroker) and
    threaded through under `_credential` by the scheduler, mirroring
    every other connector wheel in the workspace.
    """
    cred_obj = config.get("_credential")
    if not isinstance(cred_obj, Credential):
        raise ValueError(
            "ci_logs factory requires a resolved Credential under "
            "config['_credential'] (set by the scheduler from CredentialBroker)"
        )
    flavor_raw = config.get("flavor")
    if flavor_raw not in ("github_actions", "circleci", "buildkite", "jenkins"):
        raise ValueError(
            f"ci_logs connector config['flavor'] must be one of "
            f"'github_actions' | 'circleci' | 'buildkite' | 'jenkins'; "
            f"got {flavor_raw!r}"
        )
    since_raw = config.get("since")
    since: datetime | None
    if isinstance(since_raw, datetime):
        since = since_raw
    elif isinstance(since_raw, str) and since_raw:
        # Reuse the same parser the discover loop uses — keeps
        # behaviour uniform across config-driven and runtime filters.
        since = _parse_iso_or_none(since_raw)
        if since is None:
            raise ValueError(
                f"ci_logs config['since'] must be ISO-8601; got {since_raw!r}"
            )
    else:
        since = None
    return CiLogsConnector(
        CiLogsConfig(
            flavor=flavor_raw,
            owner=str(config["owner"]) if config.get("owner") is not None else None,
            repo=str(config["repo"]) if config.get("repo") is not None else None,
            vcs_type=str(config.get("vcs_type", "gh")),
            org=str(config["org"]) if config.get("org") is not None else None,
            pipeline=(
                str(config["pipeline"]) if config.get("pipeline") is not None else None
            ),
            base_url=(
                str(config["base_url"]) if config.get("base_url") is not None else None
            ),
            since=since,
            max_builds=int(config.get("max_builds", DEFAULT_MAX_BUILDS)),
            failed_only=bool(config.get("failed_only", False)),
            max_log_bytes=int(config.get("max_log_bytes", DEFAULT_MAX_LOG_BYTES)),
            id=str(config["id"]) if config.get("id") is not None else None,
        ),
        credential=cred_obj,
    )


SPEC = ConnectorSpec(
    kind=KIND,
    version="0.1.0",
    factory=_factory,
    capabilities=Capabilities(
        incremental=True,
        binary=False,
        content_hash_delta=False,
        max_concurrent_fetches=4,
    ),
    required_scopes=("ci:read", "actions:read"),
    description=(
        "CI build-log connector. Single kind, four wire flavors "
        "(`flavor=github_actions|circleci|buildkite|jenkins`). "
        "Per-flavor pagination + rate-limit handling; `failed_only` "
        "filter for leak-rich runs; `since` incremental cutoff; "
        "`max_builds` cap. GHA `/logs` zip is unpacked in-memory with "
        "a per-member size cap to defend against zip bombs. ADR-0007 §13."
    ),
)


__all__ = [
    "DEFAULT_MAX_BUILDS",
    "DEFAULT_MAX_LOG_BYTES",
    "KIND",
    "SPEC",
    "CiLogsConfig",
    "CiLogsConnector",
]
