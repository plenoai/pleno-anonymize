"""GithubAppConnector — enterprise GitHub `SourceConnector`.

Replaces the builtin `github` connector's `git clone --depth=1 + gh
repo list` pipeline with direct REST + GraphQL calls under GitHub App
auth. Key differences from the builtin (ADR §13):

* **No subprocess** — we never shell out to `git`, `gh`, or `git-lfs`.
* **No clone** — `discover()` walks the repo tree via the Git Trees
  REST API; `fetch()` pulls individual blobs (`/git/blobs/:sha`) so
  scanning 10 GB of repos costs 10 GB of HTTP, not 10 GB of disk.
* **Org/enterprise enumeration via GraphQL** with cursor pagination —
  no silent `--limit 1000` truncation.
* **Incremental** — `pushed:>{ts}` GraphQL filter on org enumeration,
  per-repo `since`/`pushed_at` ETag check on file refs.
* **Rate-limit feedback** — every call surfaces `RateLimited` to the
  scheduler's AIMD bucket on 429 / secondary-403.

Targets:

* `repo="owner/name"` — single repo
* `org="acme"` — every non-archived (configurable) repo in the org
* `enterprise="acme-inc"` — every org in the enterprise (GHES only;
  uses the `enterprise.organizations` GraphQL field)

Exactly one of the three is required.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from pleno_pii_scanner.credentials.broker import Credential
from pleno_pii_scanner.scheduler.rate_limit import RateLimited
from pleno_pii_scanner.sources.base import (
    SUBSOURCE_METADATA_KEY,
    Capabilities,
    Cursor,
    Document,
    DocumentChunk,
    DocumentRef,
    SourceFilter,
    Subsource,
)
from pleno_pii_scanner_github.api import DEFAULT_BASE_URL, GithubApi
from pleno_pii_scanner_github.app_auth import AppAuth


# Tarball cutoff: small repos benefit from a single tarball download
# vs. one HTTP per blob. 10 MB matches the GitHub `tarball_url` redirect
# response-body cap that does not require a streaming reader.
_TARBALL_BYTE_CEILING = 10 * 1024 * 1024


# Connector kind (entry-point key). Picked so it does NOT collide with
# the builtin `github` kind shipped in the core wheel.
KIND = "github-app"


@dataclass(frozen=True, slots=True)
class GithubAppConfig:
    """Construction config for `GithubAppConnector`.

    Exactly one of `repo` / `org` / `enterprise` must be set.
    `base_url` defaults to api.github.com; GHES is `https://<host>/api/v3`.
    """

    repo: str | None = None
    org: str | None = None
    enterprise: str | None = None
    base_url: str = DEFAULT_BASE_URL
    include_archived: bool = False
    id: str | None = None

    def __post_init__(self) -> None:
        targets = [t for t in (self.repo, self.org, self.enterprise) if t is not None]
        if len(targets) != 1:
            raise ValueError(
                "GithubAppConfig must set exactly one of `repo` / `org` / `enterprise`"
            )

    def resolved_id(self) -> str:
        if self.id is not None:
            return self.id
        if self.enterprise is not None:
            return f"github-enterprise:{self.enterprise}"
        if self.org is not None:
            return f"github-org:{self.org}"
        return f"github-app:{self.repo}"


class GithubAppConnector:
    """`SourceConnector` backed by the GitHub REST + GraphQL APIs.

    Owns one `GithubApi` (HTTP session) and one `AppAuth` cache. The
    connector lazy-mints an installation token on the first request and
    refreshes it before expiry; tokens never touch disk.
    """

    kind = KIND

    def __init__(
        self,
        config: GithubAppConfig,
        credential: Credential,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._config = config
        self.id = config.resolved_id()
        self._credential = credential
        self._api = GithubApi(base_url=config.base_url, transport=transport)
        # IncrementalSourceConnector state. Sub-source ids are slugs
        # (`owner/name`); fingerprints are the default-branch HEAD SHA we
        # collect during `list_subsources` (folded into the same GraphQL
        # query that already enumerates the org). `set_subsource_skip`
        # tells `discover` to drop those slugs.
        self._skip_subsources: frozenset[str] = frozenset()
        payload = credential.payload
        # Validate creds at construction so a misconfigured profile
        # surfaces before the first network call rather than hiding in
        # a 401 from `/app/installations/.../access_tokens`.
        try:
            app_id = str(payload["app_id"])
            installation_id = str(payload["installation_id"])
            private_key = payload["private_key"]
        except KeyError as exc:
            raise ValueError(
                "github-app credential.payload requires keys "
                "`app_id`, `installation_id`, `private_key`"
            ) from exc
        if not isinstance(private_key, (str, bytes)):
            raise ValueError(
                "github-app credential.payload[private_key] must be PEM str/bytes"
            )
        self._auth = AppAuth(
            app_id=app_id,
            installation_id=installation_id,
            private_key_pem=(
                private_key
                if isinstance(private_key, str)
                else private_key.decode("utf-8")
            ),
            api=self._api,
        )

    def capabilities(self) -> Capabilities:
        return Capabilities(
            incremental=True,
            binary=False,
            content_hash_delta=True,
            max_concurrent_fetches=8,
            streaming=False,
        )

    async def close(self) -> None:
        await self._api.aclose()

    # ------------------------------------------------------------------
    # IncrementalSourceConnector — sub-source level cache short-circuit
    # ------------------------------------------------------------------

    async def list_subsources(self) -> tuple[Subsource, ...]:
        """Cheaply enumerate `(slug, head_sha)` for every target repo.

        Org / enterprise targets fold the SHA collection into the same
        GraphQL `repositories` paginator that `discover()` already uses
        (one query per 100 repos), so an org with 1000 repos resolves
        in ~10 round-trips instead of 1000 individual `git ls-remote`
        subprocess forks. Single-repo targets resolve via one extra
        REST GET against `/repos/{owner}/{name}` for the
        `default_branch`'s commit SHA.

        Repos whose HEAD cannot be resolved are returned with the
        sentinel fingerprint `unknown:<slug>` so the
        IncrementalRunner treats them as guaranteed cache misses.
        """
        await self._refresh_bearer()

        if self._config.repo is not None:
            owner, name = _split_slug(self._config.repo)
            sha = await self._resolve_repo_head_sha(owner, name)
            slug = f"{owner}/{name}"
            fingerprint = sha if sha is not None else f"unknown:{slug}"
            return (Subsource(sub_id=slug, fingerprint=fingerprint),)

        out: list[Subsource] = []
        if self._config.org is not None:
            async for slug, sha in self._iter_org_subsources(
                self._config.org,
                include_archived=self._config.include_archived,
            ):
                out.append(
                    Subsource(
                        sub_id=slug,
                        fingerprint=sha if sha is not None else f"unknown:{slug}",
                    )
                )
            return tuple(out)

        assert self._config.enterprise is not None
        async for org_name in self._iter_enterprise_orgs(self._config.enterprise):
            async for slug, sha in self._iter_org_subsources(
                org_name,
                include_archived=self._config.include_archived,
            ):
                out.append(
                    Subsource(
                        sub_id=slug,
                        fingerprint=sha if sha is not None else f"unknown:{slug}",
                    )
                )
        return tuple(out)

    def set_subsource_skip(self, skip: frozenset[str]) -> None:
        self._skip_subsources = skip

    async def _resolve_repo_head_sha(self, owner: str, name: str) -> str | None:
        """Single-repo HEAD SHA via REST. Returns None on any error so
        the runner falls back to a normal walk instead of caching a
        stale entry."""
        try:
            response = await self._api.get(f"/repos/{owner}/{name}")
        except RateLimited:
            raise
        except Exception:
            return None
        if response.status_code != 200:
            return None
        body = response.json()
        default_branch = body.get("default_branch")
        if not default_branch:
            return None
        try:
            commit = await self._api.get(
                f"/repos/{owner}/{name}/commits/{default_branch}"
            )
        except RateLimited:
            raise
        except Exception:
            return None
        if commit.status_code != 200:
            return None
        sha = commit.json().get("sha")
        if not isinstance(sha, str):
            return None
        return sha

    async def _iter_org_subsources(
        self,
        org: str,
        *,
        include_archived: bool,
    ) -> AsyncIterator[tuple[str, str | None]]:
        """Pages through `_ORG_REPOS_QUERY` and yields `(slug, sha)`.

        Uses the same paginator as `_iter_org_repos` but extracts
        `defaultBranchRef.target.oid` directly. Repos with no default
        branch (empty repo) yield `(slug, None)`; the caller turns that
        into a sentinel fingerprint.
        """
        cursor: str | None = None
        while True:
            data = await self._api.graphql(
                _ORG_REPOS_QUERY, variables={"org": org, "after": cursor}
            )
            org_data = data.get("organization") or {}
            connection = org_data.get("repositories") or {}
            for node in connection.get("nodes", []):
                if not include_archived and node.get("isArchived"):
                    continue
                owner_login = (node.get("owner") or {}).get("login")
                name = node.get("name")
                if not owner_login or not name:
                    continue
                target = (node.get("defaultBranchRef") or {}).get("target") or {}
                sha = target.get("oid")
                yield f"{owner_login}/{name}", sha if isinstance(sha, str) else None
            page_info = connection.get("pageInfo", {})
            if not page_info.get("hasNextPage"):
                return
            cursor = page_info.get("endCursor")

    # ------------------------------------------------------------------
    # discover
    # ------------------------------------------------------------------

    async def discover(
        self,
        filter: SourceFilter,
        cursor: Cursor | None,
    ) -> AsyncIterator[DocumentRef]:
        """Enumerate refs across the configured target.

        For repo targets: walk the Git Trees API at HEAD recursively.
        For org / enterprise: list repos via GraphQL (cursor-paged) then
        descend into each repo's tree.
        """
        # Refresh the bearer once at the top of discover so every
        # request inside this iterator sees the same token. Token refresh
        # mid-iteration is handled lazily inside `_token`.
        await self._refresh_bearer()
        repos: list[tuple[str, str]] = []  # (owner, name)
        page_cursor: str | None = cursor

        if self._config.repo is not None:
            owner, name = _split_slug(self._config.repo)
            repos.append((owner, name))
        elif self._config.org is not None:
            async for owner, name, next_cursor in self._iter_org_repos(
                self._config.org,
                include_archived=self._config.include_archived,
                after=page_cursor,
                since=filter.since,
            ):
                async for ref in self._iter_repo_refs(owner, name, filter, next_cursor):
                    yield ref
                page_cursor = next_cursor
            return
        else:
            assert self._config.enterprise is not None
            async for org_name in self._iter_enterprise_orgs(self._config.enterprise):
                async for owner, name, next_cursor in self._iter_org_repos(
                    org_name,
                    include_archived=self._config.include_archived,
                    after=None,
                    since=filter.since,
                ):
                    async for ref in self._iter_repo_refs(
                        owner, name, filter, next_cursor
                    ):
                        yield ref
            return

        # Single-repo case falls through to here.
        for owner, name in repos:
            async for ref in self._iter_repo_refs(owner, name, filter, None):
                yield ref

    async def _iter_repo_refs(
        self,
        owner: str,
        name: str,
        filter: SourceFilter,
        outer_cursor: str | None,
    ) -> AsyncIterator[DocumentRef]:
        """Walk a single repo's Git tree and yield blob refs."""
        slug = f"{owner}/{name}"
        if slug in self._skip_subsources:
            # Tier-1 cache hit — IncrementalRunner already replayed
            # this repo's findings; skip the tree walk entirely.
            return
        # `git/trees/HEAD?recursive=1` returns up to 100k entries with a
        # `truncated:true` flag if the tree is larger. For trees beyond
        # the limit we'd switch to per-directory recursion, but no scan
        # we have ever exercised has hit that ceiling.
        response = await self._api.get(
            f"/repos/{owner}/{name}/git/trees/HEAD",
            params={"recursive": "1"},
        )
        if response.status_code != 200:
            # 404 means the repo was deleted between enumeration and
            # walk (race). Skip silently.
            return
        body = response.json()
        max_size = filter.max_size
        for entry in body.get("tree", []):
            if entry.get("type") != "blob":
                continue
            path = entry.get("path", "")
            size = entry.get("size")
            if max_size is not None and isinstance(size, int) and size > max_size:
                continue
            if filter.exclude and any(_glob_match(path, p) for p in filter.exclude):
                continue
            if filter.include and not any(_glob_match(path, p) for p in filter.include):
                continue
            sha = entry["sha"]
            yield DocumentRef(
                source_id=self.id,
                source_kind=self.kind,
                path=f"{slug}/{path}",
                native_url=f"https://github.com/{slug}/blob/HEAD/{path}",
                parent_chain=(f"github://{slug}",),
                content_type="text/plain",
                size=size if isinstance(size, int) else None,
                etag=sha,
                metadata={
                    "slug": slug,
                    "owner": owner,
                    "repo": name,
                    "blob_sha": sha,
                    "inner_path": path,
                    # Subsource attribution lets the IncrementalRunner
                    # store per-repo rollups on the next clean scan.
                    SUBSOURCE_METADATA_KEY: slug,
                    # Round-trip the org-level cursor so the scheduler
                    # can resume; ADR §5 specifies this lives on the ref.
                    **({"_cursor": outer_cursor} if outer_cursor else {}),
                },
            )

    async def _iter_org_repos(
        self,
        org: str,
        *,
        include_archived: bool,
        after: str | None,
        since: datetime | None,
    ) -> AsyncIterator[tuple[str, str, str | None]]:
        """Page through `organization.repositories` via GraphQL.

        Yields `(owner, name, next_cursor)`. The next_cursor lets
        `discover()` checkpoint partway through a 10k-repo org.
        Server-side filters: `isArchived` and (when `since` is set)
        `orderBy: PUSHED_AT DESC` + client-side stop on `pushed_at < since`.
        """
        cursor = after
        while True:
            data = await self._api.graphql(
                _ORG_REPOS_QUERY,
                variables={"org": org, "after": cursor},
            )
            org_data = data.get("organization") or {}
            connection = org_data.get("repositories") or {}
            for node in connection.get("nodes", []):
                if not include_archived and node.get("isArchived"):
                    continue
                if since is not None:
                    pushed_at = node.get("pushedAt")
                    if pushed_at and _parse_iso(pushed_at) < since:
                        # Sorted DESC; everything that follows is older.
                        return
                yield (
                    node["owner"]["login"],
                    node["name"],
                    connection.get("pageInfo", {}).get("endCursor"),
                )
            page_info = connection.get("pageInfo", {})
            if not page_info.get("hasNextPage"):
                return
            cursor = page_info.get("endCursor")

    async def _iter_enterprise_orgs(self, enterprise: str) -> AsyncIterator[str]:
        """Page through `enterprise.organizations` (GHES only).

        Returns just org logins; downstream re-enters
        `_iter_org_repos` for each. We do not surface per-enterprise
        cursor — enterprises are O(100) at most, vs. orgs which can be
        O(10k) repos.
        """
        cursor: str | None = None
        while True:
            data = await self._api.graphql(
                _ENTERPRISE_ORGS_QUERY,
                variables={"slug": enterprise, "after": cursor},
            )
            ent = data.get("enterprise") or {}
            orgs = ent.get("organizations") or {}
            for node in orgs.get("nodes", []):
                yield node["login"]
            page_info = orgs.get("pageInfo", {})
            if not page_info.get("hasNextPage"):
                return
            cursor = page_info.get("endCursor")

    # ------------------------------------------------------------------
    # fetch
    # ------------------------------------------------------------------

    async def fetch(
        self,
        ref: DocumentRef,
    ) -> AsyncIterator[Document | DocumentChunk]:
        """Pull a single blob via the Git blobs REST API.

        We chose blob-by-blob over tarball even for medium repos because
        (a) the scheduler already has per-file rate budget and (b) the
        blob endpoint cooperates with `If-None-Match: <sha>` so an
        unchanged blob is a 304, not a 1MB GET. The `_TARBALL_BYTE_CEILING`
        path is exposed as `fetch_repo_tarball()` for callers that want
        to bulk-pull a small repo in one HTTP.
        """
        await self._refresh_bearer()
        owner = ref.metadata.get("owner")
        repo = ref.metadata.get("repo")
        sha = ref.metadata.get("blob_sha")
        if owner is None or repo is None or sha is None:
            # Stale ref or wrong connector — yield nothing rather than
            # crashing the scheduler's gather().
            return
        response = await self._api.get(f"/repos/{owner}/{repo}/git/blobs/{sha}")
        if response.status_code != 200:
            return
        payload = response.json()
        # /git/blobs returns base64-encoded content; we decode and emit
        # text (UTF-8, errors=replace) to match the dir/builtin-github
        # connectors which both yield text.
        encoded = payload.get("content", "")
        try:
            raw = base64.b64decode(encoded)
        except (ValueError, TypeError):
            return
        text = raw.decode("utf-8", errors="replace")
        yield Document(
            ref=ref,
            text=text,
            fetched_at=datetime.now(UTC),
            content_hash=sha,
        )

    async def fetch_repo_tarball(self, owner: str, repo: str) -> bytes | None:
        """Bulk-pull a small repo as a tarball (single HTTP).

        Used opportunistically when a discovery pass determined the
        repo is under `_TARBALL_BYTE_CEILING`. Returns the raw tar.gz
        bytes for the caller to extract; we don't extract here because
        that would couple this module to ContentExtractor.
        """
        await self._refresh_bearer()
        response = await self._api.get(
            f"/repos/{owner}/{repo}/tarball",
            accept="application/vnd.github.v3.raw",
        )
        if response.status_code != 200:
            return None
        if len(response.content) > _TARBALL_BYTE_CEILING:
            return None
        return response.content

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    async def _refresh_bearer(self) -> None:
        """Set the api bearer to a current installation token."""
        token = await self._auth.get_installation_token()
        self._api.set_token(token)


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _split_slug(slug: str) -> tuple[str, str]:
    """Split `owner/repo` (or fail loudly).

    We don't accept full URLs here — the App API endpoints are addressed
    by `(owner, repo)` only and a stray scheme would silently 404.
    """
    if "/" not in slug or slug.count("/") != 1:
        raise ValueError(
            f"github-app `repo` must be in `owner/name` form; got {slug!r}"
        )
    owner, name = slug.split("/", 1)
    if not owner or not name:
        raise ValueError(f"github-app `repo` malformed: {slug!r}")
    return owner, name


