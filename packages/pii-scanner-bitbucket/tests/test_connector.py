"""Tests for BitbucketConnector — Cloud + Server.

Hermetic: every test injects either an `httpx.MockTransport` (so no
real HTTP is dispatched) or a `clone_fn` / `enumerate_fn` double (so
no real `git clone` runs). Each `git`-side path the connector might
shell out to has a stub.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from pleno_pii_scanner.credentials.broker import Credential
from pleno_pii_scanner.sources.base import (
    Capabilities,
    Document,
    DocumentRef,
    SourceConnector,
    SourceFilter,
)
from pleno_pii_scanner_bitbucket import (
    DEFAULT_CLOUD_BASE_URL,
    BasicAuth,
    BearerAuth,
    BitbucketConfig,
    BitbucketConnector,
    SPEC,
    KIND,
)
from pleno_pii_scanner_bitbucket.connector import (
    _browse_url,
    _build_auth,
    _embed_credentials,
    _normalise_base_url,
    _pick_cloud_clone_url,
    _pick_server_clone_url,
    _single_repo_clone_url,
)
from tests.conftest import make_handler


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


async def _drain(it: AsyncIterator[DocumentRef]) -> list[DocumentRef]:
    return [r async for r in it]


def _populate_repo(dest: Path, files: dict[str, str]) -> Path:
    """Test seam: write files into `dest` to pretend a clone succeeded."""
    dest.mkdir(parents=True, exist_ok=True)
    for path, content in files.items():
        full = dest / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
    return dest


# ---------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------


class TestConfig:
    def test_cloud_requires_workspace_or_repo_slug(self) -> None:
        with pytest.raises(ValueError, match="workspace"):
            BitbucketConfig(flavor="cloud")

    def test_server_requires_project_or_repo_slug(self) -> None:
        with pytest.raises(ValueError, match="project"):
            BitbucketConfig(flavor="server", base_url="https://bb.acme")

    def test_server_requires_base_url(self) -> None:
        with pytest.raises(ValueError, match="base_url"):
            BitbucketConfig(flavor="server", project="PROD")

    def test_unsupported_flavor_rejected(self) -> None:
        with pytest.raises(ValueError, match="flavor"):
            BitbucketConfig(flavor="ghe", workspace="x")  # type: ignore[arg-type]

    def test_depth_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="depth"):
            BitbucketConfig(flavor="cloud", workspace="x", depth=0)

    def test_resolved_id_cloud_workspace(self) -> None:
        c = BitbucketConfig(flavor="cloud", workspace="acme")
        assert c.resolved_id() == "bitbucket-cloud:acme"

    def test_resolved_id_server_project(self) -> None:
        c = BitbucketConfig(
            flavor="server",
            project="PROD",
            base_url="https://bb.acme/rest/api/1.0",
        )
        assert c.resolved_id() == "bitbucket-server:PROD"

    def test_resolved_id_repo_slug(self) -> None:
        c = BitbucketConfig(flavor="cloud", repo_slug="acme/widgets")
        assert c.resolved_id() == "bitbucket-cloud:acme/widgets"

    def test_resolved_id_explicit_id_wins(self) -> None:
        c = BitbucketConfig(flavor="cloud", workspace="acme", id="custom")
        assert c.resolved_id() == "custom"

    def test_resolved_base_url_cloud_default(self) -> None:
        c = BitbucketConfig(flavor="cloud", workspace="acme")
        assert c.resolved_base_url() == DEFAULT_CLOUD_BASE_URL

    def test_resolved_base_url_appends_api_prefix_for_cloud(self) -> None:
        c = BitbucketConfig(
            flavor="cloud",
            workspace="acme",
            base_url="https://api.bitbucket.org",
        )
        assert c.resolved_base_url() == "https://api.bitbucket.org/2.0"

    def test_resolved_base_url_appends_api_prefix_for_server(self) -> None:
        c = BitbucketConfig(
            flavor="server",
            project="PROD",
            base_url="https://bb.acme",
        )
        assert c.resolved_base_url() == "https://bb.acme/rest/api/1.0"


# ---------------------------------------------------------------------
# Auth selection
# ---------------------------------------------------------------------


class TestAuthSelection:
    def test_cloud_bearer(self, cloud_token_credential: Credential) -> None:
        auth = _build_auth("cloud", cloud_token_credential)
        assert isinstance(auth, BearerAuth)
        assert auth.token == "ws_token_abc"

    def test_cloud_basic(self, cloud_basic_credential: Credential) -> None:
        auth = _build_auth("cloud", cloud_basic_credential)
        assert isinstance(auth, BasicAuth)
        assert auth.username == "alice"

    def test_server_bearer(self, server_token_credential: Credential) -> None:
        auth = _build_auth("server", server_token_credential)
        assert isinstance(auth, BearerAuth)

    def test_server_basic(self, server_basic_credential: Credential) -> None:
        auth = _build_auth("server", server_basic_credential)
        assert isinstance(auth, BasicAuth)
        assert auth.password == "p@55"

    def test_token_alias_accepted(self) -> None:
        # `token` is accepted as a synonym for `access_token` because
        # several CredentialResolver plugins (1Password, Vault) emit
        # the more generic key.
        cred = Credential(kind="bitbucket", payload={"token": "tok"})
        auth = _build_auth("cloud", cred)
        assert isinstance(auth, BearerAuth)

    def test_missing_credential_rejected_cloud(self) -> None:
        cred = Credential(kind="bitbucket", payload={})
        with pytest.raises(ValueError, match="app_password"):
            _build_auth("cloud", cred)

    def test_missing_credential_rejected_server(self) -> None:
        cred = Credential(kind="bitbucket", payload={"username": "a"})
        with pytest.raises(ValueError, match="password"):
            _build_auth("server", cred)


# ---------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------


class TestConstruction:
    def test_runtime_protocol_isinstance(
        self, cloud_token_credential: Credential
    ) -> None:
        c = BitbucketConnector(
            BitbucketConfig(flavor="cloud", workspace="acme"),
            credential=cloud_token_credential,
        )
        assert isinstance(c, SourceConnector)
        assert c.kind == "bitbucket"
        assert c.id == "bitbucket-cloud:acme"

    def test_capabilities(self, cloud_token_credential: Credential) -> None:
        c = BitbucketConnector(
            BitbucketConfig(flavor="cloud", workspace="acme"),
            credential=cloud_token_credential,
        )
        caps = c.capabilities()
        assert caps == Capabilities(
            incremental=False,
            binary=False,
            content_hash_delta=False,
            max_concurrent_fetches=4,
            streaming=False,
        )


# ---------------------------------------------------------------------
# Cloud discover/fetch
# ---------------------------------------------------------------------


class TestCloud:
    async def test_workspace_enumeration_yields_blob_refs(
        self, tmp_path: Path, cloud_token_credential: Credential
    ) -> None:
        # Two repos in one Cloud page: r1 active, r2 archived (filtered).
        def repos_handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "values": [
                        {
                            "full_name": "acme/r1",
                            "slug": "r1",
                            "is_archived": False,
                            "is_private": True,
                            "links": {
                                "clone": [
                                    {
                                        "name": "https",
                                        "href": "https://bitbucket.org/acme/r1.git",
                                    }
                                ]
                            },
                        },
                        {
                            "full_name": "acme/r2",
                            "slug": "r2",
                            "is_archived": True,
                            "is_private": True,
                            "links": {
                                "clone": [
                                    {
                                        "name": "https",
                                        "href": "https://bitbucket.org/acme/r2.git",
                                    }
                                ]
                            },
                        },
                    ]
                },
            )

        clones: list[str] = []

        def fake_clone(url: str, dest: Path) -> Path:
            clones.append(url)
            return _populate_repo(
                dest, {"src/secret.py": "TOKEN=hunter2\n", "README.md": "# r1\n"}
            )

        transport = httpx.MockTransport(
            make_handler([("/repositories/acme", repos_handler)])
        )
        c = BitbucketConnector(
            BitbucketConfig(flavor="cloud", workspace="acme"),
            credential=cloud_token_credential,
            transport=transport,
            clone_fn=fake_clone,
        )
        try:
            refs = await _drain(c.discover(SourceFilter(), None))
            paths = sorted(r.path for r in refs)
            assert paths == ["acme/r1/README.md", "acme/r1/src/secret.py"]
            for r in refs:
                assert r.metadata["slug"] == "acme/r1"
                assert r.metadata["flavor"] == "cloud"
                assert r.parent_chain == ("bitbucket://acme/r1",)
                assert r.native_url is not None
                assert "bitbucket.org/acme/r1/src/HEAD/" in r.native_url
            # Bearer auth must be embedded as `x-token-auth:<token>@host`.
            assert clones == [
                "https://x-token-auth:ws_token_abc@bitbucket.org/acme/r1.git"
            ]
        finally:
            await c.close()

    async def test_workspace_enumeration_basic_auth_embedded(
        self, tmp_path: Path, cloud_basic_credential: Credential
    ) -> None:
        def repos_handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "values": [
                        {
                            "full_name": "acme/r1",
                            "slug": "r1",
                            "is_archived": False,
                            "is_private": True,
                            "links": {
                                "clone": [
                                    {"name": "https", "href": "https://bitbucket.org/acme/r1.git"}
                                ]
                            },
                        }
                    ]
                },
            )

        clones: list[str] = []

        def fake_clone(url: str, dest: Path) -> Path:
            clones.append(url)
            return _populate_repo(dest, {"f.txt": "x"})

        transport = httpx.MockTransport(
            make_handler([("/repositories/acme", repos_handler)])
        )
        c = BitbucketConnector(
            BitbucketConfig(flavor="cloud", workspace="acme"),
            credential=cloud_basic_credential,
            transport=transport,
            clone_fn=fake_clone,
        )
        try:
            await _drain(c.discover(SourceFilter(), None))
            assert clones == [
                "https://alice:ATBB-abc123@bitbucket.org/acme/r1.git"
            ]
        finally:
            await c.close()

    async def test_include_archived_keeps_archived(
        self, cloud_token_credential: Credential
    ) -> None:
        def repos_handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "values": [
                        {
                            "full_name": "acme/r1",
                            "slug": "r1",
                            "is_archived": True,
                            "is_private": True,
                            "links": {
                                "clone": [
                                    {"name": "https", "href": "https://bitbucket.org/acme/r1.git"}
                                ]
                            },
                        }
                    ]
                },
            )

        def fake_clone(_url: str, dest: Path) -> Path:
            return _populate_repo(dest, {"f.txt": "x"})

        transport = httpx.MockTransport(
            make_handler([("/repositories/acme", repos_handler)])
        )
        c = BitbucketConnector(
            BitbucketConfig(
                flavor="cloud", workspace="acme", include_archived=True
            ),
            credential=cloud_token_credential,
            transport=transport,
            clone_fn=fake_clone,
        )
        try:
            refs = await _drain(c.discover(SourceFilter(), None))
            assert {r.metadata["slug"] for r in refs} == {"acme/r1"}
        finally:
            await c.close()

    async def test_include_public_false_filters_public(
        self, cloud_token_credential: Credential
    ) -> None:
        def repos_handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "values": [
                        {
                            "full_name": "acme/private",
                            "slug": "private",
                            "is_archived": False,
                            "is_private": True,
                            "links": {
                                "clone": [
                                    {"name": "https", "href": "https://bitbucket.org/acme/private.git"}
                                ]
                            },
                        },
                        {
                            "full_name": "acme/public",
                            "slug": "public",
                            "is_archived": False,
                            "is_private": False,
                            "links": {
                                "clone": [
                                    {"name": "https", "href": "https://bitbucket.org/acme/public.git"}
                                ]
                            },
                        },
                    ]
                },
            )

        def fake_clone(_url: str, dest: Path) -> Path:
            return _populate_repo(dest, {"f.txt": "x"})

        transport = httpx.MockTransport(
            make_handler([("/repositories/acme", repos_handler)])
        )
        c = BitbucketConnector(
            BitbucketConfig(
                flavor="cloud", workspace="acme", include_public=False
            ),
            credential=cloud_token_credential,
            transport=transport,
            clone_fn=fake_clone,
        )
        try:
            refs = await _drain(c.discover(SourceFilter(), None))
            assert {r.metadata["slug"] for r in refs} == {"acme/private"}
        finally:
            await c.close()

    async def test_repo_slug_shortcut_skips_enumeration(
        self, cloud_token_credential: Credential
    ) -> None:
        # When repo_slug is set, enumerate_fn should NOT call the API.
        # We verify by giving the transport a handler that fails loudly.
        def fail(_: httpx.Request) -> httpx.Response:
            raise AssertionError("enumeration should not have been called")

        clones: list[str] = []

        def fake_clone(url: str, dest: Path) -> Path:
            clones.append(url)
            return _populate_repo(dest, {"f.txt": "x"})

        transport = httpx.MockTransport(fail)
        c = BitbucketConnector(
            BitbucketConfig(flavor="cloud", repo_slug="acme/r1"),
            credential=cloud_token_credential,
            transport=transport,
            clone_fn=fake_clone,
        )
        try:
            refs = await _drain(c.discover(SourceFilter(), None))
            assert {r.metadata["slug"] for r in refs} == {"acme/r1"}
            assert clones[0].endswith("@bitbucket.org/acme/r1.git")
        finally:
            await c.close()

    async def test_fetch_re_emits_with_outer_ref(
        self, cloud_token_credential: Credential
    ) -> None:
        def repos_handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "values": [
                        {
                            "full_name": "acme/r1",
                            "slug": "r1",
                            "is_archived": False,
                            "is_private": True,
                            "links": {
                                "clone": [
                                    {"name": "https", "href": "https://bitbucket.org/acme/r1.git"}
                                ]
                            },
                        }
                    ]
                },
            )

        def fake_clone(_url: str, dest: Path) -> Path:
            return _populate_repo(dest, {"a.py": "AWS_KEY=AKIA....\n"})

        transport = httpx.MockTransport(
            make_handler([("/repositories/acme", repos_handler)])
        )
        c = BitbucketConnector(
            BitbucketConfig(flavor="cloud", workspace="acme"),
            credential=cloud_token_credential,
            transport=transport,
            clone_fn=fake_clone,
        )
        try:
            refs = await _drain(c.discover(SourceFilter(), None))
            assert len(refs) == 1
            docs = [d async for d in c.fetch(refs[0])]
            assert len(docs) == 1
            assert isinstance(docs[0], Document)
            assert docs[0].text == "AWS_KEY=AKIA....\n"
            assert docs[0].ref is refs[0]  # outer ref preserved
        finally:
            await c.close()

    async def test_fetch_with_unknown_slug_yields_nothing(
        self, cloud_token_credential: Credential
    ) -> None:
        def fail(_: httpx.Request) -> httpx.Response:
            raise AssertionError("no http should be issued")

        c = BitbucketConnector(
            BitbucketConfig(flavor="cloud", workspace="acme"),
            credential=cloud_token_credential,
            transport=httpx.MockTransport(fail),
            clone_fn=lambda _u, d: d,
        )
        try:
            ghost = DocumentRef(source_id=c.id, source_kind=c.kind, path="x")
            docs = [d async for d in c.fetch(ghost)]
            assert docs == []
        finally:
            await c.close()

    async def test_malformed_repo_entry_skipped(
        self, cloud_token_credential: Credential
    ) -> None:
        # Missing `links.clone` block — third-party Bitbucket-compatible
        # servers sometimes do this. Skip silently rather than crash.
        def repos_handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "values": [
                        {"slug": "broken"},
                        {
                            "full_name": "acme/ok",
                            "slug": "ok",
                            "is_archived": False,
                            "is_private": True,
                            "links": {
                                "clone": [
                                    {"name": "https", "href": "https://bitbucket.org/acme/ok.git"}
                                ]
                            },
                        },
                    ]
                },
            )

        def fake_clone(_url: str, dest: Path) -> Path:
            return _populate_repo(dest, {"f.txt": "x"})

        transport = httpx.MockTransport(
            make_handler([("/repositories/acme", repos_handler)])
        )
        c = BitbucketConnector(
            BitbucketConfig(flavor="cloud", workspace="acme"),
            credential=cloud_token_credential,
            transport=transport,
            clone_fn=fake_clone,
        )
        try:
            refs = await _drain(c.discover(SourceFilter(), None))
            assert {r.metadata["slug"] for r in refs} == {"acme/ok"}
        finally:
            await c.close()


# ---------------------------------------------------------------------
# Server discover/fetch
# ---------------------------------------------------------------------


class TestServer:
    async def test_project_enumeration_paginated(
        self, server_token_credential: Credential
    ) -> None:
        pages = iter(
            [
                {
                    "values": [
                        {
                            "slug": "alpha",
                            "archived": False,
                            "public": False,
                            "links": {
                                "clone": [
                                    {"name": "http", "href": "https://bb.acme/scm/prod/alpha.git"}
                                ]
                            },
                        },
                    ],
                    "isLastPage": False,
                    "nextPageStart": 1,
                },
                {
                    "values": [
                        {
                            "slug": "beta",
                            "archived": True,  # filtered out by default
                            "public": False,
                            "links": {
                                "clone": [
                                    {"name": "http", "href": "https://bb.acme/scm/prod/beta.git"}
                                ]
                            },
                        },
                        {
                            "slug": "gamma",
                            "archived": False,
                            "public": False,
                            "links": {
                                "clone": [
                                    {"name": "http", "href": "https://bb.acme/scm/prod/gamma.git"}
                                ]
                            },
                        },
                    ],
                    "isLastPage": True,
                },
            ]
        )

        def repos_handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=next(pages))

        def fake_clone(_url: str, dest: Path) -> Path:
            return _populate_repo(dest, {"x.txt": "x"})

        transport = httpx.MockTransport(
            make_handler([("/projects/PROD/repos", repos_handler)])
        )
        c = BitbucketConnector(
            BitbucketConfig(
                flavor="server",
                project="PROD",
                base_url="https://bb.acme/rest/api/1.0",
            ),
            credential=server_token_credential,
            transport=transport,
            clone_fn=fake_clone,
        )
        try:
            refs = await _drain(c.discover(SourceFilter(), None))
            slugs = sorted({r.metadata["slug"] for r in refs})
            assert slugs == ["PROD/alpha", "PROD/gamma"]
            for r in refs:
                assert r.metadata["flavor"] == "server"
                assert r.native_url is not None
                assert (
                    "bb.acme/projects/PROD/repos/" in r.native_url
                )
        finally:
            await c.close()

    async def test_basic_auth_embedded_in_clone_url(
        self, server_basic_credential: Credential
    ) -> None:
        def repos_handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "values": [
                        {
                            "slug": "alpha",
                            "archived": False,
                            "public": False,
                            "links": {
                                "clone": [
                                    {
                                        "name": "https",
                                        "href": "https://bitbucket-server@bb.acme/scm/prod/alpha.git",
                                    }
                                ]
                            },
                        }
                    ],
                    "isLastPage": True,
                },
            )

        clones: list[str] = []

        def fake_clone(url: str, dest: Path) -> Path:
            clones.append(url)
            return _populate_repo(dest, {"x.txt": "x"})

        transport = httpx.MockTransport(
            make_handler([("/projects/PROD/repos", repos_handler)])
        )
        c = BitbucketConnector(
            BitbucketConfig(
                flavor="server",
                project="PROD",
                base_url="https://bb.acme/rest/api/1.0",
            ),
            credential=server_basic_credential,
            transport=transport,
            clone_fn=fake_clone,
        )
        try:
            await _drain(c.discover(SourceFilter(), None))
            # Pre-existing `bitbucket-server@` userinfo is stripped and
            # replaced with the configured basic auth.
            assert clones == [
                "https://svc:p%4055@bb.acme/scm/prod/alpha.git"
            ]
        finally:
            await c.close()

    async def test_include_public_false_filters_public_server(
        self, server_token_credential: Credential
    ) -> None:
        def repos_handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "values": [
                        {
                            "slug": "private",
                            "archived": False,
                            "public": False,
                            "links": {
                                "clone": [
                                    {"name": "https", "href": "https://bb.acme/scm/prod/private.git"}
                                ]
                            },
                        },
                        {
                            "slug": "public",
                            "archived": False,
                            "public": True,
                            "links": {
                                "clone": [
                                    {"name": "https", "href": "https://bb.acme/scm/prod/public.git"}
                                ]
                            },
                        },
                    ],
                    "isLastPage": True,
                },
            )

        def fake_clone(_url: str, dest: Path) -> Path:
            return _populate_repo(dest, {"f.txt": "x"})

        c = BitbucketConnector(
            BitbucketConfig(
                flavor="server",
                project="PROD",
                base_url="https://bb.acme/rest/api/1.0",
                include_public=False,
            ),
            credential=server_token_credential,
            transport=httpx.MockTransport(
                make_handler([("/projects/PROD/repos", repos_handler)])
            ),
            clone_fn=fake_clone,
        )
        try:
            refs = await _drain(c.discover(SourceFilter(), None))
            assert {r.metadata["slug"] for r in refs} == {"PROD/private"}
        finally:
            await c.close()

    async def test_malformed_server_entry_skipped(
        self, server_token_credential: Credential
    ) -> None:
        # Missing slug or links.clone — third-party Bitbucket-compatible
        # implementations sometimes ship malformed payloads. Skip silently.
        def repos_handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "values": [
                        {"archived": False, "public": False},  # no slug
                        {
                            "slug": "ok",
                            "archived": False,
                            "public": False,
                            "links": {
                                "clone": [
                                    {"name": "https", "href": "https://bb.acme/scm/prod/ok.git"}
                                ]
                            },
                        },
                    ],
                    "isLastPage": True,
                },
            )

        def fake_clone(_url: str, dest: Path) -> Path:
            return _populate_repo(dest, {"f.txt": "x"})

        c = BitbucketConnector(
            BitbucketConfig(
                flavor="server",
                project="PROD",
                base_url="https://bb.acme/rest/api/1.0",
            ),
            credential=server_token_credential,
            transport=httpx.MockTransport(
                make_handler([("/projects/PROD/repos", repos_handler)])
            ),
            clone_fn=fake_clone,
        )
        try:
            refs = await _drain(c.discover(SourceFilter(), None))
            assert {r.metadata["slug"] for r in refs} == {"PROD/ok"}
        finally:
            await c.close()

    async def test_ensure_clone_cache_hit_returns_existing_path(
        self, cloud_token_credential: Credential
    ) -> None:
        # Drive `discover()` twice for the same repo via repo_slug;
        # the second call must hit the cache branch in `_ensure_clone`
        # (line 309 in connector.py) — no second clone shell-out.
        clone_calls: list[str] = []

        def fake_clone(url: str, dest: Path) -> Path:
            clone_calls.append(url)
            return _populate_repo(dest, {"x.txt": "x"})

        c = BitbucketConnector(
            BitbucketConfig(flavor="cloud", repo_slug="acme/r"),
            credential=cloud_token_credential,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(404)
            ),
            clone_fn=fake_clone,
        )
        try:
            await _drain(c.discover(SourceFilter(), None))
            await _drain(c.discover(SourceFilter(), None))
            assert len(clone_calls) == 1
        finally:
            await c.close()

    async def test_repo_slug_shortcut_synthesizes_clone_url(
        self, server_token_credential: Credential
    ) -> None:
        def fail(_: httpx.Request) -> httpx.Response:
            raise AssertionError("no http should be issued")

        clones: list[str] = []

        def fake_clone(url: str, dest: Path) -> Path:
            clones.append(url)
            return _populate_repo(dest, {"x.txt": "x"})

        c = BitbucketConnector(
            BitbucketConfig(
                flavor="server",
                repo_slug="PROD/alpha",
                base_url="https://bb.acme/rest/api/1.0",
            ),
            credential=server_token_credential,
            transport=httpx.MockTransport(fail),
            clone_fn=fake_clone,
        )
        try:
            await _drain(c.discover(SourceFilter(), None))
            assert clones[0].endswith("@bb.acme/scm/prod/alpha.git")
        finally:
            await c.close()


# ---------------------------------------------------------------------
# 429 backoff (end-to-end via discover)
# ---------------------------------------------------------------------


class TestRateLimitDuringDiscover:
    async def test_discover_succeeds_after_one_429(
        self, cloud_token_credential: Credential
    ) -> None:
        slept: list[float] = []

        async def fake_sleep(s: float) -> None:
            slept.append(s)

        responses = iter(
            [
                httpx.Response(429, headers={"Retry-After": "3"}),
                httpx.Response(
                    200,
                    json={
                        "values": [
                            {
                                "full_name": "acme/r1",
                                "slug": "r1",
                                "is_archived": False,
                                "is_private": True,
                                "links": {
                                    "clone": [
                                        {"name": "https", "href": "https://bitbucket.org/acme/r1.git"}
                                    ]
                                },
                            }
                        ]
                    },
                ),
            ]
        )

        def handler(_: httpx.Request) -> httpx.Response:
            return next(responses)

        def fake_clone(_url: str, dest: Path) -> Path:
            return _populate_repo(dest, {"f.txt": "x"})

        c = BitbucketConnector(
            BitbucketConfig(flavor="cloud", workspace="acme"),
            credential=cloud_token_credential,
            transport=httpx.MockTransport(handler),
            clone_fn=fake_clone,
            sleep=fake_sleep,
        )
        try:
            refs = await _drain(c.discover(SourceFilter(), None))
            assert len(refs) == 1
            assert slept == [3.0]
        finally:
            await c.close()


# ---------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------


class TestLifecycle:
    async def test_close_rmtree_clones_and_aclose_api(
        self, tmp_path: Path, cloud_token_credential: Credential
    ) -> None:
        seen_dest: list[Path] = []

        def fake_clone(_url: str, dest: Path) -> Path:
            seen_dest.append(dest)
            return _populate_repo(dest, {"f.txt": "x"})

        def repos_handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "values": [
                        {
                            "full_name": "acme/r1",
                            "slug": "r1",
                            "is_archived": False,
                            "is_private": True,
                            "links": {
                                "clone": [
                                    {"name": "https", "href": "https://bitbucket.org/acme/r1.git"}
                                ]
                            },
                        }
                    ]
                },
            )

        c = BitbucketConnector(
            BitbucketConfig(flavor="cloud", workspace="acme"),
            credential=cloud_token_credential,
            transport=httpx.MockTransport(
                make_handler([("/repositories/acme", repos_handler)])
            ),
            clone_fn=fake_clone,
        )
        await _drain(c.discover(SourceFilter(), None))
        assert seen_dest and seen_dest[0].exists()
        await c.close()
        assert not seen_dest[0].exists()


# ---------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------


class TestCloneIntoTempdir:
    def test_clones_local_bare_repo(self, tmp_path: Path) -> None:
        # Build a real local bare repo, then exercise `_clone_into_tempdir`
        # against it. This proves the subprocess shell-out works without
        # touching the network. Skips cleanly when `git` is unavailable.
        import shutil as _sh
        import subprocess as _sp

        from pleno_pii_scanner_bitbucket.connector import _clone_into_tempdir

        if _sh.which("git") is None:
            pytest.skip("git not on PATH")

        # Source: a one-commit working repo, then clone --bare to get the
        # canonical clone source. We isolate `HOME` and disable git config
        # discovery so user-level hooks/aliases cannot perturb the test.
        src = tmp_path / "src"
        src.mkdir()
        env = {
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "HOME": str(tmp_path),
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        }
        _sp.run(["git", "init", "-q", "-b", "main", str(src)], check=True, env=env)
        (src / "f.txt").write_text("hello\n")
        _sp.run(["git", "-C", str(src), "add", "."], check=True, env=env)
        _sp.run(
            ["git", "-C", str(src), "commit", "-q", "-m", "init"],
            check=True,
            env=env,
        )

        bare = tmp_path / "src.git"
        _sp.run(
            ["git", "clone", "-q", "--bare", str(src), str(bare)],
            check=True,
            env=env,
        )

        dest = tmp_path / "out"
        # `_clone_into_tempdir` does not pass an env override; Bitbucket
        # server URLs in production have no userinfo collision with the
        # operator's git config. The local clone exercises the same path.
        result = _clone_into_tempdir(str(bare), dest)
        assert result == dest
        assert (dest / "f.txt").read_text() == "hello\n"

    def test_failed_clone_rmtrees_dest(self, tmp_path: Path) -> None:
        # Pointing at a non-existent path makes `git clone` fail; we
        # verify that the helper rmtree's the half-populated dest dir
        # so a failed clone does not leak temporary state.
        from pleno_pii_scanner_bitbucket.connector import _clone_into_tempdir

        dest = tmp_path / "out"
        with pytest.raises(Exception):
            _clone_into_tempdir(str(tmp_path / "does-not-exist"), dest)
        assert not dest.exists()


class TestUrlHelpers:
    def test_pick_cloud_clone_url_https_only(self) -> None:
        repo = {
            "links": {
                "clone": [
                    {"name": "ssh", "href": "git@bitbucket.org:acme/r.git"},
                    {"name": "https", "href": "https://bitbucket.org/acme/r.git"},
                ]
            }
        }
        assert _pick_cloud_clone_url(repo) == "https://bitbucket.org/acme/r.git"

    def test_pick_cloud_clone_url_missing_returns_none(self) -> None:
        assert _pick_cloud_clone_url({}) is None
        assert _pick_cloud_clone_url({"links": {"clone": []}}) is None
        assert _pick_cloud_clone_url(
            {"links": {"clone": [{"name": "ssh", "href": "x"}]}}
        ) is None

    def test_pick_server_clone_url_accepts_http_or_https(self) -> None:
        assert _pick_server_clone_url(
            {"links": {"clone": [{"name": "http", "href": "https://bb/scm/p/r.git"}]}}
        ) == "https://bb/scm/p/r.git"

    def test_pick_server_clone_url_missing_returns_none(self) -> None:
        assert _pick_server_clone_url({}) is None
        assert _pick_server_clone_url(
            {"links": {"clone": [{"name": "ssh", "href": "x"}]}}
        ) is None

    def test_single_repo_clone_url_cloud(self) -> None:
        c = BitbucketConfig(flavor="cloud", repo_slug="acme/r")
        assert (
            _single_repo_clone_url(c) == "https://bitbucket.org/acme/r.git"
        )

    def test_single_repo_clone_url_server(self) -> None:
        c = BitbucketConfig(
            flavor="server",
            repo_slug="PROD/alpha",
            base_url="https://bb.acme/rest/api/1.0",
        )
        # Project segment lower-cased to match Bitbucket Server's SCM
        # routing convention.
        assert _single_repo_clone_url(c) == "https://bb.acme/scm/prod/alpha.git"

    def test_single_repo_clone_url_server_bare_host(self) -> None:
        # When the operator passes the bare host (no `/rest/api/1.0`
        # suffix) the helper must NOT strip anything — exercises the
        # else branch of the suffix-trim guard.
        c = BitbucketConfig(
            flavor="server",
            repo_slug="PROD/alpha",
            base_url="https://bb.acme",
        )
        assert _single_repo_clone_url(c) == "https://bb.acme/scm/prod/alpha.git"

    def test_browse_url_cloud(self) -> None:
        c = BitbucketConfig(flavor="cloud", workspace="acme")
        url = _browse_url(c, "acme/r1", "src/main.py")
        assert url == "https://bitbucket.org/acme/r1/src/HEAD/src/main.py"

    def test_browse_url_server(self) -> None:
        c = BitbucketConfig(
            flavor="server", project="PROD", base_url="https://bb.acme"
        )
        url = _browse_url(c, "PROD/alpha", "src/main.py")
        assert (
            url == "https://bb.acme/projects/PROD/repos/alpha/browse/src/main.py"
        )

    def test_normalise_base_url_cloud_idempotent(self) -> None:
        assert (
            _normalise_base_url("cloud", "https://api.bitbucket.org/2.0/")
            == "https://api.bitbucket.org/2.0"
        )

    def test_normalise_base_url_cloud_already_with_suffix(self) -> None:
        # The branch where the URL already has /2.0 must not append it
        # again. Caught a real bug we hit in an earlier draft.
        assert (
            _normalise_base_url("cloud", "https://api.bitbucket.org/2.0")
            == "https://api.bitbucket.org/2.0"
        )

    def test_pick_clone_url_skips_non_matching_entries(self) -> None:
        # Both pickers must traverse past entries that don't match the
        # protocol filter (this exercises the loop-continue branch).
        repo_cloud = {
            "links": {
                "clone": [
                    {"name": "ssh", "href": "x"},
                    {"name": "https", "href": "https://h"},
                ]
            }
        }
        assert _pick_cloud_clone_url(repo_cloud) == "https://h"
        # And the case where the matching name is found but href is not
        # a string (defensive against schema drift).
        assert _pick_cloud_clone_url(
            {"links": {"clone": [{"name": "https"}]}}
        ) is None
        assert _pick_server_clone_url(
            {"links": {"clone": [{"name": "https"}]}}
        ) is None

    def test_normalise_base_url_server_idempotent(self) -> None:
        assert (
            _normalise_base_url(
                "server", "https://bb.acme/rest/api/1.0"
            )
            == "https://bb.acme/rest/api/1.0"
        )

    def test_embed_credentials_passes_through_non_https(self) -> None:
        # SSH URL must not be modified — git's auth path for ssh is
        # the user's ssh-agent, not URL-embedded creds.
        url = "ssh://git@bitbucket.org:acme/r.git"
        assert _embed_credentials(url, BearerAuth(token="t")) == url


# ---------------------------------------------------------------------
# Factory + SPEC
# ---------------------------------------------------------------------


class TestSpec:
    def test_spec_metadata(self) -> None:
        assert SPEC.kind == "bitbucket"
        assert KIND == "bitbucket"
        assert SPEC.required_scopes == ("repository:read",)
        assert SPEC.capabilities.incremental is False

    def test_factory_cloud_workspace(
        self, cloud_token_credential: Credential
    ) -> None:
        c = SPEC.factory(
            {
                "flavor": "cloud",
                "workspace": "acme",
                "_credential": cloud_token_credential,
            }
        )
        assert isinstance(c, BitbucketConnector)
        assert c.id == "bitbucket-cloud:acme"

    def test_factory_server_project(
        self, server_token_credential: Credential
    ) -> None:
        c = SPEC.factory(
            {
                "flavor": "server",
                "project": "PROD",
                "base_url": "https://bb.acme/rest/api/1.0",
                "_credential": server_token_credential,
                "ca_bundle_path": None,
                "include_archived": True,
                "include_public": False,
                "depth": 1,
                "id": "my-source",
            }
        )
        assert isinstance(c, BitbucketConnector)
        assert c.id == "my-source"

    def test_factory_repo_slug_only(
        self, cloud_token_credential: Credential
    ) -> None:
        c = SPEC.factory(
            {
                "flavor": "cloud",
                "repo_slug": "acme/r",
                "_credential": cloud_token_credential,
            }
        )
        assert c.id == "bitbucket-cloud:acme/r"

    def test_factory_requires_credential(self) -> None:
        with pytest.raises(ValueError, match="Credential"):
            SPEC.factory({"flavor": "cloud", "workspace": "acme"})

    def test_factory_rejects_invalid_flavor(
        self, cloud_token_credential: Credential
    ) -> None:
        with pytest.raises(ValueError, match="flavor"):
            SPEC.factory(
                {
                    "flavor": "ghe",
                    "workspace": "acme",
                    "_credential": cloud_token_credential,
                }
            )

    def test_factory_passes_ca_bundle(
        self, server_token_credential: Credential, tmp_path: Path
    ) -> None:
        # Construct a minimally valid PEM so SSLContext does not error.
        from datetime import datetime, timedelta, timezone

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ca")])
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(1)
            .not_valid_before(datetime.now(timezone.utc))
            .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
            .sign(key, hashes.SHA256())
        )
        bundle = tmp_path / "ca.pem"
        bundle.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        c = SPEC.factory(
            {
                "flavor": "server",
                "project": "PROD",
                "base_url": "https://bb.acme/rest/api/1.0",
                "ca_bundle_path": str(bundle),
                "_credential": server_token_credential,
            }
        )
        assert c.config.ca_bundle_path == str(bundle)


# ---------------------------------------------------------------------
# Package __init__ re-exports
# ---------------------------------------------------------------------


class TestPackageInit:
    def test_top_level_exports(self) -> None:
        import pleno_pii_scanner_bitbucket as pkg

        assert pkg.SPEC is SPEC
        assert pkg.KIND == "bitbucket"
        assert pkg.BitbucketConfig is BitbucketConfig
        assert pkg.BitbucketConnector is BitbucketConnector
        assert pkg.__version__ == "0.1.0"
