"""BitbucketConnector — Cloud + Server (Data Center) `SourceConnector`.

Single connector kind (`bitbucket`) backed by two REST flavors selected
at construction time. The wire-level differences (paginator shape, URL
prefix, archived/public filter semantics) live inside this module; the
`SourceConnector` contract the scheduler sees is identical to every
other git-host connector in the workspace.

Repository content is scanned via `git clone --depth=1` — we mirror the
builtin `github` connector instead of inventing a Bitbucket-specific
blob walker, so:

* the same ContentExtractor pipeline (#8) handles every git host
  uniformly,
* operator git-credential-helper hooks (e.g. SSO-issued ephemeral
  OAuth tokens) keep working without us re-implementing them,
* shallow clone semantics match what the builtin scanner already
  exercises in production.

The clone shell-out and the workspace/project enumeration call are
both injectable via `clone_fn` / `enumerate_fn` so the test suite is
hermetic — no real `git`, no real network.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

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
from pleno_pii_scanner.sources.builtin.dir_source import DirConfig, DirConnector
from pleno_pii_scanner.sources.registry import ConnectorSpec
from pleno_pii_scanner_bitbucket.api import (
    DEFAULT_CLOUD_BASE_URL,
    AuthMode,
    BasicAuth,
    BearerAuth,
    BitbucketApi,
    Flavor,
)


# Connector kind exported via the `pleno_pii_scanner.connectors` entry
# point group (see pyproject.toml). One kind covers both flavors; the
# wire flavor is selected by config.
KIND = "bitbucket"


# Test seams. `CloneFn` takes a clone URL + destination Path and is
# expected to populate that directory with the repo contents. `EnumerateFn`
# takes the configured api + workspace/project key + filter knobs and
# yields `(slug, clone_url)` pairs. Both default to the production
# implementations at the bottom of this module.
CloneFn = Callable[[str, Path], None]
EnumerateFn = Callable[
    ["BitbucketConnector", "BitbucketConfig"],
    Awaitable[list[tuple[str, str]]],
]


@dataclass(frozen=True, slots=True)
class BitbucketConfig:
    """Construction config for `BitbucketConnector`.

    `flavor` selects the wire protocol. `workspace` is required for
    Cloud, `project` for Server (the ADR §13 entry calls these
    "workspace" and "project" enumeration). A single repo is
    addressable by setting `repo_slug` instead.

    `include_archived` toggles whether archived/inactive repositories
    pass the discover filter. Bitbucket Cloud has no archive concept
    (the closest equivalent is `is_private` — see
    `include_public=False` for that), Server uses `archived=true` on
    each repo metadata blob.

    `ca_bundle_path` is honored only for Server installs behind a
    private CA. Cloud (api.bitbucket.org) uses public Mozilla certs.
    """

    flavor: Flavor
    workspace: str | None = None  # required when flavor=cloud and project unset
    project: str | None = None    # required when flavor=server and repo_slug unset
    repo_slug: str | None = None  # `workspace/repo` (cloud) or `PROJECT/repo` (server)
    base_url: str | None = None   # default api.bitbucket.org/2.0 for cloud
    include_archived: bool = False
    include_public: bool = True
    ca_bundle_path: str | None = None
    depth: int = 1
    id: str | None = None

    def __post_init__(self) -> None:
        if self.flavor not in ("cloud", "server"):
            raise ValueError(
                f"BitbucketConfig.flavor must be 'cloud' or 'server'; "
                f"got {self.flavor!r}"
            )
        if self.flavor == "cloud":
            if self.workspace is None and self.repo_slug is None:
                raise ValueError(
                    "BitbucketConfig(flavor='cloud') requires `workspace` or "
                    "`repo_slug`"
                )
        else:
            if self.project is None and self.repo_slug is None:
                raise ValueError(
                    "BitbucketConfig(flavor='server') requires `project` or "
                    "`repo_slug` and `base_url`"
                )
            if self.base_url is None:
                raise ValueError(
                    "BitbucketConfig(flavor='server') requires `base_url` "
                    "(self-hosted Bitbucket Server has no default endpoint)"
                )
        if self.depth < 1:
            raise ValueError("depth must be >= 1")

    def resolved_base_url(self) -> str:
        if self.base_url is not None:
            return _normalise_base_url(self.flavor, self.base_url)
        # cloud-only fall-through (server raises in __post_init__).
        return DEFAULT_CLOUD_BASE_URL

    def resolved_id(self) -> str:
        if self.id is not None:
            return self.id
        if self.repo_slug is not None:
            return f"bitbucket-{self.flavor}:{self.repo_slug}"
        if self.flavor == "cloud":
            assert self.workspace is not None
            return f"bitbucket-cloud:{self.workspace}"
        assert self.project is not None
        return f"bitbucket-server:{self.project}"


class BitbucketConnector:
    """`SourceConnector` for Bitbucket Cloud + Bitbucket Server.

    Owns one `BitbucketApi` (HTTP session) for repository enumeration
    and lazily-managed shallow git clones for content scanning. The
    clones live until `close()` so a multi-fetch run does not re-clone
    the same repo per file.
    """

    kind = KIND

    def __init__(
        self,
        config: BitbucketConfig,
        credential: Credential,
        *,
        transport: "Any | None" = None,
        clone_fn: CloneFn | None = None,
        enumerate_fn: EnumerateFn | None = None,
        sleep: "Any | None" = None,
    ) -> None:
        self._config = config
        self.id = config.resolved_id()
        self._credential = credential
        # Validate auth shape upfront so a misconfigured profile fails
        # at construction rather than mid-discover. Cloud accepts basic
        # (username + app_password) or Bearer; Server accepts Bearer
        # (HTTP access token) or basic (PAT/password).
        self._auth: AuthMode = _build_auth(config.flavor, credential)
        self._api = BitbucketApi(
            flavor=config.flavor,
            base_url=config.resolved_base_url(),
            auth=self._auth,
            transport=transport,
            ca_bundle_path=config.ca_bundle_path,
            sleep=sleep,
        )
        # Test seams: production defaults shell out to git for cloning
        # and walk the configured target via the api wrapper for
        # enumeration. Tests inject doubles to avoid real I/O.
        self._clone_fn: CloneFn = clone_fn or _clone_into_tempdir
        self._enumerate_fn: EnumerateFn = enumerate_fn or _default_enumerate
        # slug -> on-disk clone path. Populated lazily in discover().
        self._clones: dict[str, Path] = {}
        self._tempdirs: list[Path] = []
        self._lock = asyncio.Lock()

    @property
    def api(self) -> BitbucketApi:
        # Exposed so the default enumerate function can reuse the same
        # http client + auth. Tests can also poke at it for assertions.
        return self._api

    @property
    def config(self) -> BitbucketConfig:
        return self._config

    def capabilities(self) -> Capabilities:
        # `incremental=False` because we shallow-clone HEAD per run;
        # there is no per-blob ETag to short-circuit on. The dedicated
        # `git` connector is the right fit for incremental history scan.
        return Capabilities(
            incremental=False,
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
        """Enumerate every repository under the configured target.

        For workspace/project targets we page through the appropriate
        REST endpoint (Cloud `/repositories/{ws}` or Server
        `/projects/{key}/repos`). For a single-repo target we yield one
        synthetic `(slug, clone_url)` tuple. Each repo is shallow-cloned
        on first reference and walked via `DirConnector` so the same
        ContentExtractor pipeline as every other git-host connector
        applies.
        """
        del cursor  # incremental=False; cursor is unused for now
        repos = await self._enumerate_fn(self, self._config)
        for slug, clone_url in repos:
            repo_path = await self._ensure_clone(slug, clone_url)
            inner = DirConnector(
                DirConfig(root=repo_path, id=f"bitbucket:{slug}")
            )
            try:
                async for inner_ref in inner.discover(filter, None):
                    yield self._wrap_ref(inner_ref, slug)
            finally:
                await inner.close()

    async def fetch(
        self,
        ref: DocumentRef,
    ) -> AsyncIterator[Document | DocumentChunk]:
        """Re-emit a `DirConnector` fetch with our outer ref identity."""
        slug = ref.metadata.get("slug")
        if slug is None or slug not in self._clones:
            # Either the ref came from a different connector or
            # discover() never ran. Mirror the builtin `github`
            # connector's silent-empty idiom rather than raising.
            return
        repo_path = self._clones[slug]
        inner = DirConnector(DirConfig(root=repo_path, id=f"bitbucket:{slug}"))
        try:
            inner_ref = self._unwrap_ref(ref, slug)
            async for doc in inner.fetch(inner_ref):
                # DirConnector advertises streaming=False — only emits
                # `Document`. Re-wrap so finding paths report the
                # `<slug>/<path>` form, not the local clone path.
                assert isinstance(doc, Document)
                yield Document(
                    ref=ref,
                    text=doc.text,
                    binary=doc.binary,
                    fetched_at=doc.fetched_at,
                    content_hash=doc.content_hash,
                    created_by=doc.created_by,
                    extra=doc.extra,
                )
        finally:
            await inner.close()

    async def close(self) -> None:
        """Release the http client + rmtree every shallow clone.

        Best-effort tempdir cleanup: rmtree errors are swallowed
        because the cleaner is housekeeping. Raising in `close()` would
        mask whatever error caused close() to be invoked from a
        finally clause — the `finally` is the right place to do this.
        """
        async with self._lock:
            for path in self._tempdirs:
                await asyncio.to_thread(
                    shutil.rmtree, path, ignore_errors=True
                )
            self._tempdirs.clear()
            self._clones.clear()
        await self._api.aclose()

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    async def _ensure_clone(self, slug: str, clone_url: str) -> Path:
        async with self._lock:
            cached = self._clones.get(slug)
            if cached is not None:
                return cached
        # Run the clone outside the lock so concurrent tenants do not
        # serialize on each other's shell-outs. The race-window risk
        # (two coroutines cloning the same slug) is acceptable: the
        # second coroutine's tempdir is rmtree'd in close().
        embedded = _embed_credentials(clone_url, self._auth)
        path = await asyncio.to_thread(self._clone_fn, embedded, _new_tempdir())
        async with self._lock:
            self._clones[slug] = path
            self._tempdirs.append(path)
        return path

    def _wrap_ref(self, inner: DocumentRef, slug: str) -> DocumentRef:
        # Re-emit with our connector identity + the slug so fetch can
        # find the right clone. parent_chain keeps the `bitbucket://`
        # provenance for findings dashboards.
        return DocumentRef(
            source_id=self.id,
            source_kind=self.kind,
            path=f"{slug}/{inner.path}",
            native_url=_browse_url(self._config, slug, inner.path),
            parent_chain=(f"bitbucket://{slug}",),
            content_type=inner.content_type,
            size=inner.size,
            etag=inner.etag,
            last_modified=inner.last_modified,
            metadata={
                **inner.metadata,
                "slug": slug,
                "inner_path": inner.path,
                "flavor": self._config.flavor,
            },
        )

    def _unwrap_ref(self, ref: DocumentRef, slug: str) -> DocumentRef:
        inner_path = ref.metadata["inner_path"]
        return DocumentRef(
            source_id=f"bitbucket:{slug}",
            source_kind="dir",
            path=inner_path,
            content_type=ref.content_type,
            size=ref.size,
            etag=ref.etag,
            last_modified=ref.last_modified,
        )


# ---------------------------------------------------------------------
# Default enumerate
# ---------------------------------------------------------------------


async def _default_enumerate(
    connector: BitbucketConnector,
    config: BitbucketConfig,
) -> list[tuple[str, str]]:
    """Production enumerate: page repos via REST + harvest clone URLs.

    Returns `[(slug, clone_url), ...]`. The clone URL is HTTPS (never
    SSH) because the connector authenticates with username/password or
    a Bearer token, both of which only work over HTTPS.
    """
    if config.repo_slug is not None:
        # Single-repo path: synthesize the URL from the slug. Cloud
        # uses `bitbucket.org/<slug>.git`; Server uses
        # `<host>/scm/<project>/<repo>.git` where the host is derived
        # from base_url (stripping `/rest/api/1.0`).
        return [(config.repo_slug, _single_repo_clone_url(config))]

    out: list[tuple[str, str]] = []
    if config.flavor == "cloud":
        async for repo in connector.api.paginate(
            f"/repositories/{config.workspace}",
        ):
            if not config.include_archived and repo.get("is_archived"):
                continue
            if not config.include_public and not repo.get("is_private", True):
                continue
            slug = repo.get("full_name") or (
                f"{config.workspace}/{repo.get('slug')}" if repo.get("slug") else None
            )
            clone_url = _pick_cloud_clone_url(repo)
            if slug is None or clone_url is None:
                # Defensive: skip malformed entries rather than crashing
                # the whole enumeration. Bitbucket's response schema is
                # stable but third-party Bitbucket-compatible servers
                # (e.g. bitbucket-server-emulator) are not.
                continue
            out.append((slug, clone_url))
    else:
        assert config.project is not None
        async for repo in connector.api.paginate(
            f"/projects/{config.project}/repos",
        ):
            if not config.include_archived and repo.get("archived"):
                continue
            if not config.include_public and repo.get("public"):
                continue
            slug = repo.get("slug")
            clone_url = _pick_server_clone_url(repo)
            if slug is None or clone_url is None:
                continue
            out.append((f"{config.project}/{slug}", clone_url))
    return out


# ---------------------------------------------------------------------
# Auth + URL helpers
# ---------------------------------------------------------------------


def _build_auth(flavor: Flavor, credential: Credential) -> AuthMode:
    """Validate the credential payload shape per flavor.

    Cloud: prefers Bearer (workspace access token), falls back to
    basic (username + app_password). Server: same priority but the
    Bearer is an "HTTP access token" and the basic option is a
    PAT/password pair.
    """
    payload = credential.payload
    token = payload.get("access_token") or payload.get("token")
    if isinstance(token, str) and token:
        return BearerAuth(token=token)
    username = payload.get("username")
    password = (
        payload.get("app_password")
        if flavor == "cloud"
        else payload.get("password")
    )
    if isinstance(username, str) and isinstance(password, str) and username and password:
        return BasicAuth(username=username, password=password)
    raise ValueError(
        f"bitbucket-{flavor} credential.payload requires either "
        f"`access_token`/`token` (Bearer) or "
        f"`username` + `{'app_password' if flavor == 'cloud' else 'password'}`"
    )


def _embed_credentials(clone_url: str, auth: AuthMode) -> str:
    """Inject the auth into the HTTPS clone URL for git's basic prompt.

    Git supports `https://user:pass@host/path` for basic auth and
    `https://x-token-auth:<token>@host/path` for Cloud workspace
    tokens. We use both — the alternative is configuring a per-tenant
    `.gitconfig` credential helper which is awkward in tests and
    doesn't compose with multiple tenants in one run. The embedded
    URL only lives in the in-memory subprocess argv; we never write it
    to disk and the connector's `close()` rmtree's the clone.
    """
    parsed = urlparse(clone_url)
    if parsed.scheme not in ("https", "http"):
        # SSH or local URL — pass through unmodified. The default
        # enumerate path always returns HTTPS so this branch only
        # fires when callers inject custom URLs via `enumerate_fn`.
        return clone_url
    if isinstance(auth, BearerAuth):
        userinfo = f"x-token-auth:{quote(auth.token, safe='')}"
    else:
        userinfo = (
            f"{quote(auth.username, safe='')}:{quote(auth.password, safe='')}"
        )
    netloc = parsed.netloc
    # Strip any pre-existing userinfo on the URL — Bitbucket's clone
    # URLs sometimes carry one (`bitbucket-server@host/...`) and we
    # do not want to double-up.
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[-1]
    return parsed._replace(netloc=f"{userinfo}@{netloc}").geturl()


def _pick_cloud_clone_url(repo: Mapping[str, Any]) -> str | None:
    """Pull the HTTPS clone URL out of a Cloud `repository` payload."""
    links = repo.get("links") or {}
    clones = links.get("clone") or []
    for entry in clones:
        if entry.get("name") == "https":
            href = entry.get("href")
            if isinstance(href, str):
                return href
    return None


def _pick_server_clone_url(repo: Mapping[str, Any]) -> str | None:
    """Pull the HTTP(S) clone URL out of a Server `repository` payload."""
    links = repo.get("links") or {}
    clones = links.get("clone") or []
    for entry in clones:
        if entry.get("name") in ("http", "https"):
            href = entry.get("href")
            if isinstance(href, str):
                return href
    return None


def _single_repo_clone_url(config: BitbucketConfig) -> str:
    """Synthesize an HTTPS clone URL for the `repo_slug` shortcut."""
    assert config.repo_slug is not None
    if config.flavor == "cloud":
        return f"https://bitbucket.org/{config.repo_slug}.git"
    # Server: derive the host from base_url. base_url looks like
    # `https://bitbucket.acme.internal/rest/api/1.0`; strip the
    # `/rest/api/1.0` suffix to get the SCM root.
    assert config.base_url is not None
    host = config.base_url.rstrip("/")
    if host.endswith("/rest/api/1.0"):
        host = host[: -len("/rest/api/1.0")]
    # repo_slug for Server takes the form `PROJECT/repo`.
    project, _, repo = config.repo_slug.partition("/")
    return f"{host}/scm/{project.lower()}/{repo}.git"


def _browse_url(
    config: BitbucketConfig, slug: str, inner_path: str
) -> str | None:
    """Render a human-clickable browse URL for findings dashboards."""
    if config.flavor == "cloud":
        return f"https://bitbucket.org/{slug}/src/HEAD/{inner_path}"
    assert config.base_url is not None
    host = config.base_url.rstrip("/")
    if host.endswith("/rest/api/1.0"):
        host = host[: -len("/rest/api/1.0")]
    project, _, repo = slug.partition("/")
    return f"{host}/projects/{project}/repos/{repo}/browse/{inner_path}"


def _normalise_base_url(flavor: Flavor, base_url: str) -> str:
    """Drop trailing slash and append the API prefix when missing.

    Operators tend to paste the host root (`https://bitbucket.acme/`)
    and forget the `/rest/api/1.0` suffix; this fix-up keeps the rest
    of the module honest about always having the prefix on hand for
    the URL helpers.
    """
    base = base_url.rstrip("/")
    if flavor == "cloud":
        if not base.endswith("/2.0"):
            return base + "/2.0"
        return base
    if not base.endswith("/rest/api/1.0"):
        return base + "/rest/api/1.0"
    return base


def _new_tempdir() -> Path:
    """Centralised tempdir factory so tests can monkey-patch one place."""
    return Path(tempfile.mkdtemp(prefix="pleno-bb-"))


def _clone_into_tempdir(clone_url: str, dest: Path) -> Path:
    """Synchronous helper: shallow-clone `clone_url` into `dest`.

    Returns the cloned root. The connector's `close()` is responsible
    for rmtree-ing the directory; on any clone failure here we rmtree
    immediately so a half-populated dir does not survive.
    """
    try:
        cmd = [
            "git",
            "clone",
            "--quiet",
            "--depth=1",
            clone_url,
            str(dest),
        ]
        subprocess.run(
            cmd,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        return dest
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise


# ---------------------------------------------------------------------
# Factory + Spec
# ---------------------------------------------------------------------


def _factory(config: Mapping[str, Any]) -> SourceConnector:
    """Build a connector from a plain config mapping.

    The credential is fetched separately (CredentialBroker) and threaded
    through under `_credential` by the scheduler, mirroring the
    github-app factory contract.
    """
    cred_obj = config.get("_credential")
    if not isinstance(cred_obj, Credential):
        raise ValueError(
            "bitbucket factory requires a resolved Credential under "
            "config['_credential'] (set by the scheduler from CredentialBroker)"
        )
    flavor_raw = config.get("flavor", "cloud")
    if flavor_raw not in ("cloud", "server"):
        raise ValueError(
            f"bitbucket connector config['flavor'] must be 'cloud' or "
            f"'server'; got {flavor_raw!r}"
        )
    return BitbucketConnector(
        BitbucketConfig(
            flavor=flavor_raw,
            workspace=(
                str(config["workspace"])
                if config.get("workspace") is not None
                else None
            ),
            project=(
                str(config["project"])
                if config.get("project") is not None
                else None
            ),
            repo_slug=(
                str(config["repo_slug"])
                if config.get("repo_slug") is not None
                else None
            ),
            base_url=(
                str(config["base_url"])
                if config.get("base_url") is not None
                else None
            ),
            include_archived=bool(config.get("include_archived", False)),
            include_public=bool(config.get("include_public", True)),
            ca_bundle_path=(
                str(config["ca_bundle_path"])
                if config.get("ca_bundle_path") is not None
                else None
            ),
            depth=int(config.get("depth", 1)),
            id=str(config["id"]) if config.get("id") is not None else None,
        ),
        credential=cred_obj,
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
    required_scopes=("repository:read",),
    description=(
        "Bitbucket Cloud + Bitbucket Server / Data Center connector. "
        "Single kind, two wire flavors (`flavor=cloud|server`). "
        "Workspace/project enumeration with archived + public filters; "
        "shallow git clone for content scan; 429 backoff via Retry-After; "
        "self-signed CA support for Server installs (ca_bundle_path). "
        "ADR-0007 §13."
    ),
)


__all__ = [
    "KIND",
    "SPEC",
    "BitbucketConfig",
    "BitbucketConnector",
    "CloneFn",
    "EnumerateFn",
]
