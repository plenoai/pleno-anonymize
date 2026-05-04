"""Tests for the GithubAppConnector — discover, fetch, org enumeration."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime

import httpx
import pytest

from pleno_pii_scanner.credentials.broker import Credential
from pleno_pii_scanner.scheduler.rate_limit import RateLimited
from pleno_pii_scanner.sources.base import (
    Capabilities,
    Document,
    DocumentRef,
    SourceConnector,
    SourceFilter,
)
from pleno_pii_scanner_github.connector import (
    KIND,
    SPEC,
    _TARBALL_BYTE_CEILING,
    GithubAppConfig,
    GithubAppConnector,
    _glob_match,
    _parse_iso,
    _split_slug,
)


# ---------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------


def make_credential(rsa_pem: str) -> Credential:
    return Credential(
        kind="github-app",
        payload={
            "app_id": "1",
            "installation_id": "42",
            "private_key": rsa_pem,
        },
    )


# A canned access-token response for App auth — every test handler must
# answer this first when the connector lazy-mints its installation token.
def _access_token_response() -> httpx.Response:
    return httpx.Response(
        201,
        json={
            "token": "ghs_install_abc",
            "expires_at": "2099-01-01T00:00:00Z",
        },
    )


def make_handler(
    routes: list[tuple[str, Callable[[httpx.Request], httpx.Response]]],
) -> Callable[[httpx.Request], httpx.Response]:
    """Build a routing handler from a list of (path-suffix, response_fn).

    Routes are matched in order. The first match wins. Unmatched
    requests cause an explicit AssertionError so we never silently
    return 200 for a path the test forgot to mock.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for suffix, fn in routes:
            if suffix in url:
                return fn(request)
        raise AssertionError(f"no route matches {url}")
    return handler


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


async def _drain(it: AsyncIterator[DocumentRef]) -> list[DocumentRef]:
    return [r async for r in it]


# ---------------------------------------------------------------------
# config
# ---------------------------------------------------------------------