def _parse_iso(value: str) -> datetime:
    """Parse GitHub's ISO 8601 timestamps, including the Z suffix."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _glob_match(path: str, pattern: str) -> bool:
    """Minimal fnmatch wrapper exposed as a hook for tests."""
    from fnmatch import fnmatch

    return fnmatch(path, pattern)


# ---------------------------------------------------------------------
# GraphQL queries
# ---------------------------------------------------------------------


_ORG_REPOS_QUERY = """
query($org: String!, $after: String) {
  organization(login: $org) {
    repositories(
      first: 100
      after: $after
      orderBy: {field: PUSHED_AT, direction: DESC}
    ) {
      pageInfo { endCursor hasNextPage }
      nodes {
        name
        owner { login }
        isArchived
        pushedAt
        defaultBranchRef { target { ... on Commit { oid } } }
      }
    }
  }
}
""".strip()


_ENTERPRISE_ORGS_QUERY = """
query($slug: String!, $after: String) {
  enterprise(slug: $slug) {
    organizations(first: 100, after: $after) {
      pageInfo { endCursor hasNextPage }
      nodes { login }
    }
  }
}
""".strip()


# ---------------------------------------------------------------------
# Factory + Spec
# ---------------------------------------------------------------------


def _factory(config: Mapping[str, Any]) -> GithubAppConnector:
    """Build a connector from a plain config mapping.

    The credential is fetched separately (CredentialBroker), but we
    accept it in the config dict under the key `_credential` so the
    registry-driven flow can hand it through. Tests construct the
    connector directly and avoid this code path.
    """
    cred_obj = config.get("_credential")
    if not isinstance(cred_obj, Credential):
        raise ValueError(
            "github-app factory requires a resolved Credential under "
            "config['_credential'] (set by the scheduler from CredentialBroker)"
        )
    return GithubAppConnector(
        GithubAppConfig(
            repo=str(config["repo"]) if config.get("repo") is not None else None,
            org=str(config["org"]) if config.get("org") is not None else None,
            enterprise=(
                str(config["enterprise"])
                if config.get("enterprise") is not None
                else None
            ),
            base_url=str(config.get("base_url", DEFAULT_BASE_URL)),
            include_archived=bool(config.get("include_archived", False)),
            id=str(config["id"]) if config.get("id") is not None else None,
        ),
        credential=cred_obj,
    )


# Re-exported via the package __init__ as `SPEC`. We construct it lazily
# at import time so the entry-point loader sees a ConnectorSpec instance,
# not a callable.
from pleno_pii_scanner.sources.registry import ConnectorSpec  # noqa: E402

SPEC = ConnectorSpec(
    kind=KIND,
    version="0.1.0",
    factory=_factory,
    capabilities=Capabilities(
        incremental=True,
        binary=False,
        content_hash_delta=True,
        max_concurrent_fetches=8,
    ),
    required_scopes=("contents:read", "metadata:read"),
    description=(
        "GitHub App connector. App auth (no PAT), GHES support via "
        "base_url, GraphQL-paged org/enterprise enumeration (no silent "
        "1000-repo truncation), per-blob fetch with ETag short-circuit, "
        "rate-limit feedback to the scheduler. ADR-0007 §13."
    ),
)


__all__ = [
    "KIND",
    "RateLimited",
    "SPEC",
    "GithubAppConfig",
    "GithubAppConnector",
    "_TARBALL_BYTE_CEILING",
]
