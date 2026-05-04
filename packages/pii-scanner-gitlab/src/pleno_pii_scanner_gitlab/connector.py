"""GitlabConnector — SaaS + self-managed GitLab `SourceConnector`.

Hits the REST API for project enumeration and shallow-clones each
project into a temp dir for content scan. The clone path mirrors the
core wheel's builtin `github` connector so the same `clone_fn` /
`enumerate_fn` test seams keep tests hermetic.

Targets (exactly one required):

  * `project="ns/path"` — single project (URL-encoded path used
    server-side; we accept the `/`-separated form operators paste).
  * `group="ns"` — recursive walk of every project under the group,
    honouring `include_subgroups=true`.

Auth (chosen at credential time):

  * PAT          — `auth=pat`,     `token=glpat-...`
  * OAuth2       — `auth=oauth`,   `access_token=...`
  * Project token — `auth=project`, `token=glpat-project-...`

Self-managed deployments behind a private CA pass `ca_bundle_path` in
the source config; we forward it as httpx's `verify=`.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
import urllib.parse
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from pleno_pii_scanner.credentials.broker import Credential
from pleno_pii_scanner.scheduler.rate_limit import RateLimited  # noqa: F401 — re-export
from pleno_pii_scanner.sources.base import (
    Capabilities,
    Cursor,
    Document,
    DocumentChunk,  # noqa: F401 — referenced by fetch's return-type annotation
    DocumentRef,
    SourceFilter,
)
from pleno_pii_scanner.sources.builtin.dir_source import DirConfig, DirConnector
from pleno_pii_scanner.sources.registry import ConnectorSpec

from pleno_pii_scanner_gitlab.api import DEFAULT_BASE_URL, GitlabApi
from pleno_pii_scanner_gitlab.auth import GitlabAuthMode, parse_auth_mode


# Connector kind (entry-point key).
KIND = "gitlab"

# Visibility values GitLab's API accepts on the projects endpoints. Anything
# else is rejected at config time so a typo (`"publik"`) does not silently
# turn into "no filter applied".
_LEGAL_VISIBILITY: frozenset[str] = frozenset({"private", "internal", "public"})

# Page size we request. GitLab caps `per_page` at 100 — anything higher is
# silently clamped, but explicit is better than implicit.
_PAGE_SIZE = 100


# Test seam: factory that turns (project, config, token) into a local
# cloned path. Production default is `_clone_into_tempdir`, which shells
# out to `git clone --depth=1`. Same shape as the builtin github source.
ProjectInfo = Mapping[str, Any]
CloneFn = Callable[["GitlabConnector", ProjectInfo], Path]
# Enumeration is async because it walks paginated REST. Tests inject an
# AsyncIterable to drive the connector without touching the API client.
EnumerateFn = Callable[
    ["GitlabConnector"], AsyncIterator[ProjectInfo]
]


@dataclass(frozen=True, slots=True)
class GitlabConfig:
    """Construction config for `GitlabConnector`.

    Exactly one of `project` / `group` is required. All other fields
    have safe defaults; `base_url` defaults to gitlab.com and only
    needs to change for self-managed instances.
    """

    project: str | None = None
    group: str | None = None
    base_url: str = DEFAULT_BASE_URL
    include_archived: bool = False
    visibility: str | None = None
    ca_bundle_path: str | None = None
    id: str | None = None

    def __post_init__(self) -> None:
        targets = [t for t in (self.project, self.group) if t is not None]
        if len(targets) != 1:
            raise ValueError(
                "GitlabConfig must set exactly one of `project` or `group`"
            )
        if self.visibility is not None and self.visibility not in _LEGAL_VISIBILITY:
            raise ValueError(
                f"visibility must be one of {sorted(_LEGAL_VISIBILITY)} or None; "
                f"got {self.visibility!r}"
            )

    def resolved_id(self) -> str:
        if self.id is not None:
            return self.id
        if self.group is not None:
            return f"gitlab-group:{self.group}"
        return f"gitlab:{self.project}"


class GitlabConnector:
    """`SourceConnector` backed by GitLab REST + shallow clone.

    Owns one `GitlabApi` (HTTP session) and one or more shallow clones.
    Clones live for the connector's lifetime under `/tmp/pleno-gl-*`
    and are rmtree'd in `close()` (best-effort; see comment).
    """

    kind = KIND

    def __init__(
        self,
        config: GitlabConfig,
        credential: Credential,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clone_fn: CloneFn | None = None,
        enumerate_fn: EnumerateFn | None = None,
    ) -> None:
        self._config = config
        self.id = config.resolved_id()
        self._credential = credential
        # Validate credential payload up-front so a misconfigured profile
        # surfaces at construction time, not from the first 401.
        auth_mode, token = _extract_credential(credential)
        self._auth_mode = auth_mode
        self._token = token
        # `verify` honours the operator-supplied CA bundle path. httpx
        # treats a string as a path and `True` as the system bundle.
        verify: bool | str = (
            config.ca_bundle_path if config.ca_bundle_path is not None else True
        )
        self._api = GitlabApi(
            base_url=config.base_url,
            auth_mode=auth_mode,
            token=token,
            transport=transport,
            verify=verify,
        )
        self._clone_fn: CloneFn = clone_fn or _clone_into_tempdir
        self._enumerate_fn: EnumerateFn | None = enumerate_fn
        # path_with_namespace -> cloned dir; lazy.
        self._clones: dict[str, Path] = {}
        self._tempdirs: list[Path] = []
        self._lock = asyncio.Lock()

    def capabilities(self) -> Capabilities:
        return Capabilities(
            incremental=True,
            binary=False,
            content_hash_delta=False,
            max_concurrent_fetches=4,
            streaming=False,
        )

    async def close(self) -> None:
        # Best-effort tempdir cleanup. Same rationale as builtin github:
        # close() runs from a finally; raising here would mask whatever
        # error caused close() to fire. ignore_errors keeps a rmtree
        # that races with NFS stat() calls from blowing up the scan.
        async with self._lock:
            for path in self._tempdirs:
                await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)
            self._tempdirs.clear()
            self._clones.clear()
        await self._api.aclose()

    # ------------------------------------------------------------------
    # discover
    # ------------------------------------------------------------------

    async def discover(
        self,
        filter: SourceFilter,
        cursor: Cursor | None,
    ) -> AsyncIterator[DocumentRef]:
        """Enumerate projects then yield blob refs from each shallow clone."""
        # `cursor` is unused: GitLab's keyset pagination requires opaque
        # `pagination=keyset&per_page=...&order_by=id` continuation URLs
        # which we follow inline via the Link header. Persisting a
        # cursor across discover() invocations would mean re-issuing
        # the same enumeration with a stale URL whose schema may have
        # changed across GitLab versions; cheaper to re-enumerate.
        del cursor
        projects = (
            self._enumerate_fn(self) if self._enumerate_fn is not None
            else self._iter_projects()
        )
        async for project in projects:
            path_with_ns = project.get("path_with_namespace")
            if not isinstance(path_with_ns, str):
                # Malformed page entry — skip rather than crash the scan.
                continue
            try:
                clone_path = await self._ensure_clone(project)
            except _CloneFailed:
                # Clone failure for a single project must not abort the
                # group walk. The error has been logged by the clone fn;
                # we move on to the next project.
                continue
            inner = DirConnector(
                DirConfig(root=clone_path, id=f"{KIND}:{path_with_ns}")
            )
            try:
                async for inner_ref in inner.discover(filter, None):
                    yield self._wrap_ref(inner_ref, project)
            finally:
                await inner.close()

    async def _iter_projects(self) -> AsyncIterator[ProjectInfo]:
        """Page through the configured target.

        For `project=...`, hit the single-project endpoint (one HTTP).
        For `group=...`, walk `/groups/:id/projects?include_subgroups=true`
        until the Link `rel="next"` header drops off.
        """
        if self._config.project is not None:
            encoded = urllib.parse.quote(self._config.project, safe="")
            response = await self._api.get(f"/projects/{encoded}")
            if response.status_code != 200:
                return
            project = response.json()
            if self._project_passes_filters(project):
                yield project
            return
        assert self._config.group is not None
        encoded = urllib.parse.quote(self._config.group, safe="")
        params: dict[str, Any] = {
            "include_subgroups": "true",
            "per_page": str(_PAGE_SIZE),
            # Server-side archived filter — cheap and removes
            # archived repos before they hit our wire.
            "archived": "true" if self._config.include_archived else "false",
        }
        if self._config.visibility is not None:
            params["visibility"] = self._config.visibility
        url: str = f"/groups/{encoded}/projects"
        first = True
        while True:
            response = await self._api.get(
                url, params=params if first else None
            )
            if response.status_code != 200:
                return
            page = response.json()
            if not isinstance(page, list):
                # GitLab returns a JSON object on error responses (which
                # we already filtered by status_code) — treat anything
                # non-list as malformed and stop the walk.
                return
            for project in page:
                if not isinstance(project, dict):
                    continue
                if self._project_passes_filters(project):
                    yield project
            next_url = self._api.parse_next_link(response)
            if next_url is None:
                return
            url = next_url
            first = False  # subsequent pages embed the cursor in the URL

    def _project_passes_filters(self, project: ProjectInfo) -> bool:
        """Belt-and-braces client-side filter on archived flag.

        We pass `archived=` server-side too, but GitLab < 13.0 ignored
        the flag on the `/groups/:id/projects` endpoint, and the
        single-project lookup has no `archived=` param at all. A second
        check here keeps behaviour consistent across deployments.
        """
        if not self._config.include_archived and project.get("archived"):
            return False
        return True

    # ------------------------------------------------------------------
    # fetch
    # ------------------------------------------------------------------

    async def fetch(
        self,
        ref: DocumentRef,
    ) -> AsyncIterator[Document | DocumentChunk]:
        """Replay the dir-connector fetch against the existing clone.

        `ref.metadata` carries `path_with_namespace` (set by `_wrap_ref`).
        If discover() never ran (or ran in a sibling instance), the
        clone is missing — we yield nothing rather than re-cloning,
        because re-cloning here would surprise the scheduler with an
        unbounded latency spike per fetch.
        """
        path_with_ns = ref.metadata.get("path_with_namespace")
        if not isinstance(path_with_ns, str) or path_with_ns not in self._clones:
            return
        clone_path = self._clones[path_with_ns]
        inner = DirConnector(
            DirConfig(root=clone_path, id=f"{KIND}:{path_with_ns}")
        )
        try:
            inner_ref = self._unwrap_ref(ref)
            async for doc in inner.fetch(inner_ref):
                # DirConnector advertises streaming=False; it only ever
                # yields Document. Re-emit with our outer ref so finding
                # paths report `ns/repo/file` instead of the local clone.
                if not isinstance(doc, Document):
                    continue  # defensive — DirConnector contract change
                yield Document(
                    ref=ref,
                    text=doc.text,
                    binary=doc.binary,
                    fetched_at=doc.fetched_at or datetime.now(UTC),
                    content_hash=doc.content_hash,
                    created_by=doc.created_by,
                    extra=doc.extra,
                )
        finally:
            await inner.close()

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    async def _ensure_clone(self, project: ProjectInfo) -> Path:
        path_with_ns = project["path_with_namespace"]
        async with self._lock:
            cached = self._clones.get(path_with_ns)
            if cached is not None:
                return cached
        # Run the synchronous clone in a thread so the scheduler keeps
        # making progress on other connectors while git is in flight.
        try:
            path = await asyncio.to_thread(self._clone_fn, self, project)
        except subprocess.CalledProcessError as exc:
            # Surface as our internal sentinel; discover() catches it
            # and skips the project. We do not log the exit code here
            # because that is the clone fn's job — keeps secrets out
            # of our exception chain.
            raise _CloneFailed(path_with_ns) from exc
        async with self._lock:
            # Double-check: a concurrent discover() may have populated
            # the entry while we awaited the clone. Both clones land
            # on disk; the loser is rmtree'd here to avoid leaks.
            if path_with_ns in self._clones:
                await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)
                return self._clones[path_with_ns]
            self._clones[path_with_ns] = path
            self._tempdirs.append(path)
        return path

    def _wrap_ref(
        self, inner: DocumentRef, project: ProjectInfo
    ) -> DocumentRef:
        path_with_ns = project["path_with_namespace"]
        web_url = project.get("web_url") or f"{self._config.base_url.rstrip('/')}/{path_with_ns}"
        # Surface the project metadata enough that the FindingsStore
        # can render `<group>/<project>:<file>:<line>` without a second
        # API hop.
        return DocumentRef(
            source_id=self.id,
            source_kind=self.kind,
            path=f"{path_with_ns}/{inner.path}",
            native_url=f"{web_url}/-/blob/HEAD/{inner.path}",
            parent_chain=(f"gitlab://{path_with_ns}",),
            content_type=inner.content_type,
            size=inner.size,
            etag=inner.etag,
            last_modified=inner.last_modified,
            metadata={
                **inner.metadata,
                "path_with_namespace": path_with_ns,
                "project_id": str(project.get("id", "")),
                "default_branch": str(project.get("default_branch", "HEAD")),
                "inner_path": inner.path,
            },
        )

    def _unwrap_ref(self, ref: DocumentRef) -> DocumentRef:
        # `inner_path` was set in `_wrap_ref`; unwrap to a DirConnector
        # ref so the dir source treats us as a plain filesystem walker.
        path_with_ns = ref.metadata["path_with_namespace"]
        inner_path = ref.metadata["inner_path"]
        return DocumentRef(
            source_id=f"{KIND}:{path_with_ns}",
            source_kind="dir",
            path=inner_path,
            content_type=ref.content_type,
            size=ref.size,
            etag=ref.etag,
            last_modified=ref.last_modified,
        )

    # Properties exposed for `clone_fn` so the seam can read base_url
    # and credential token without touching private attributes.
    @property
    def base_url(self) -> str:
        return self._config.base_url

    @property
    def token(self) -> str:
        return self._token

    @property
    def auth_mode(self) -> GitlabAuthMode:
        return self._auth_mode


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


class _CloneFailed(Exception):
    """Internal sentinel: a clone failed and the project should be skipped.

    Intentionally not part of the public API — the connector swallows
    these in discover(). We keep it as a real exception class (not just
    a tuple-return) so the traceback surfaces the underlying
    CalledProcessError in `__cause__` for ops to grep in logs.
    """


def _extract_credential(cred: Credential) -> tuple[GitlabAuthMode, str]:
    """Validate and unwrap the credential payload.

    Required keys differ per mode:

      * pat / project: `auth`, `token`
      * oauth:         `auth`, `access_token`

    Either token field is acceptable for any mode (so operators
    rotating between PAT and OAuth do not have to rename keys), but
    we pick the canonical one first to minimise surprise.
    """
    payload = cred.payload
    raw_mode = payload.get("auth")
    if not isinstance(raw_mode, str):
        raise ValueError(
            "gitlab credential.payload requires `auth` "
            "(one of 'pat', 'oauth', 'project')"
        )
    mode = parse_auth_mode(raw_mode)
    # OAuth canonical key is `access_token`; PAT/project canonical is `token`.
    primary = "access_token" if mode is GitlabAuthMode.OAUTH else "token"
    fallback = "token" if mode is GitlabAuthMode.OAUTH else "access_token"
    token = payload.get(primary)
    if not isinstance(token, str) or not token:
        token = payload.get(fallback)
    if not isinstance(token, str) or not token:
        raise ValueError(
            f"gitlab credential.payload requires a non-empty "
            f"{primary!r} (or {fallback!r}) string"
        )
    return mode, token


def _clone_into_tempdir(connector: GitlabConnector, project: ProjectInfo) -> Path:
    """Synchronous helper: shallow-clone `project` into a fresh tempdir.

    Authenticates by injecting the token into the clone URL via the
    `oauth2:<token>@host` form, which is the GitLab-recommended pattern
    for both PAT and OAuth tokens (see GitLab docs §"Clone using a token").
    Project access tokens use the same `oauth2:` prefix server-side.

    The connector's `close()` is responsible for rmtree-ing the directory;
    on failure we rmtree here so a half-cloned dir does not leak.
    """
    path_with_ns = project["path_with_namespace"]
    base_url = connector.base_url
    parsed = urllib.parse.urlparse(base_url)
    # Construct an authenticated clone URL. We do NOT log this URL —
    # printing it would leak the token into ops logs.
    auth_netloc = f"oauth2:{connector.token}@{parsed.netloc}"
    clone_url = urllib.parse.urlunparse(
        parsed._replace(netloc=auth_netloc, path=f"/{path_with_ns}.git")
    )
    tmp = Path(tempfile.mkdtemp(prefix="pleno-gl-"))
    try:
        cmd: list[str] = [
            "git",
            "clone",
            "--quiet",
            "--depth=1",
            clone_url,
            str(tmp),
        ]
        # `stderr=PIPE` so a clone failure surfaces the git error in the
        # CalledProcessError, but `stdout=DEVNULL` because git prints
        # progress lines we do not want polluting our scan logs.
        subprocess.run(
            cmd,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        return tmp
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


# ---------------------------------------------------------------------
# Factory + Spec
# ---------------------------------------------------------------------


def _factory(config: Mapping[str, Any]) -> GitlabConnector:
    """Build a connector from a plain config mapping.

    Mirrors the github-app factory: the credential is passed under
    `_credential` by the scheduler after CredentialBroker resolution.
    """
    cred_obj = config.get("_credential")
    if not isinstance(cred_obj, Credential):
        raise ValueError(
            "gitlab factory requires a resolved Credential under "
            "config['_credential'] (set by the scheduler from CredentialBroker)"
        )
    return GitlabConnector(
        GitlabConfig(
            project=str(config["project"]) if config.get("project") is not None else None,
            group=str(config["group"]) if config.get("group") is not None else None,
            base_url=str(config.get("base_url", DEFAULT_BASE_URL)),
            include_archived=bool(config.get("include_archived", False)),
            visibility=(
                str(config["visibility"]) if config.get("visibility") is not None else None
            ),
            ca_bundle_path=(
                str(config["ca_bundle_path"])
                if config.get("ca_bundle_path") is not None
                else None
            ),
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
    required_scopes=("read_api", "read_repository"),
    description=(
        "GitLab connector. SaaS gitlab.com + self-managed CE/EE, three "
        "auth modes (PAT/OAuth/project token), recursive group + "
        "subgroup enumeration via REST, shallow clone for content scan, "
        "self-managed CA bundle support. ADR-0007 §13."
    ),
)


__all__ = [
    "KIND",
    "SPEC",
    "GitlabConfig",
    "GitlabConnector",
]