class TestConfig:
    def test_requires_exactly_one_target(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            GithubAppConfig()

    def test_rejects_repo_and_org(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            GithubAppConfig(repo="a/b", org="acme")

    def test_rejects_all_three(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            GithubAppConfig(repo="a/b", org="acme", enterprise="x")

    def test_resolved_id_repo(self) -> None:
        assert GithubAppConfig(repo="a/b").resolved_id() == "github-app:a/b"

    def test_resolved_id_org(self) -> None:
        assert GithubAppConfig(org="acme").resolved_id() == "github-org:acme"

    def test_resolved_id_enterprise(self) -> None:
        assert (
            GithubAppConfig(enterprise="acme-inc").resolved_id()
            == "github-enterprise:acme-inc"
        )

    def test_explicit_id_overrides(self) -> None:
        assert GithubAppConfig(repo="a/b", id="custom").resolved_id() == "custom"

    def test_default_base_url(self) -> None:
        assert GithubAppConfig(repo="a/b").base_url == "https://api.github.com"


# ---------------------------------------------------------------------
# construction / capabilities / protocol
# ---------------------------------------------------------------------


class TestConstruction:
    def test_runtime_protocol_isinstance(self, rsa_pem: str) -> None:
        c = GithubAppConnector(
            GithubAppConfig(repo="a/b"),
            credential=make_credential(rsa_pem),
        )
        assert isinstance(c, SourceConnector)
        assert c.kind == "github-app"
        assert c.id == "github-app:a/b"

    def test_capabilities(self, rsa_pem: str) -> None:
        c = GithubAppConnector(
            GithubAppConfig(repo="a/b"),
            credential=make_credential(rsa_pem),
        )
        caps = c.capabilities()
        assert caps == Capabilities(
            incremental=True,
            binary=False,
            content_hash_delta=True,
            max_concurrent_fetches=8,
            streaming=False,
        )

    def test_credential_missing_keys_rejected(self) -> None:
        cred = Credential(kind="github-app", payload={"app_id": "1"})
        with pytest.raises(ValueError, match="requires keys"):
            GithubAppConnector(
                GithubAppConfig(repo="a/b"), credential=cred
            )

    def test_credential_non_string_pk_rejected(self, rsa_pem: str) -> None:
        cred = Credential(
            kind="github-app",
            payload={
                "app_id": "1",
                "installation_id": "42",
                "private_key": 12345,
            },
        )
        with pytest.raises(ValueError, match="PEM"):
            GithubAppConnector(GithubAppConfig(repo="a/b"), credential=cred)

    def test_credential_bytes_pk_accepted(self, rsa_pem: str) -> None:
        cred = Credential(
            kind="github-app",
            payload={
                "app_id": "1",
                "installation_id": "42",
                "private_key": rsa_pem.encode(),
            },
        )
        # No exception.
        GithubAppConnector(GithubAppConfig(repo="a/b"), credential=cred)


# ---------------------------------------------------------------------
# discover — single repo
# ---------------------------------------------------------------------


class TestDiscoverSingleRepo:
    async def test_yields_blob_refs_with_metadata(self, rsa_pem: str) -> None:
        def access_token(_: httpx.Request) -> httpx.Response:
            return _access_token_response()

        def trees(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "tree": [
                        {
                            "type": "blob",
                            "path": "src/secret.py",
                            "sha": "abc123",
                            "size": 100,
                        },
                        {
                            "type": "tree",
                            "path": "src",
                            "sha": "def456",
                        },
                        {
                            "type": "blob",
                            "path": "README.md",
                            "sha": "ffff",
                            "size": 50,
                        },
                    ]
                },
            )

        transport = httpx.MockTransport(make_handler([
            ("/access_tokens", access_token),
            ("/git/trees/HEAD", trees),
        ]))
        c = GithubAppConnector(
            GithubAppConfig(repo="acme/widgets"),
            credential=make_credential(rsa_pem),
            transport=transport,
        )
        try:
            refs = await _drain(c.discover(SourceFilter(), None))
            paths = {r.path for r in refs}
            assert paths == {"acme/widgets/src/secret.py", "acme/widgets/README.md"}
            for r in refs:
                assert r.metadata["slug"] == "acme/widgets"
                assert r.metadata["owner"] == "acme"
                assert r.metadata["repo"] == "widgets"
                assert r.metadata["blob_sha"] in {"abc123", "ffff"}
                assert r.parent_chain == ("github://acme/widgets",)
                assert r.native_url is not None
                assert "github.com/acme/widgets/blob/HEAD" in r.native_url
        finally:
            await c.close()

    async def test_filter_max_size(self, rsa_pem: str) -> None:
        def trees(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "tree": [
                        {"type": "blob", "path": "small.txt", "sha": "a", "size": 100},
                        {"type": "blob", "path": "big.bin", "sha": "b", "size": 99999},
                    ]
                },
            )

        transport = httpx.MockTransport(make_handler([
            ("/access_tokens", lambda _: _access_token_response()),
            ("/git/trees/HEAD", trees),
        ]))
        c = GithubAppConnector(
            GithubAppConfig(repo="a/b"),
            credential=make_credential(rsa_pem),
            transport=transport,
        )
        try:
            refs = await _drain(c.discover(SourceFilter(max_size=1000), None))
            paths = {r.path for r in refs}
            assert paths == {"a/b/small.txt"}
        finally:
            await c.close()

    async def test_filter_include_exclude_globs(self, rsa_pem: str) -> None:
        def trees(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "tree": [
                        {"type": "blob", "path": "src/a.py", "sha": "1", "size": 1},
                        {"type": "blob", "path": "src/b.go", "sha": "2", "size": 1},
                        {"type": "blob", "path": "tests/c.py", "sha": "3", "size": 1},
                    ]
                },
            )

        transport = httpx.MockTransport(make_handler([
            ("/access_tokens", lambda _: _access_token_response()),
            ("/git/trees/HEAD", trees),
        ]))
        c = GithubAppConnector(
            GithubAppConfig(repo="a/b"),
            credential=make_credential(rsa_pem),
            transport=transport,
        )
        try:
            refs = await _drain(
                c.discover(
                    SourceFilter(
                        include=("src/*",),
                        exclude=("src/*.go",),
                    ),
                    None,
                )
            )
            paths = {r.path for r in refs}
            # Filter is applied against the inner repo path (`src/a.py`)
            # before the slug prefix is prepended; only `src/a.py` keeps.
            assert paths == {"a/b/src/a.py"}
        finally:
            await c.close()

    async def test_404_repo_yields_nothing(self, rsa_pem: str) -> None:
        transport = httpx.MockTransport(make_handler([
            ("/access_tokens", lambda _: _access_token_response()),
            ("/git/trees/HEAD", lambda _: httpx.Response(404)),
        ]))
        c = GithubAppConnector(
            GithubAppConfig(repo="a/b"),
            credential=make_credential(rsa_pem),
            transport=transport,
        )
        try:
            refs = await _drain(c.discover(SourceFilter(), None))
            assert refs == []
        finally:
            await c.close()


