"""AzureDevOpsConnector — `SourceConnector` for ADO Services + Server.

ADR-0007 §13. Mirrors the GitHub connector's split: enumeration via
REST (paginated by ``x-ms-continuationtoken`` header), repo content
via shallow `git clone --depth=1` shelled to the system git binary.

We deliberately do NOT walk the Git Items REST API for content the way
the GitHub-App connector walks `git/trees`. Two reasons specific to
Azure DevOps:

  * The Items API is rate-limited per-call (1 TSTU / blob), and a
    medium repo with 10k blobs blows the org quota in one scan.
  * The Items endpoint has no batched form analogous to GitHub's
    `git/trees?recursive=1`; you'd issue one call per blob.

A shallow clone is one HTTPS upload-pack negotiation regardless of
repo size and works under PAT (basic auth, libgit2 understands it),
OAuth bearer (`http.extraheader=Authorization: Bearer ...`), and
federated bearer (same as OAuth).
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from pleno_pii_scanner.sources.base import (
    Capabilities,
    Cursor,
    Document,
    DocumentChunk,  # noqa: F401 — kept in fetch() return-type annotation
    DocumentRef,
    SourceConnector,
    SourceFilter,
)
from pleno_pii_scanner.sources.builtin.dir_source import DirConfig, DirConnector
from pleno_pii_scanner.sources.registry import ConnectorSpec

from pleno_pii_scanner_azure_devops.api import (
    DEFAULT_API_VERSION,
    SERVICES_DEFAULT_HOST,
    AzureDevOpsApi,
)
from pleno_pii_scanner_azure_devops.auth import (
    AzureDevOpsAuth,
    FederatedConfig,
)


# Connector kind (entry-point key, snake-case). Picked so it cannot
# collide with the builtin `git` (zero-deps) or `github` connectors.
KIND = "azure_devops"


# Test seams. `clone_fn` accepts `(clone_url, AzureDevOpsConfig,
# auth_header)` and returns the local clone root. `enumerate_fn` lets
# tests replace the project + repo enumeration paths wholesale (used
# in connector-level integration tests where mocking httpx would
# triple the test surface for no value).
CloneFn = Callable[[str, "AzureDevOpsConfig", str], Path]
EnumerateFn = Callable[
    ["AzureDevOpsConnector", SourceFilter, Cursor | None],
    AsyncIterator[tuple[str, str, str, bool]],  # (project, repo, clone_url, disabled)
]


@dataclass(frozen=True, slots=True)
class AzureDevOpsConfig:
    """Construction config for `AzureDevOpsConnector`.

    `flavor` picks Services vs. Server semantics. For Services you
    supply `organization` (e.g. "contoso") and we derive the base URL
    `https://dev.azure.com/contoso`. For Server you supply `base_url`
    pointing at the on-prem collection
    (e.g. `https://tfs.example.internal/DefaultCollection`) and may
    optionally supply `ca_bundle_path` for the install's private CA.

    `project` filters discovery to a single project; `None` enumerates
    every project the credential can see.

    `include_disabled` controls whether `isDisabled=true` repos are
    yielded. The default `False` matches the operator expectation that
    archived/disabled repos are excluded from active scans (this aligns
    with the github-app connector's `include_archived=False` default).
    """

    flavor: str = "services"
    organization: str | None = None
    base_url: str | None = None
    project: str | None = None
    include_disabled: bool = False
    include_private: bool = True
    ca_bundle_path: Path | None = None
    api_version: str = DEFAULT_API_VERSION
    id: str | None = None

    def __post_init__(self) -> None:
        if self.flavor not in ("services", "server"):
            raise ValueError(
                f"flavor must be 'services' or 'server'; got {self.flavor!r}"
            )
        if self.flavor == "services":
            # Services REQUIRES `organization`. `base_url` is derived;
            # passing both is allowed (operators pinning the URL for an
            # MSEE preview) but at least one of the two paths must
            # resolve to a non-empty URL.
            if not self.organization and not self.base_url:
                raise ValueError(
                    "flavor='services' requires `organization` "
                    "(e.g. 'contoso') or an explicit `base_url`"
                )
        else:
            # Server REQUIRES base_url; deriving it from a hostname is
            # not possible (collection path varies per install).
            if not self.base_url:
                raise ValueError(
                    "flavor='server' requires explicit `base_url` (collection URL)"
                )

    def resolved_base_url(self) -> str:
        """Join the flavor + organization into the REST root URL."""
        if self.base_url:
            return self.base_url.rstrip("/")
        # Services + organization-only path.
        return f"{SERVICES_DEFAULT_HOST}/{self.organization}"

    def resolved_id(self) -> str:
        if self.id is not None:
            return self.id
        if self.flavor == "server":
            return f"azure-devops-server:{self.resolved_base_url()}"
        target = self.organization or self.resolved_base_url()
        return f"azure-devops:{target}"


class AzureDevOpsConnector:
    """`SourceConnector` for Azure DevOps repos (Services + Server)."""

    kind = KIND

    def __init__(
        self,
        config: AzureDevOpsConfig,
        auth: AzureDevOpsAuth,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clone_fn: CloneFn | None = None,
        enumerate_fn: EnumerateFn | None = None,
    ) -> None:
        self._config = config
        self.id = config.resolved_id()
        self._auth = auth
        self._api = AzureDevOpsApi(
            base_url=config.resolved_base_url(),
            auth=auth,
            transport=transport,
            ca_bundle_path=config.ca_bundle_path,
            api_version=config.api_version,
        )
        self._clone_fn: CloneFn = clone_fn or _clone_into_tempdir
        self._enumerate_fn = enumerate_fn
        # repo-key (project/name) -> local clone path.
        self._clones: dict[str, Path] = {}
        self._tempdirs: list[Path] = []
        self._lock = asyncio.Lock()

    def capabilities(self) -> Capabilities:
        return Capabilities(
            incremental=False,
            binary=False,
            content_hash_delta=False,
            max_concurrent_fetches=4,
            streaming=False,
        )

    # --- discover / fetch ----------------------------------------------

    async def discover(
        self,
        filter: SourceFilter,
        cursor: Cursor | None,
    ) -> AsyncIterator[DocumentRef]:
        """Walk projects → repos → file refs.

        `cursor` is the projects-page continuation token; we round-trip
        it through `DocumentRef.metadata['_cursor']` so the scheduler
        can checkpoint mid-org.
        """
        enumerate_iter = (
            self._enumerate_fn(self, filter, cursor)
            if self._enumerate_fn is not None
            else self._enumerate_repos(filter, cursor)
        )
        async for project, repo_name, clone_url, disabled in enumerate_iter:
            if disabled and not self._config.include_disabled:
                continue
            slug = f"{project}/{repo_name}"
            repo_path = await self._ensure_clone(slug, clone_url)
            inner = DirConnector(DirConfig(root=repo_path, id=f"azure-devops:{slug}"))
            try:
                async for inner_ref in inner.discover(filter, None):
                    yield self._wrap_ref(inner_ref, project, repo_name)
            finally:
                await inner.close()

    async def fetch(
        self,
        ref: DocumentRef,
    ) -> AsyncIterator[Document | DocumentChunk]:
        slug = ref.metadata.get("slug")
        inner_path = ref.metadata.get("inner_path")
        if slug is None or inner_path is None or slug not in self._clones:
            # Stale ref (checkpoint reload across runs) or wrong
            # connector. Same idiom as GitHub builtin: yield nothing.
            return
        repo_path = self._clones[slug]
        inner = DirConnector(DirConfig(root=repo_path, id=f"azure-devops:{slug}"))
        try:
            inner_ref = self._unwrap_ref(ref, slug, inner_path)
            async for doc in inner.fetch(inner_ref):
                assert isinstance(doc, Document)
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

    async def close(self) -> None:
        """Tear down clones + the HTTP client.

        Errors during rmtree are intentionally swallowed: the worst
        case is a leftover tempdir under /tmp/pleno-ado-*, which is
        annoying but not a correctness issue, and raising here would
        mask whatever error invoked `close()` from a finally clause.
        """
        async with self._lock:
            for path in self._tempdirs:
                await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)
            self._tempdirs.clear()
            self._clones.clear()
        await self._api.aclose()

    # --- internals ------------------------------------------------------

    async def _enumerate_repos(
        self,
        filter: SourceFilter,
        cursor: Cursor | None,
    ) -> AsyncIterator[tuple[str, str, str, bool]]:
        """Yield ``(project, repo, clone_url, disabled)`` for the target.

        Cursor handling: the projects endpoint paginates via
        `x-ms-continuationtoken`. We expose only the *projects* cursor
        because per-project repo enumeration is bounded (Azure caps
        repos-per-project at ~100 in practice; the API does not paginate
        the response).
        """
        del filter  # filter applies inside DirConnector; nothing to do here
        if self._config.project is not None:
            # Single-project shortcut: skip the projects page entirely.
            async for repo in self._iter_project_repos(self._config.project):
                yield repo
            return
        async for project_name, _next_cursor in self._iter_projects(cursor):
            async for repo in self._iter_project_repos(project_name):
                yield repo

    async def _iter_projects(
        self, start_cursor: Cursor | None
    ) -> AsyncIterator[tuple[str, str | None]]:
        """Page through `_apis/projects`. Yields ``(name, next_cursor)``.

        The `next_cursor` is the continuation token observed *after*
        emitting the page's projects, so the scheduler can checkpoint
        between pages without losing any project. The first page sets
        `next_cursor=start_cursor` so a resume from a stored cursor is
        consistent with a fresh start.
        """
        params: dict[str, Any] = {}
        if start_cursor:
            params["continuationToken"] = start_cursor
        async for response in self._api.get_paginated("/_apis/projects", params=params):
            payload = response.json()
            next_cursor = response.headers.get("x-ms-continuationtoken") or None
            for project in payload.get("value", []):
                name = project.get("name")
                if not name:
                    # Defensive: a malformed project entry without a
                    # `name` would cause `_apis/git/repositories` to
                    # 404 down the line. Skip rather than blow up.
                    continue
                visibility = project.get("visibility")
                if not self._config.include_private and visibility == "private":
                    continue
                yield name, next_cursor

    async def _iter_project_repos(
        self, project: str
    ) -> AsyncIterator[tuple[str, str, str, bool]]:
        """Yield repos for one project as ``(project, repo, clone_url, disabled)``."""
        response = await self._api.get(f"/{project}/_apis/git/repositories")
        if response.status_code == 404:
            # Project was deleted between projects-list and repos-list
            # (race) or the credential cannot see it. Either way, skip
            # silently — a single 404 must not abort the whole scan.
            return
        if response.status_code != 200:
            # Any other non-success here is a real problem (auth, 5xx);
            # surface as ApiError so the scheduler retries / alerts.
            from pleno_pii_scanner_azure_devops.api import AzureDevOpsApiError

            raise AzureDevOpsApiError(
                f"unexpected status {response.status_code} listing repos for "
                f"project {project!r}"
            )
        payload = response.json()
        for repo in payload.get("value", []):
            name = repo.get("name")
            clone_url = repo.get("remoteUrl") or repo.get("webUrl") or ""
            if not name or not clone_url:
                continue
            yield (
                project,
                name,
                clone_url,
                bool(repo.get("isDisabled", False)),
            )

    async def _ensure_clone(self, slug: str, clone_url: str) -> Path:
        """Clone (or reuse cached clone of) one repo.

        The lock guards the cache + tempdir registry; the actual
        subprocess runs outside the lock so two repos can clone in
        parallel — only the bookkeeping is serialized.
        """
        async with self._lock:
            cached = self._clones.get(slug)
            if cached is not None:
                return cached
        auth_header = await self._auth.authorization_header()
        path = await asyncio.to_thread(
            self._clone_fn, clone_url, self._config, auth_header
        )
        async with self._lock:
            existing = self._clones.get(slug)
            if existing is not None:
                # Lost the race; the loser cleans up its own tempdir
                # and uses the cached one. Avoids two clones of the
                # same repo when discover is invoked concurrently.
                await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)
                return existing
            self._clones[slug] = path
            self._tempdirs.append(path)
        return path

    def _wrap_ref(self, inner: DocumentRef, project: str, repo: str) -> DocumentRef:
        slug = f"{project}/{repo}"
        return DocumentRef(
            source_id=self.id,
            source_kind=self.kind,
            path=f"{slug}/{inner.path}",
            native_url=(
                f"{self._config.resolved_base_url()}/{project}/_git/"
                f"{repo}?path={inner.path}"
            ),
            parent_chain=(f"azure-devops://{slug}",),
            content_type=inner.content_type,
            size=inner.size,
            etag=inner.etag,
            last_modified=inner.last_modified,
            metadata={
                **inner.metadata,
                "slug": slug,
                "project": project,
                "repo": repo,
                "inner_path": inner.path,
            },
        )

    def _unwrap_ref(self, ref: DocumentRef, slug: str, inner_path: str) -> DocumentRef:
        return DocumentRef(
            source_id=f"azure-devops:{slug}",
            source_kind="dir",
            path=inner_path,
            content_type=ref.content_type,
            size=ref.size,
            etag=ref.etag,
            last_modified=ref.last_modified,
        )


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _clone_into_tempdir(
    clone_url: str, config: AzureDevOpsConfig, auth_header: str
) -> Path:
    """Shallow-clone `clone_url` into a fresh tempdir using `git`.

    The auth header is passed via ``-c http.extraheader=`` rather than
    embedded in the URL so it does not appear in the parent process's
    `ps` output, in `git remote -v`, or in any reflog. The tempdir is
    rmtree'd by `AzureDevOpsConnector.close()`.
    """
    tmp = Path(tempfile.mkdtemp(prefix="pleno-ado-"))
    try:
        cmd: list[str] = [
            "git",
            "-c",
            f"http.extraheader=Authorization: {auth_header}",
            "clone",
            "--quiet",
            "--depth=1",
            clone_url,
            str(tmp),
        ]
        # If the operator gave us a private CA bundle, point libcurl at
        # it for this clone (the http.extraheader runs through the
        # same TLS channel as the API; we honor the same trust root).
        if config.ca_bundle_path is not None:
            cmd[1:1] = ["-c", f"http.sslCAInfo={config.ca_bundle_path}"]
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


def _build_auth(payload: Mapping[str, Any]) -> AzureDevOpsAuth:
    """Construct an `AzureDevOpsAuth` from a raw credential payload.

    Validates the mode and required keys upfront; downstream gets a
    typed `AzureDevOpsAuth` and never has to defend against a
    partially-specified credential dict.
    """
    mode = payload.get("mode")
    if mode == "pat":
        pat = payload.get("pat")
        if not isinstance(pat, str):
            raise ValueError("credential mode='pat' requires `pat` string")
        return AzureDevOpsAuth.pat(pat)
    if mode == "oauth":
        access_token = payload.get("access_token")
        if not isinstance(access_token, str):
            raise ValueError("credential mode='oauth' requires `access_token` string")
        return AzureDevOpsAuth.oauth(access_token)
    if mode == "federated":
        oidc_token_path = payload.get("oidc_token_path")
        tenant_id = payload.get("tenant_id")
        client_id = payload.get("client_id")
        if not (
            isinstance(oidc_token_path, (str, Path))
            and isinstance(tenant_id, str)
            and isinstance(client_id, str)
        ):
            raise ValueError(
                "credential mode='federated' requires `oidc_token_path`, "
                "`tenant_id`, `client_id`"
            )
        return AzureDevOpsAuth.federated(
            FederatedConfig(
                oidc_token_path=Path(oidc_token_path),
                tenant_id=tenant_id,
                client_id=client_id,
            )
        )
    raise ValueError(
        f"credential.mode must be 'pat' / 'oauth' / 'federated'; got {mode!r}"
    )


def _factory(config: Mapping[str, Any]) -> SourceConnector:
    """Build the connector from a registry-driven config dict.

    The credential payload arrives under `_credential` (a dict), the
    same convention pii-scanner-github uses. Direct test construction
    bypasses this by passing `AzureDevOpsAuth` to the class directly.
    """
    cred_payload = config.get("_credential")
    if not isinstance(cred_payload, Mapping):
        raise ValueError(
            "azure_devops factory requires config['_credential'] mapping "
            "(set by the scheduler from CredentialBroker)"
        )
    auth = _build_auth(cred_payload)
    ca_path_raw = config.get("ca_bundle_path")
    return AzureDevOpsConnector(
        AzureDevOpsConfig(
            flavor=str(config.get("flavor", "services")),
            organization=(
                str(config["organization"])
                if config.get("organization") is not None
                else None
            ),
            base_url=(
                str(config["base_url"]) if config.get("base_url") is not None else None
            ),
            project=(
                str(config["project"]) if config.get("project") is not None else None
            ),
            include_disabled=bool(config.get("include_disabled", False)),
            include_private=bool(config.get("include_private", True)),
            ca_bundle_path=Path(ca_path_raw) if ca_path_raw else None,
            api_version=str(config.get("api_version", DEFAULT_API_VERSION)),
            id=str(config["id"]) if config.get("id") is not None else None,
        ),
        auth=auth,
    )


SPEC = ConnectorSpec(
    kind=KIND,
    version="0.1.0",
    factory=_factory,
    capabilities=Capabilities(
        incremental=False,
        binary=False,
        content_hash_delta=False,
        max_concurrent_fetches=4,
    ),
    required_scopes=("vso.code", "vso.project"),
    description=(
        "Azure DevOps connector. Supports Services (dev.azure.com) and "
        "Server (on-prem TFS successor). Three auth modes: PAT (basic), "
        "OAuth bearer, federated workload identity (OIDC -> AAD token "
        "exchange). Project + repo enumeration via x-ms-continuationtoken "
        "header pagination; repo content via shallow git clone."
    ),
)


__all__ = [
    "KIND",
    "SPEC",
    "AzureDevOpsConfig",
    "AzureDevOpsConnector",
    "CloneFn",
    "EnumerateFn",
    "_build_auth",
    "_clone_into_tempdir",
]
