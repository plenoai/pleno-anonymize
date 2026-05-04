"""Tests for AzureDevOpsConnector — discover/fetch/lifecycle/factory.

Two flavors are tested side-by-side. The HTTP layer is exercised
through `httpx.MockTransport`; the clone layer is replaced by a
deterministic `clone_fn` that returns a pre-built directory the
`make_repo` fixture seeds. The `enumerate_fn` seam is also exercised
to demonstrate that connector-level integration tests can bypass
HTTP entirely when they want to.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from pleno_pii_scanner.sources import (
    Capabilities,
    Document,
    DocumentRef,
    SourceConnector,
    SourceFilter,
    create,
    register,
)
from pleno_pii_scanner_azure_devops import (
    KIND,
    SPEC,
    AzureDevOpsConfig,
    AzureDevOpsConnector,
    AzureDevOpsAuth,
)
from pleno_pii_scanner_azure_devops.api import CONTINUATION_TOKEN_HEADER
from pleno_pii_scanner_azure_devops.connector import (
    _build_auth,
    _clone_into_tempdir,
)


# ----- helpers -----------------------------------------------------------


def _stub_clone(path_factory):
    """Return a `clone_fn` that ignores the URL and returns a fixed path.

    `path_factory` may be a callable `(slug)->Path` or a Path itself.
    """

    def fn(clone_url: str, _config: AzureDevOpsConfig, auth_header: str) -> Path:
        # Sanity assertion: caller MUST pass an Authorization header
        # value (Bearer or Basic) — surface mistakes loudly.
        assert auth_header.startswith(("Bearer ", "Basic "))
        # Identify the repo by the trailing path segment of the URL.
        slug = clone_url.rstrip("/").rsplit("/", 1)[-1]
        return (
            path_factory(slug) if callable(path_factory) else path_factory
        )

    return fn


def _projects_response(
    names: list[str], *, continuation: str | None = None
) -> httpx.Response:
    headers = (
        {CONTINUATION_TOKEN_HEADER: continuation} if continuation else {}
    )
    return httpx.Response(
        200,
        json={"value": [{"name": n, "visibility": "private"} for n in names]},
        headers=headers,
    )


def _repos_response(
    repos: list[dict[str, Any]],
) -> httpx.Response:
    return httpx.Response(200, json={"value": repos})


# ----- config validation -------------------------------------------------


class TestConfig:
    def test_services_requires_org_or_base_url(self) -> None:
        with pytest.raises(ValueError, match="organization"):
            AzureDevOpsConfig(flavor="services")

    def test_services_with_org_resolves_dev_azure(self) -> None:
        cfg = AzureDevOpsConfig(flavor="services", organization="contoso")
        assert cfg.resolved_base_url() == "https://dev.azure.com/contoso"

    def test_services_with_explicit_base_url_overrides(self) -> None:
        cfg = AzureDevOpsConfig(
            flavor="services",
            organization="contoso",
            base_url="https://msee.example/contoso",
        )
        assert cfg.resolved_base_url() == "https://msee.example/contoso"

    def test_server_requires_base_url(self) -> None:
        with pytest.raises(ValueError, match="base_url"):
            AzureDevOpsConfig(flavor="server")

    def test_server_resolves_base_url(self) -> None:
        cfg = AzureDevOpsConfig(
            flavor="server",
            base_url="https://tfs.internal/DefaultCollection/",
        )
        assert cfg.resolved_base_url() == "https://tfs.internal/DefaultCollection"

    def test_invalid_flavor(self) -> None:
        with pytest.raises(ValueError, match="flavor"):
            AzureDevOpsConfig(flavor="garbage", organization="x")

    def test_resolved_id_services_default(self) -> None:
        cfg = AzureDevOpsConfig(flavor="services", organization="contoso")
        assert cfg.resolved_id() == "azure-devops:contoso"

    def test_resolved_id_server(self) -> None:
        cfg = AzureDevOpsConfig(
            flavor="server", base_url="https://tfs/Coll"
        )
        assert "azure-devops-server:" in cfg.resolved_id()

    def test_resolved_id_explicit(self) -> None:
        cfg = AzureDevOpsConfig(
            flavor="services", organization="x", id="custom"
        )
        assert cfg.resolved_id() == "custom"


# ----- discover/fetch over Services flavor -------------------------------


class TestServicesDiscoverFetch:
    async def test_single_project_lists_repos_and_clones(
        self, make_repo
    ) -> None:
        # One project shortcut: skip the projects-list call entirely.
        # Repo response contains one enabled repo + one disabled repo;
        # default config skips the disabled one.
        captured: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(str(request.url))
            assert "/Banking/_apis/git/repositories" in str(request.url)
            return _repos_response(
                [
                    {
                        "name": "core",
                        "remoteUrl": "https://dev.azure.com/contoso/Banking/_git/core",
                        "isDisabled": False,
                    },
                    {
                        "name": "legacy",
                        "remoteUrl": "https://dev.azure.com/contoso/Banking/_git/legacy",
                        "isDisabled": True,
                    },
                ]
            )

        repo = make_repo("core", {"app.py": "secret = 1"})
        connector = AzureDevOpsConnector(
            AzureDevOpsConfig(
                flavor="services",
                organization="contoso",
                project="Banking",
            ),
            auth=AzureDevOpsAuth.pat("ado-pat"),
            transport=httpx.MockTransport(handler),
            clone_fn=_stub_clone(lambda _slug: repo),
        )
        try:
            refs = [
                r async for r in connector.discover(SourceFilter(), None)
            ]
            assert len(refs) == 1
            ref = refs[0]
            assert ref.path == "Banking/core/app.py"
            assert ref.metadata["project"] == "Banking"
            assert ref.metadata["repo"] == "core"
            assert ref.metadata["slug"] == "Banking/core"
            assert ref.parent_chain == ("azure-devops://Banking/core",)
        finally:
            await connector.close()

    async def test_include_disabled_yields_disabled_repos(
        self, make_repo
    ) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return _repos_response(
                [
                    {
                        "name": "old",
                        "remoteUrl": "https://dev.azure.com/contoso/Banking/_git/old",
                        "isDisabled": True,
                    }
                ]
            )

        repo = make_repo("old", {"x.py": "1"})
        connector = AzureDevOpsConnector(
            AzureDevOpsConfig(
                flavor="services",
                organization="contoso",
                project="Banking",
                include_disabled=True,
            ),
            auth=AzureDevOpsAuth.pat("p"),
            transport=httpx.MockTransport(handler),
            clone_fn=_stub_clone(lambda _: repo),
        )
        try:
            refs = [r async for r in connector.discover(SourceFilter(), None)]
            assert any("old" in r.path for r in refs)
        finally:
            await connector.close()

    async def test_full_org_paginated_projects(self, make_repo) -> None:
        # First page of /_apis/projects has continuation header; second
        # page does not. Each project then makes one repos call.
        repo = make_repo("svc", {"f.txt": "hello"})

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/_apis/projects" in url and "continuationToken=tok-2" in url:
                return _projects_response(["P2"])
            if "/_apis/projects" in url:
                return _projects_response(["P1"], continuation="tok-2")
            if "/P1/_apis/git/repositories" in url:
                return _repos_response(
                    [
                        {
                            "name": "svc",
                            "remoteUrl": "https://dev/contoso/P1/_git/svc",
                        }
                    ]
                )
            if "/P2/_apis/git/repositories" in url:
                return _repos_response(
                    [
                        {
                            "name": "svc",
                            "remoteUrl": "https://dev/contoso/P2/_git/svc",
                        }
                    ]
                )
            raise AssertionError(f"unexpected url {url}")

        connector = AzureDevOpsConnector(
            AzureDevOpsConfig(flavor="services", organization="contoso"),
            auth=AzureDevOpsAuth.pat("p"),
            transport=httpx.MockTransport(handler),
            clone_fn=_stub_clone(lambda _: repo),
        )
        try:
            refs = [r async for r in connector.discover(SourceFilter(), None)]
            projects = {r.metadata["project"] for r in refs}
            assert projects == {"P1", "P2"}
        finally:
            await connector.close()

    async def test_resume_from_cursor(self, make_repo) -> None:
        # Pass a starting cursor: first /_apis/projects request must
        # carry it in the query string.
        seen_urls: list[str] = []
        repo = make_repo("svc", {"f.txt": "x"})

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            seen_urls.append(url)
            if "/_apis/projects" in url:
                return _projects_response(["P3"])
            if "/_apis/git/repositories" in url:
                return _repos_response(
                    [
                        {
                            "name": "svc",
                            "remoteUrl": "https://x/P3/_git/svc",
                        }
                    ]
                )
            raise AssertionError(url)

        connector = AzureDevOpsConnector(
            AzureDevOpsConfig(flavor="services", organization="contoso"),
            auth=AzureDevOpsAuth.pat("p"),
            transport=httpx.MockTransport(handler),
            clone_fn=_stub_clone(lambda _: repo),
        )
        try:
            _ = [r async for r in connector.discover(SourceFilter(), "RESUME")]
            assert any("continuationToken=RESUME" in u for u in seen_urls)
        finally:
            await connector.close()

    async def test_404_on_repos_listing_skips_project(
        self, make_repo
    ) -> None:
        # Race: project disappears between projects-list and repos-list.
        # Connector must skip silently and continue (no exception).
        repo = make_repo("a", {"f.txt": "x"})
        first_project_returns_404 = {"flag": True}

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/_apis/projects" in url:
                return _projects_response(["P1", "P2"])
            if "/P1/_apis/git/repositories" in url:
                first_project_returns_404["flag"] = False
                return httpx.Response(404)
            if "/P2/_apis/git/repositories" in url:
                return _repos_response(
                    [
                        {
                            "name": "a",
                            "remoteUrl": "https://x/P2/_git/a",
                        }
                    ]
                )
            raise AssertionError(url)

        connector = AzureDevOpsConnector(
            AzureDevOpsConfig(flavor="services", organization="contoso"),
            auth=AzureDevOpsAuth.pat("p"),
            transport=httpx.MockTransport(handler),
            clone_fn=_stub_clone(lambda _: repo),
        )
        try:
            refs = [r async for r in connector.discover(SourceFilter(), None)]
            assert all(r.metadata["project"] == "P2" for r in refs)
        finally:
            await connector.close()

    async def test_500_on_repos_listing_raises(self) -> None:
        from pleno_pii_scanner_azure_devops.api import AzureDevOpsApiError

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/_apis/git/repositories" in url:
                return httpx.Response(500, text="boom")
            return _projects_response(["P1"])

        connector = AzureDevOpsConnector(
            AzureDevOpsConfig(flavor="services", organization="contoso"),
            auth=AzureDevOpsAuth.pat("p"),
            transport=httpx.MockTransport(handler),
        )
        try:
            with pytest.raises(AzureDevOpsApiError, match="500"):
                async for _ in connector.discover(SourceFilter(), None):
                    pass
        finally:
            await connector.close()

    async def test_malformed_project_entry_skipped(self, make_repo) -> None:
        # `name` missing from a project entry must not crash the scan.
        repo = make_repo("svc", {"f.txt": "x"})

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/_apis/projects" in url:
                return httpx.Response(
                    200,
                    json={
                        "value": [
                            {"visibility": "private"},  # missing name
                            {"name": "Good", "visibility": "private"},
                        ]
                    },
                )
            return _repos_response(
                [
                    {
                        "name": "svc",
                        "remoteUrl": "https://x/Good/_git/svc",
                    }
                ]
            )

        connector = AzureDevOpsConnector(
            AzureDevOpsConfig(flavor="services", organization="contoso"),
            auth=AzureDevOpsAuth.pat("p"),
            transport=httpx.MockTransport(handler),
            clone_fn=_stub_clone(lambda _: repo),
        )
        try:
            refs = [r async for r in connector.discover(SourceFilter(), None)]
            assert {r.metadata["project"] for r in refs} == {"Good"}
        finally:
            await connector.close()

    async def test_malformed_repo_entry_skipped(self, make_repo) -> None:
        # Repo missing remoteUrl is dropped silently.
        repo = make_repo("ok", {"f.txt": "x"})

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/_apis/projects" in url:
                return _projects_response(["P"])
            return _repos_response(
                [
                    {"name": "noUrl"},
                    {"name": "ok", "remoteUrl": "https://x/P/_git/ok"},
                ]
            )

        connector = AzureDevOpsConnector(
            AzureDevOpsConfig(flavor="services", organization="contoso"),
            auth=AzureDevOpsAuth.pat("p"),
            transport=httpx.MockTransport(handler),
            clone_fn=_stub_clone(lambda _: repo),
        )
        try:
            refs = [r async for r in connector.discover(SourceFilter(), None)]
            assert {r.metadata["repo"] for r in refs} == {"ok"}
        finally:
            await connector.close()

    async def test_visibility_filter_drops_private_when_disabled(
        self, make_repo
    ) -> None:
        repo = make_repo("svc", {"f.txt": "x"})

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/_apis/projects" in url:
                return httpx.Response(
                    200,
                    json={
                        "value": [
                            {"name": "Pub", "visibility": "public"},
                            {"name": "Priv", "visibility": "private"},
                        ]
                    },
                )
            project = url.split("/_apis/")[0].rsplit("/", 1)[-1]
            return _repos_response(
                [
                    {
                        "name": "svc",
                        "remoteUrl": f"https://x/{project}/_git/svc",
                    }
                ]
            )

        connector = AzureDevOpsConnector(
            AzureDevOpsConfig(
                flavor="services",
                organization="contoso",
                include_private=False,
            ),
            auth=AzureDevOpsAuth.pat("p"),
            transport=httpx.MockTransport(handler),
            clone_fn=_stub_clone(lambda _: repo),
        )
        try:
            refs = [r async for r in connector.discover(SourceFilter(), None)]
            assert {r.metadata["project"] for r in refs} == {"Pub"}
        finally:
            await connector.close()


# ----- discover/fetch over Server flavor -------------------------------


class TestServerDiscoverFetch:
    async def test_server_uses_explicit_base_url(self, make_repo) -> None:
        seen_urls: list[str] = []
        repo = make_repo("api", {"app.py": "x"})

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            seen_urls.append(url)
            if "/_apis/projects" in url:
                return _projects_response(["Banking"])
            return _repos_response(
                [
                    {
                        "name": "api",
                        "remoteUrl": "https://tfs.internal/Coll/Banking/_git/api",
                    }
                ]
            )

        connector = AzureDevOpsConnector(
            AzureDevOpsConfig(
                flavor="server",
                base_url="https://tfs.internal/Coll",
            ),
            auth=AzureDevOpsAuth.pat("p"),
            transport=httpx.MockTransport(handler),
            clone_fn=_stub_clone(lambda _: repo),
        )
        try:
            refs = [r async for r in connector.discover(SourceFilter(), None)]
            assert all(
                u.startswith("https://tfs.internal/Coll/") for u in seen_urls
            )
            assert refs and refs[0].native_url is not None
            assert "https://tfs.internal/Coll/" in refs[0].native_url
        finally:
            await connector.close()

    async def test_server_native_url_includes_project(
        self, make_repo
    ) -> None:
        repo = make_repo("api", {"app.py": "x"})

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/_apis/projects" in url:
                return _projects_response(["Banking"])
            return _repos_response(
                [
                    {
                        "name": "api",
                        "remoteUrl": "https://tfs/Coll/Banking/_git/api",
                    }
                ]
            )

        connector = AzureDevOpsConnector(
            AzureDevOpsConfig(
                flavor="server",
                base_url="https://tfs.internal/Coll",
            ),
            auth=AzureDevOpsAuth.pat("p"),
            transport=httpx.MockTransport(handler),
            clone_fn=_stub_clone(lambda _: repo),
        )
        try:
            refs = [r async for r in connector.discover(SourceFilter(), None)]
            assert "?path=app.py" in refs[0].native_url
        finally:
            await connector.close()


# ----- fetch path --------------------------------------------------------


class TestFetch:
    async def test_fetch_yields_document_for_known_ref(
        self, make_repo
    ) -> None:
        repo = make_repo("svc", {"a.txt": "alpha", "b.txt": "beta"})

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/_apis/git/repositories" in url:
                return _repos_response(
                    [
                        {
                            "name": "svc",
                            "remoteUrl": "https://x/P/_git/svc",
                        }
                    ]
                )
            raise AssertionError(url)

        connector = AzureDevOpsConnector(
            AzureDevOpsConfig(
                flavor="services", organization="contoso", project="P"
            ),
            auth=AzureDevOpsAuth.pat("p"),
            transport=httpx.MockTransport(handler),
            clone_fn=_stub_clone(lambda _: repo),
        )
        try:
            refs = [r async for r in connector.discover(SourceFilter(), None)]
            ref = next(r for r in refs if r.metadata["inner_path"] == "a.txt")
            docs: list[Document] = [d async for d in connector.fetch(ref)]
            assert len(docs) == 1
            assert docs[0].text == "alpha"
            # Outer ref preserved (not the dir's inner ref)
            assert docs[0].ref.path == "P/svc/a.txt"
        finally:
            await connector.close()

    async def test_fetch_unknown_ref_yields_empty(self) -> None:
        connector = AzureDevOpsConnector(
            AzureDevOpsConfig(
                flavor="services", organization="x", project="P"
            ),
            auth=AzureDevOpsAuth.pat("p"),
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"value": []})
            ),
        )
        try:
            stale = DocumentRef(
                source_id=connector.id,
                source_kind=connector.kind,
                path="P/never/file.txt",
                metadata={"slug": "P/never", "inner_path": "file.txt"},
            )
            docs = [d async for d in connector.fetch(stale)]
            assert docs == []
        finally:
            await connector.close()

    async def test_fetch_missing_metadata_yields_empty(self) -> None:
        connector = AzureDevOpsConnector(
            AzureDevOpsConfig(
                flavor="services", organization="x", project="P"
            ),
            auth=AzureDevOpsAuth.pat("p"),
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"value": []})
            ),
        )
        try:
            stale = DocumentRef(
                source_id=connector.id,
                source_kind=connector.kind,
                path="x",
                metadata={},  # no slug, no inner_path
            )
            docs = [d async for d in connector.fetch(stale)]
            assert docs == []
        finally:
            await connector.close()


# ----- enumerate_fn seam -----------------------------------------------


class TestEnumerateSeam:
    async def test_enumerate_fn_replaces_http_enumeration(
        self, make_repo
    ) -> None:
        repo = make_repo("only", {"f.txt": "x"})

        async def fake_enum(
            connector: AzureDevOpsConnector,
            filter: SourceFilter,
            cursor,
        ) -> AsyncIterator[tuple[str, str, str, bool]]:
            del filter, cursor
            yield ("Acme", "only", "https://x/Acme/_git/only", False)

        connector = AzureDevOpsConnector(
            AzureDevOpsConfig(
                flavor="services", organization="x"
            ),
            auth=AzureDevOpsAuth.pat("p"),
            transport=httpx.MockTransport(
                lambda _: httpx.Response(500)
            ),  # would error if HTTP path is hit
            clone_fn=_stub_clone(lambda _: repo),
            enumerate_fn=fake_enum,
        )
        try:
            refs = [r async for r in connector.discover(SourceFilter(), None)]
            assert refs and refs[0].metadata["project"] == "Acme"
        finally:
            await connector.close()

    async def test_enumerate_fn_disabled_filtered(
        self, make_repo
    ) -> None:
        repo = make_repo("svc", {"f.txt": "x"})

        async def enum(connector, filter, cursor):
            yield ("P", "svc", "https://x/P/_git/svc", True)  # disabled

        connector = AzureDevOpsConnector(
            AzureDevOpsConfig(flavor="services", organization="x"),
            auth=AzureDevOpsAuth.pat("p"),
            transport=httpx.MockTransport(lambda _: httpx.Response(404)),
            clone_fn=_stub_clone(lambda _: repo),
            enumerate_fn=enum,
        )
        try:
            refs = [r async for r in connector.discover(SourceFilter(), None)]
            assert refs == []
        finally:
            await connector.close()


# ----- clone caching / lifecycle ---------------------------------------


class TestCloneLifecycle:
    async def test_clone_called_once_per_slug(self, make_repo) -> None:
        # Two refs from the same repo => clone_fn invoked exactly once.
        repo = make_repo("svc", {"a.txt": "1", "b.txt": "2"})
        n = {"calls": 0}

        def counting(clone_url, _config, _auth_header):
            n["calls"] += 1
            return repo

        def handler(request: httpx.Request) -> httpx.Response:
            return _repos_response(
                [
                    {
                        "name": "svc",
                        "remoteUrl": "https://x/P/_git/svc",
                    }
                ]
            )

        connector = AzureDevOpsConnector(
            AzureDevOpsConfig(
                flavor="services", organization="x", project="P"
            ),
            auth=AzureDevOpsAuth.pat("p"),
            transport=httpx.MockTransport(handler),
            clone_fn=counting,
        )
        try:
            refs = [r async for r in connector.discover(SourceFilter(), None)]
            assert len(refs) == 2
            assert n["calls"] == 1
        finally:
            await connector.close()

    async def test_close_drops_tempdirs_and_clients(
        self, tmp_path: Path
    ) -> None:
        # We use a real tmp directory (not the make_repo one) and check
        # rmtree happens; the stub returns this dir and after close()
        # it must be gone.
        clone_dir = tmp_path / "clone"
        clone_dir.mkdir()
        (clone_dir / "f.txt").write_text("x")

        def handler(request: httpx.Request) -> httpx.Response:
            return _repos_response(
                [
                    {
                        "name": "svc",
                        "remoteUrl": "https://x/P/_git/svc",
                    }
                ]
            )

        connector = AzureDevOpsConnector(
            AzureDevOpsConfig(
                flavor="services", organization="x", project="P"
            ),
            auth=AzureDevOpsAuth.pat("p"),
            transport=httpx.MockTransport(handler),
            clone_fn=lambda *_: clone_dir,
        )
        # Trigger a discover so the clone is registered.
        _ = [r async for r in connector.discover(SourceFilter(), None)]
        await connector.close()
        # Tempdir cleanup is best-effort (ignore_errors=True). We
        # verify the bookkeeping cleared the cache.
        assert connector._clones == {}
        assert connector._tempdirs == []

    async def test_close_idempotent(self) -> None:
        connector = AzureDevOpsConnector(
            AzureDevOpsConfig(
                flavor="services", organization="x"
            ),
            auth=AzureDevOpsAuth.pat("p"),
        )
        await connector.close()
        await connector.close()


# ----- protocol + capabilities -----------------------------------------


class TestProtocol:
    def test_runtime_isinstance(self) -> None:
        c = AzureDevOpsConnector(
            AzureDevOpsConfig(flavor="services", organization="x"),
            auth=AzureDevOpsAuth.pat("p"),
        )
        assert isinstance(c, SourceConnector)

    def test_capabilities(self) -> None:
        c = AzureDevOpsConnector(
            AzureDevOpsConfig(flavor="services", organization="x"),
            auth=AzureDevOpsAuth.pat("p"),
        )
        caps = c.capabilities()
        assert caps == Capabilities(
            incremental=False,
            binary=False,
            content_hash_delta=False,
            max_concurrent_fetches=4,
            streaming=False,
        )


# ----- factory + spec --------------------------------------------------


class TestFactory:
    def test_metadata(self) -> None:
        assert SPEC.kind == KIND == "azure_devops"
        assert SPEC.version == "0.1.0"
        assert "vso.code" in SPEC.required_scopes
        assert "Services" in SPEC.description

    def test_factory_pat(self) -> None:
        register(SPEC)
        c = create(
            "azure_devops",
            {
                "organization": "contoso",
                "_credential": {"mode": "pat", "pat": "tok"},
            },
        )
        assert isinstance(c, AzureDevOpsConnector)
        assert c.id == "azure-devops:contoso"

    def test_factory_oauth(self) -> None:
        register(SPEC)
        c = create(
            "azure_devops",
            {
                "organization": "contoso",
                "_credential": {
                    "mode": "oauth",
                    "access_token": "ey.x",
                },
            },
        )
        assert isinstance(c, AzureDevOpsConnector)

    def test_factory_federated(self, tmp_path: Path) -> None:
        register(SPEC)
        token_path = tmp_path / "oidc"
        token_path.write_text("ey.x")
        c = create(
            "azure_devops",
            {
                "organization": "contoso",
                "_credential": {
                    "mode": "federated",
                    "oidc_token_path": str(token_path),
                    "tenant_id": "t",
                    "client_id": "cid",
                },
            },
        )
        assert isinstance(c, AzureDevOpsConnector)

    def test_factory_server_full_config(self, tmp_path: Path) -> None:
        register(SPEC)
        ca = tmp_path / "ca.pem"
        ca.write_text("dummy")
        c = create(
            "azure_devops",
            {
                "flavor": "server",
                "base_url": "https://tfs/Coll",
                "ca_bundle_path": str(ca),
                "project": "Banking",
                "include_disabled": True,
                "include_private": False,
                "api_version": "6.0",
                "id": "tfs-banking",
                "_credential": {"mode": "pat", "pat": "x"},
            },
        )
        assert c.id == "tfs-banking"
        assert c._config.flavor == "server"
        assert c._config.api_version == "6.0"

    def test_factory_rejects_missing_credential(self) -> None:
        with pytest.raises(ValueError, match="_credential"):
            SPEC.factory({"organization": "x"})

    def test_factory_rejects_unknown_mode(self) -> None:
        with pytest.raises(ValueError, match="mode"):
            SPEC.factory(
                {
                    "organization": "x",
                    "_credential": {"mode": "garbage"},
                }
            )

    def test_build_auth_pat_validation(self) -> None:
        with pytest.raises(ValueError, match="pat"):
            _build_auth({"mode": "pat"})

    def test_build_auth_oauth_validation(self) -> None:
        with pytest.raises(ValueError, match="access_token"):
            _build_auth({"mode": "oauth"})

    def test_build_auth_federated_validation(self) -> None:
        with pytest.raises(ValueError, match="oidc_token_path"):
            _build_auth({"mode": "federated", "tenant_id": "t"})


# ----- _clone_into_tempdir (subprocess wrapper) -------------------------


class TestCloneIntoTempdir:
    def test_subprocess_called_with_extraheader_and_depth_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Replace subprocess.run so we don't actually shell out, but
        # capture the command for assertion. Also install a fake
        # mkdtemp so we control the destination path.
        captured: dict[str, Any] = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return None

        from pleno_pii_scanner_azure_devops import connector as mod

        monkeypatch.setattr(mod.subprocess, "run", fake_run)

        path = _clone_into_tempdir(
            "https://dev/contoso/P/_git/svc",
            AzureDevOpsConfig(
                flavor="services", organization="contoso"
            ),
            "Bearer xyz",
        )
        assert path.exists()
        assert any("Bearer xyz" in part for part in captured["cmd"])
        assert "--depth=1" in captured["cmd"]
        assert "https://dev/contoso/P/_git/svc" in captured["cmd"]
        # CA bundle not configured -> no http.sslCAInfo flag.
        assert not any(
            "http.sslCAInfo" in part for part in captured["cmd"]
        )

    def test_subprocess_with_ca_bundle(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return None

        from pleno_pii_scanner_azure_devops import connector as mod

        monkeypatch.setattr(mod.subprocess, "run", fake_run)

        ca = tmp_path / "ca.pem"
        ca.write_text("dummy")
        _clone_into_tempdir(
            "https://tfs/Coll/P/_git/svc",
            AzureDevOpsConfig(
                flavor="server",
                base_url="https://tfs/Coll",
                ca_bundle_path=ca,
            ),
            "Basic Og==",
        )
        assert any(
            "http.sslCAInfo" in part for part in captured["cmd"]
        )

    def test_subprocess_failure_cleans_up_tempdir(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # subprocess raises -> the tempdir we created must be removed
        # so failed scans don't leave litter under /tmp.
        from pleno_pii_scanner_azure_devops import connector as mod

        captured_paths: list[Path] = []
        original_mkdtemp = mod.tempfile.mkdtemp

        def trace_mkdtemp(**kw):
            p = original_mkdtemp(**kw)
            captured_paths.append(Path(p))
            return p

        monkeypatch.setattr(mod.tempfile, "mkdtemp", trace_mkdtemp)

        def fake_run(cmd, **kwargs):
            raise RuntimeError("git failed")

        monkeypatch.setattr(mod.subprocess, "run", fake_run)

        with pytest.raises(RuntimeError, match="git failed"):
            _clone_into_tempdir(
                "https://x",
                AzureDevOpsConfig(
                    flavor="services", organization="x"
                ),
                "Bearer t",
            )
        assert captured_paths
        # Tempdir was rmtree'd on failure
        assert not captured_paths[0].exists()