# ---------------------------------------------------------------------
# discover — org enumeration via GraphQL
# ---------------------------------------------------------------------


class TestDiscoverOrg:
    async def test_paginated_org_repos_yielded(self, rsa_pem: str) -> None:
        # Two GraphQL pages, two repos each, then a tree fetch per repo.
        graphql_pages = iter([
            {
                "data": {
                    "organization": {
                        "repositories": {
                            "pageInfo": {"endCursor": "c1", "hasNextPage": True},
                            "nodes": [
                                {
                                    "name": "r1",
                                    "owner": {"login": "acme"},
                                    "isArchived": False,
                                    "pushedAt": "2024-01-01T00:00:00Z",
                                },
                                {
                                    "name": "r2",
                                    "owner": {"login": "acme"},
                                    "isArchived": True,  # filtered out
                                    "pushedAt": "2024-01-01T00:00:00Z",
                                },
                            ],
                        }
                    }
                }
            },
            {
                "data": {
                    "organization": {
                        "repositories": {
                            "pageInfo": {"endCursor": "c2", "hasNextPage": False},
                            "nodes": [
                                {
                                    "name": "r3",
                                    "owner": {"login": "acme"},
                                    "isArchived": False,
                                    "pushedAt": "2024-01-01T00:00:00Z",
                                },
                            ],
                        }
                    }
                }
            },
        ])

        def graphql(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=next(graphql_pages))

        def trees(request: httpx.Request) -> httpx.Response:
            # Each repo has one file.
            return httpx.Response(
                200,
                json={
                    "tree": [
                        {
                            "type": "blob",
                            "path": "x.txt",
                            "sha": str(request.url).split("/")[-3] + "_sha",
                            "size": 1,
                        }
                    ]
                },
            )

        transport = httpx.MockTransport(make_handler([
            ("/access_tokens", lambda _: _access_token_response()),
            ("/graphql", graphql),
            ("/git/trees/HEAD", trees),
        ]))
        c = GithubAppConnector(
            GithubAppConfig(org="acme", include_archived=False),
            credential=make_credential(rsa_pem),
            transport=transport,
        )
        try:
            refs = await _drain(c.discover(SourceFilter(), None))
            slugs = sorted({r.metadata["slug"] for r in refs})
            assert slugs == ["acme/r1", "acme/r3"]
            # _cursor must be present so the scheduler can resume.
            cursors = {r.metadata.get("_cursor") for r in refs}
            assert cursors <= {"c1", "c2"}
        finally:
            await c.close()

    async def test_include_archived_keeps_archived_repos(
        self, rsa_pem: str
    ) -> None:
        def graphql(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "organization": {
                            "repositories": {
                                "pageInfo": {
                                    "endCursor": None,
                                    "hasNextPage": False,
                                },
                                "nodes": [
                                    {
                                        "name": "r1",
                                        "owner": {"login": "acme"},
                                        "isArchived": True,
                                        "pushedAt": "2024-01-01T00:00:00Z",
                                    }
                                ],
                            }
                        }
                    }
                },
            )

        def trees(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"tree": [{"type": "blob", "path": "f", "sha": "s", "size": 1}]}
            )

        transport = httpx.MockTransport(make_handler([
            ("/access_tokens", lambda _: _access_token_response()),
            ("/graphql", graphql),
            ("/git/trees/HEAD", trees),
        ]))
        c = GithubAppConnector(
            GithubAppConfig(org="acme", include_archived=True),
            credential=make_credential(rsa_pem),
            transport=transport,
        )
        try:
            refs = await _drain(c.discover(SourceFilter(), None))
            assert {r.metadata["slug"] for r in refs} == {"acme/r1"}
        finally:
            await c.close()

    async def test_since_filter_short_circuits_on_older_repo(
        self, rsa_pem: str
    ) -> None:
        # PUSHED_AT DESC means the iterator must stop as soon as it
        # crosses the `since` threshold.
        def graphql(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "organization": {
                            "repositories": {
                                "pageInfo": {
                                    "endCursor": None,
                                    "hasNextPage": False,
                                },
                                "nodes": [
                                    {
                                        "name": "fresh",
                                        "owner": {"login": "acme"},
                                        "isArchived": False,
                                        "pushedAt": "2024-06-01T00:00:00Z",
                                    },
                                    {
                                        "name": "stale",
                                        "owner": {"login": "acme"},
                                        "isArchived": False,
                                        "pushedAt": "2020-01-01T00:00:00Z",
                                    },
                                ],
                            }
                        }
                    }
                },
            )

        def trees(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"tree": [{"type": "blob", "path": "f", "sha": "s", "size": 1}]},
            )

        transport = httpx.MockTransport(make_handler([
            ("/access_tokens", lambda _: _access_token_response()),
            ("/graphql", graphql),
            ("/git/trees/HEAD", trees),
        ]))
        c = GithubAppConnector(
            GithubAppConfig(org="acme"),
            credential=make_credential(rsa_pem),
            transport=transport,
        )
        try:
            refs = await _drain(
                c.discover(
                    SourceFilter(since=datetime(2023, 1, 1, tzinfo=UTC)),
                    None,
                )
            )
            slugs = {r.metadata["slug"] for r in refs}
            assert slugs == {"acme/fresh"}
        finally:
            await c.close()


# ---------------------------------------------------------------------
# discover — enterprise enumeration
# ---------------------------------------------------------------------


class TestDiscoverEnterprise:
    async def test_walks_each_org_in_enterprise(self, rsa_pem: str) -> None:
        # First GraphQL call is enterprise.organizations; subsequent
        # calls are organization.repositories per org.
        responses = iter([
            # enterprise.organizations
            {
                "data": {
                    "enterprise": {
                        "organizations": {
                            "pageInfo": {"endCursor": None, "hasNextPage": False},
                            "nodes": [{"login": "acme"}, {"login": "globex"}],
                        }
                    }
                }
            },
            # organization.repositories (acme)
            {
                "data": {
                    "organization": {
                        "repositories": {
                            "pageInfo": {"endCursor": None, "hasNextPage": False},
                            "nodes": [
                                {
                                    "name": "r1",
                                    "owner": {"login": "acme"},
                                    "isArchived": False,
                                    "pushedAt": "2024-01-01T00:00:00Z",
                                }
                            ],
                        }
                    }
                }
            },
            # organization.repositories (globex)
            {
                "data": {
                    "organization": {
                        "repositories": {
                            "pageInfo": {"endCursor": None, "hasNextPage": False},
                            "nodes": [
                                {
                                    "name": "r2",
                                    "owner": {"login": "globex"},
                                    "isArchived": False,
                                    "pushedAt": "2024-01-01T00:00:00Z",
                                }
                            ],
                        }
                    }
                }
            },
        ])

        def graphql(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=next(responses))

        def trees(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"tree": [{"type": "blob", "path": "f", "sha": "s", "size": 1}]},
            )

        transport = httpx.MockTransport(make_handler([
            ("/access_tokens", lambda _: _access_token_response()),
            ("/graphql", graphql),
            ("/git/trees/HEAD", trees),
        ]))
        c = GithubAppConnector(
            GithubAppConfig(enterprise="acme-inc"),
            credential=make_credential(rsa_pem),
            transport=transport,
        )
        try:
            refs = await _drain(c.discover(SourceFilter(), None))
            slugs = sorted({r.metadata["slug"] for r in refs})
            assert slugs == ["acme/r1", "globex/r2"]
        finally:
            await c.close()

    async def test_enterprise_pagination(self, rsa_pem: str) -> None:
        # Two enterprise pages of orgs.
        responses = iter([
            {
                "data": {
                    "enterprise": {
                        "organizations": {
                            "pageInfo": {"endCursor": "p1", "hasNextPage": True},
                            "nodes": [{"login": "a"}],
                        }
                    }
                }
            },
            {
                "data": {
                    "organization": {
                        "repositories": {
                            "pageInfo": {"endCursor": None, "hasNextPage": False},
                            "nodes": [],
                        }
                    }
                }
            },
            {
                "data": {
                    "enterprise": {
                        "organizations": {
                            "pageInfo": {"endCursor": None, "hasNextPage": False},
                            "nodes": [{"login": "b"}],
                        }
                    }
                }
            },
            {
                "data": {
                    "organization": {
                        "repositories": {
                            "pageInfo": {"endCursor": None, "hasNextPage": False},
                            "nodes": [],
                        }
                    }
                }
            },
        ])

        def graphql(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=next(responses))

        transport = httpx.MockTransport(make_handler([
            ("/access_tokens", lambda _: _access_token_response()),
            ("/graphql", graphql),
        ]))
        c = GithubAppConnector(
            GithubAppConfig(enterprise="acme-inc"),
            credential=make_credential(rsa_pem),
            transport=transport,
        )
        try:
            refs = await _drain(c.discover(SourceFilter(), None))
            assert refs == []  # no repos in either org
        finally:
            await c.close()


# ---------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------


class TestFetch:
    async def test_returns_decoded_blob(self, rsa_pem: str) -> None:
        def blob(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"content": _b64("password=hunter2\n"), "encoding": "base64"},
            )

        transport = httpx.MockTransport(make_handler([
            ("/access_tokens", lambda _: _access_token_response()),
            ("/git/blobs/", blob),
        ]))
        c = GithubAppConnector(
            GithubAppConfig(repo="a/b"),
            credential=make_credential(rsa_pem),
            transport=transport,
        )
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind=c.kind,
                path="a/b/x",
                metadata={"owner": "a", "repo": "b", "blob_sha": "deadbeef"},
            )
            docs = [d async for d in c.fetch(ref)]
            assert len(docs) == 1
            assert isinstance(docs[0], Document)
            assert docs[0].text == "password=hunter2\n"
            assert docs[0].content_hash == "deadbeef"
        finally:
            await c.close()

    async def test_fetch_missing_metadata_returns_empty(
        self, rsa_pem: str
    ) -> None:
        # Connector still needs to mint a token (eager refresh) before
        # bailing out; the mock transport must answer that one call.
        def access_token(_: httpx.Request) -> httpx.Response:
            return _access_token_response()

        transport = httpx.MockTransport(make_handler([
            ("/access_tokens", access_token),
        ]))
        c = GithubAppConnector(
            GithubAppConfig(repo="a/b"),
            credential=make_credential(rsa_pem),
            transport=transport,
        )
        try:
            ghost = DocumentRef(
                source_id=c.id,
                source_kind=c.kind,
                path="x",
            )
            docs = [d async for d in c.fetch(ghost)]
            assert docs == []
        finally:
            await c.close()

    async def test_fetch_404_blob_returns_empty(self, rsa_pem: str) -> None:
        transport = httpx.MockTransport(make_handler([
            ("/access_tokens", lambda _: _access_token_response()),
            ("/git/blobs/", lambda _: httpx.Response(404)),
        ]))
        c = GithubAppConnector(
            GithubAppConfig(repo="a/b"),
            credential=make_credential(rsa_pem),
            transport=transport,
        )
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind=c.kind,
                path="x",
                metadata={"owner": "a", "repo": "b", "blob_sha": "x"},
            )
            assert [d async for d in c.fetch(ref)] == []
        finally:
            await c.close()

    async def test_fetch_undecodable_base64_returns_empty(
        self, rsa_pem: str
    ) -> None:
        def blob(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"content": "!!!not_base64!!!"})

        transport = httpx.MockTransport(make_handler([
            ("/access_tokens", lambda _: _access_token_response()),
            ("/git/blobs/", blob),
        ]))
        c = GithubAppConnector(
            GithubAppConfig(repo="a/b"),
            credential=make_credential(rsa_pem),
            transport=transport,
        )
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind=c.kind,
                path="x",
                metadata={"owner": "a", "repo": "b", "blob_sha": "s"},
            )
            assert [d async for d in c.fetch(ref)] == []
        finally:
            await c.close()


# ---------------------------------------------------------------------
# fetch_repo_tarball
# ---------------------------------------------------------------------


class TestFetchTarball:
    async def test_returns_bytes_under_ceiling(self, rsa_pem: str) -> None:
        body = b"x" * 1024

        def tarball(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body)

        transport = httpx.MockTransport(make_handler([
            ("/access_tokens", lambda _: _access_token_response()),
            ("/tarball", tarball),
        ]))
        c = GithubAppConnector(
            GithubAppConfig(repo="a/b"),
            credential=make_credential(rsa_pem),
            transport=transport,
        )
        try:
            content = await c.fetch_repo_tarball("a", "b")
            assert content == body
        finally:
            await c.close()

    async def test_returns_none_above_ceiling(self, rsa_pem: str) -> None:
        body = b"x" * (_TARBALL_BYTE_CEILING + 1)

        def tarball(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body)

        transport = httpx.MockTransport(make_handler([
            ("/access_tokens", lambda _: _access_token_response()),
            ("/tarball", tarball),
        ]))
        c = GithubAppConnector(
            GithubAppConfig(repo="a/b"),
            credential=make_credential(rsa_pem),
            transport=transport,
        )
        try:
            assert await c.fetch_repo_tarball("a", "b") is None
        finally:
            await c.close()

    async def test_returns_none_on_404(self, rsa_pem: str) -> None:
        transport = httpx.MockTransport(make_handler([
            ("/access_tokens", lambda _: _access_token_response()),
            ("/tarball", lambda _: httpx.Response(404)),
        ]))
        c = GithubAppConnector(
            GithubAppConfig(repo="a/b"),
            credential=make_credential(rsa_pem),
            transport=transport,
        )
        try:
            assert await c.fetch_repo_tarball("a", "b") is None
        finally:
            await c.close()


# ---------------------------------------------------------------------
# rate-limit propagation
# ---------------------------------------------------------------------


class TestRateLimitPropagation:
    async def test_secondary_429_during_discover_surfaces_rate_limited(
        self, rsa_pem: str
    ) -> None:
        transport = httpx.MockTransport(make_handler([
            ("/access_tokens", lambda _: _access_token_response()),
            ("/git/trees/HEAD", lambda _: httpx.Response(429, headers={"Retry-After": "5"})),
        ]))
        c = GithubAppConnector(
            GithubAppConfig(repo="a/b"),
            credential=make_credential(rsa_pem),
            transport=transport,
        )
        try:
            with pytest.raises(RateLimited):
                await _drain(c.discover(SourceFilter(), None))
        finally:
            await c.close()


# ---------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------


class TestHelpers:
    def test_split_slug(self) -> None:
        assert _split_slug("a/b") == ("a", "b")

    def test_split_slug_rejects_url(self) -> None:
        with pytest.raises(ValueError, match="owner/name"):
            _split_slug("https://github.com/a/b.git")

    def test_split_slug_rejects_no_slash(self) -> None:
        with pytest.raises(ValueError, match="owner/name"):
            _split_slug("just-a-name")

    def test_split_slug_rejects_empty_parts(self) -> None:
        with pytest.raises(ValueError, match="malformed"):
            _split_slug("/b")
        with pytest.raises(ValueError, match="malformed"):
            _split_slug("a/")

    def test_parse_iso_z_suffix(self) -> None:
        ts = _parse_iso("2024-01-01T00:00:00Z")
        assert ts.tzinfo is not None
        assert ts.year == 2024

    def test_glob_match(self) -> None:
        # `_glob_match` is a thin fnmatch wrapper. fnmatch does not treat
        # `/` as a path separator (Python stdlib semantics), so `*` will
        # span directories. Connectors that need POSIX-glob semantics
        # should pre-decompose the path; we deliberately keep the same
        # behavior as the builtin `dir` connector for parity.
        assert _glob_match("a/b/c.py", "a/*/*.py") is True
        assert _glob_match("a/b/c.py", "*.py") is True
        assert _glob_match("a/b/c.go", "*.py") is False


# ---------------------------------------------------------------------
# SPEC + factory
# ---------------------------------------------------------------------


class TestSpec:
    def test_spec_metadata(self) -> None:
        assert SPEC.kind == "github-app"
        assert KIND == "github-app"
        assert SPEC.required_scopes == ("contents:read", "metadata:read")
        assert SPEC.capabilities.incremental is True
        assert SPEC.capabilities.content_hash_delta is True

    def test_factory_builds_connector_with_credential(
        self, rsa_pem: str
    ) -> None:
        cred = make_credential(rsa_pem)
        c = SPEC.factory(
            {
                "repo": "a/b",
                "_credential": cred,
                "base_url": "https://ghe.example.com/api/v3",
                "include_archived": True,
                "id": "my-source",
            }
        )
        assert isinstance(c, GithubAppConnector)
        assert c.id == "my-source"

    def test_factory_org_target(self, rsa_pem: str) -> None:
        cred = make_credential(rsa_pem)
        c = SPEC.factory({"org": "acme", "_credential": cred})
        assert isinstance(c, GithubAppConnector)
        assert c.id == "github-org:acme"

    def test_factory_enterprise_target(self, rsa_pem: str) -> None:
        cred = make_credential(rsa_pem)
        c = SPEC.factory({"enterprise": "acme-inc", "_credential": cred})
        assert isinstance(c, GithubAppConnector)
        assert c.id == "github-enterprise:acme-inc"

    def test_factory_requires_credential(self) -> None:
        with pytest.raises(ValueError, match="Credential"):
            SPEC.factory({"repo": "a/b"})


# ---------------------------------------------------------------------
# package __init__ re-exports
# ---------------------------------------------------------------------


class TestPackageInit:
    def test_top_level_exports(self) -> None:
        import pleno_pii_scanner_github as pkg

        assert pkg.SPEC is SPEC
        assert pkg.KIND == "github-app"
        assert pkg.GithubAppConfig is GithubAppConfig
        assert pkg.GithubAppConnector is GithubAppConnector
        assert hasattr(pkg, "AppAuth")
        assert hasattr(pkg, "mint_app_jwt")
        assert pkg.__version__ == "0.1.0"
