"""Tests for the GitlabConnector — discover, fetch, group walk, factory."""

from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path
from typing import Any

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
from pleno_pii_scanner_gitlab.auth import GitlabAuthMode
from pleno_pii_scanner_gitlab.connector import (
    KIND,
    SPEC,
    GitlabConfig,
    GitlabConnector,
    _clone_into_tempdir,
    _extract_credential,
)

from tests.conftest import make_credential


# ---------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------


def make_handler(
    routes: list[tuple[str, Callable[[httpx.Request], httpx.Response]]],
) -> Callable[[httpx.Request], httpx.Response]:
    """Match-by-suffix router; first match wins; unmatched -> AssertionError."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for suffix, fn in routes:
            if suffix in url:
                return fn(request)
        raise AssertionError(f"no route matches {url}")

    return handler


def stub_clone(
    return_path: Path,
    *,
    record: list[tuple[str, str]] | None = None,
) -> Callable[[GitlabConnector, Mapping[str, Any]], Path]:
    """Build a clone_fn that returns `return_path` and (optionally) records calls."""

    def fn(connector: GitlabConnector, project: Mapping[str, Any]) -> Path:
        if record is not None:
            record.append((connector.token, str(project.get("path_with_namespace"))))
        return return_path

    return fn


async def _drain(it: AsyncIterator[DocumentRef]) -> list[DocumentRef]:
    return [r async for r in it]


# ---------------------------------------------------------------------
# config
# ---------------------------------------------------------------------


class TestConfig:
    def test_requires_exactly_one_target(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            GitlabConfig()

    def test_rejects_both_project_and_group(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            GitlabConfig(project="ns/p", group="ns")

    def test_resolved_id_project(self) -> None:
        assert GitlabConfig(project="ns/p").resolved_id() == "gitlab:ns/p"

    def test_resolved_id_group(self) -> None:
        assert GitlabConfig(group="ns").resolved_id() == "gitlab-group:ns"

    def test_explicit_id_overrides(self) -> None:
        assert GitlabConfig(project="ns/p", id="custom").resolved_id() == "custom"

    def test_default_base_url(self) -> None:
        assert GitlabConfig(project="ns/p").base_url == "https://gitlab.com"

    def test_visibility_must_be_legal(self) -> None:
        with pytest.raises(ValueError, match="visibility"):
            GitlabConfig(project="ns/p", visibility="publik")

    def test_visibility_none_allowed(self) -> None:
        # No filter == None == "include all"; must not raise.
        GitlabConfig(project="ns/p", visibility=None)

    def test_visibility_private_allowed(self) -> None:
        GitlabConfig(project="ns/p", visibility="private")


# ---------------------------------------------------------------------
# construction / capabilities / protocol
# ---------------------------------------------------------------------


class TestConstruction:
    def test_runtime_protocol_isinstance(self) -> None:
        c = GitlabConnector(
            GitlabConfig(project="ns/p"),
            credential=make_credential(),
        )
        assert isinstance(c, SourceConnector)
        assert c.kind == "gitlab"
        assert c.id == "gitlab:ns/p"

    def test_capabilities(self) -> None:
        c = GitlabConnector(
            GitlabConfig(project="ns/p"),
            credential=make_credential(),
        )
        caps = c.capabilities()
        assert caps == Capabilities(
            incremental=True,
            binary=False,
            content_hash_delta=False,
            max_concurrent_fetches=4,
            streaming=False,
        )

    def test_credential_missing_auth_rejected(self) -> None:
        cred = Credential(kind="gitlab", payload={"token": "x"})
        with pytest.raises(ValueError, match="`auth`"):
            GitlabConnector(GitlabConfig(project="ns/p"), credential=cred)

    def test_credential_missing_token_rejected(self) -> None:
        cred = Credential(kind="gitlab", payload={"auth": "pat"})
        with pytest.raises(ValueError, match="non-empty"):
            GitlabConnector(GitlabConfig(project="ns/p"), credential=cred)

    def test_credential_oauth_uses_access_token(self) -> None:
        c = GitlabConnector(
            GitlabConfig(project="ns/p"),
            credential=make_credential(mode="oauth", token="oauth-secret"),
        )
        assert c.auth_mode is GitlabAuthMode.OAUTH
        assert c.token == "oauth-secret"

    def test_credential_oauth_falls_back_to_token_alias(self) -> None:
        # Operators rotating between modes may keep `token=` set; we
        # accept it as a fallback for OAuth mode rather than insisting
        # on `access_token`.
        cred = Credential(
            kind="gitlab", payload={"auth": "oauth", "token": "oauth-via-alias"}
        )
        c = GitlabConnector(GitlabConfig(project="ns/p"), credential=cred)
        assert c.token == "oauth-via-alias"

    def test_credential_pat_falls_back_to_access_token_alias(self) -> None:
        cred = Credential(
            kind="gitlab", payload={"auth": "pat", "access_token": "pat-via-alias"}
        )
        c = GitlabConnector(GitlabConfig(project="ns/p"), credential=cred)
        assert c.token == "pat-via-alias"

    def test_credential_project_mode(self) -> None:
        c = GitlabConnector(
            GitlabConfig(project="ns/p"),
            credential=make_credential(mode="project", token="ptok"),
        )
        assert c.auth_mode is GitlabAuthMode.PROJECT
        assert c.token == "ptok"

    def test_credential_invalid_mode_rejected(self) -> None:
        cred = Credential(kind="gitlab", payload={"auth": "bearer", "token": "x"})
        with pytest.raises(ValueError, match="unsupported gitlab auth mode"):
            GitlabConnector(GitlabConfig(project="ns/p"), credential=cred)

    def test_credential_empty_token_rejected(self) -> None:
        cred = Credential(kind="gitlab", payload={"auth": "pat", "token": ""})
        with pytest.raises(ValueError, match="non-empty"):
            GitlabConnector(GitlabConfig(project="ns/p"), credential=cred)

    def test_extract_credential_unit(self) -> None:
        # Unit-level coverage of the helper, mirrors integration tests.
        mode, token = _extract_credential(make_credential())
        assert mode is GitlabAuthMode.PAT
        assert token == "glpat-test-token"


# ---------------------------------------------------------------------
# discover — single project
# ---------------------------------------------------------------------


class TestDiscoverSingleProject:
    async def test_single_project_yields_clone_files(self, clone_dir: Path) -> None:
        def project(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "id": 42,
                    "path_with_namespace": "ns/repo",
                    "default_branch": "main",
                    "web_url": "https://gitlab.com/ns/repo",
                    "archived": False,
                },
            )

        transport = httpx.MockTransport(
            make_handler(
                [
                    ("/projects/", project),
                ]
            )
        )
        c = GitlabConnector(
            GitlabConfig(project="ns/repo"),
            credential=make_credential(),
            transport=transport,
            clone_fn=stub_clone(clone_dir),
        )
        try:
            refs = await _drain(c.discover(SourceFilter(), None))
            paths = {r.path for r in refs}
            # clone_dir fixture creates README.md, secret.env, src/app.py
            assert paths == {
                "ns/repo/README.md",
                "ns/repo/secret.env",
                "ns/repo/src/app.py",
            }
            for ref in refs:
                assert ref.metadata["path_with_namespace"] == "ns/repo"
                assert ref.metadata["project_id"] == "42"
                assert ref.metadata["default_branch"] == "main"
                assert ref.parent_chain == ("gitlab://ns/repo",)
                assert ref.native_url is not None
                assert "ns/repo/-/blob/HEAD" in ref.native_url
        finally:
            await c.close()

    async def test_single_project_404_yields_nothing(self) -> None:
        transport = httpx.MockTransport(
            make_handler(
                [
                    ("/projects/", lambda _: httpx.Response(404)),
                ]
            )
        )
        c = GitlabConnector(
            GitlabConfig(project="ns/missing"),
            credential=make_credential(),
            transport=transport,
        )
        try:
            assert await _drain(c.discover(SourceFilter(), None)) == []
        finally:
            await c.close()

    async def test_url_encoded_project_path_used(self, clone_dir: Path) -> None:
        seen: dict[str, str] = {}

        def project(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(
                200,
                json={
                    "id": 1,
                    "path_with_namespace": "ns/sub/repo",
                    "default_branch": "main",
                    "archived": False,
                },
            )

        transport = httpx.MockTransport(make_handler([("/projects/", project)]))
        c = GitlabConnector(
            GitlabConfig(project="ns/sub/repo"),
            credential=make_credential(),
            transport=transport,
            clone_fn=stub_clone(clone_dir),
        )
        try:
            await _drain(c.discover(SourceFilter(), None))
            # `/` → `%2F` in the project path lookup; this is critical
            # because GitLab's `/projects/:id` endpoint cannot dispatch
            # to a path-with-slashes without the URL-encoded form.
            assert "ns%2Fsub%2Frepo" in seen["url"]
        finally:
            await c.close()

    async def test_archived_project_filtered_when_not_included(
        self, clone_dir: Path
    ) -> None:
        def project(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "id": 1,
                    "path_with_namespace": "ns/old",
                    "default_branch": "main",
                    "archived": True,
                },
            )

        transport = httpx.MockTransport(
            make_handler(
                [
                    ("/projects/", project),
                ]
            )
        )
        c = GitlabConnector(
            GitlabConfig(project="ns/old", include_archived=False),
            credential=make_credential(),
            transport=transport,
            clone_fn=stub_clone(clone_dir),
        )
        try:
            assert await _drain(c.discover(SourceFilter(), None)) == []
        finally:
            await c.close()

    async def test_archived_project_included_when_flag_set(
        self, clone_dir: Path
    ) -> None:
        def project(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "id": 1,
                    "path_with_namespace": "ns/old",
                    "default_branch": "main",
                    "archived": True,
                },
            )

        transport = httpx.MockTransport(
            make_handler(
                [
                    ("/projects/", project),
                ]
            )
        )
        c = GitlabConnector(
            GitlabConfig(project="ns/old", include_archived=True),
            credential=make_credential(),
            transport=transport,
            clone_fn=stub_clone(clone_dir),
        )
        try:
            refs = await _drain(c.discover(SourceFilter(), None))
            assert len(refs) >= 1
        finally:
            await c.close()


# ---------------------------------------------------------------------
# discover — group walk + pagination
# ---------------------------------------------------------------------


class TestDiscoverGroup:
    async def test_paginated_group_projects_yielded(self, clone_dir: Path) -> None:
        # Page 1 -> Link points to page 2; page 2 -> no Link header.
        page1_url = "https://gitlab.com/api/v4/groups/acme/projects?page=2"
        responses = iter(
            [
                httpx.Response(
                    200,
                    headers={"Link": f'<{page1_url}>; rel="next"'},
                    json=[
                        {
                            "id": 1,
                            "path_with_namespace": "acme/r1",
                            "default_branch": "main",
                            "archived": False,
                        },
                        {
                            # Defensive guard: API page entry not a dict — skip.
                            "not_a_dict": True,
                        },
                    ],
                ),
                httpx.Response(
                    200,
                    json=[
                        {
                            "id": 2,
                            "path_with_namespace": "acme/sub/r2",
                            "default_branch": "main",
                            "archived": True,  # filtered out
                        },
                        {
                            "id": 3,
                            "path_with_namespace": "acme/r3",
                            "default_branch": "main",
                            "archived": False,
                        },
                    ],
                ),
            ]
        )
        # The list itself contains one non-dict entry to exercise the
        # malformed-page-entry skip.
        responses_list = list(responses)
        responses_iter = iter(responses_list)

        def groups(_: httpx.Request) -> httpx.Response:
            return next(responses_iter)

        # Replace the second page's middle entry with a non-dict to hit
        # the defensive skip branch.
        responses_list[0].json()  # ensure body is decoded
        # Override the middle entry by editing the underlying body in
        # the iterator — easier: mutate via fresh httpx.Response.
        responses_iter = iter(
            [
                httpx.Response(
                    200,
                    headers={"Link": f'<{page1_url}>; rel="next"'},
                    json=[
                        {
                            "id": 1,
                            "path_with_namespace": "acme/r1",
                            "default_branch": "main",
                            "archived": False,
                        },
                        "not-a-dict",
                    ],
                ),
                httpx.Response(
                    200,
                    json=[
                        {
                            "id": 2,
                            "path_with_namespace": "acme/sub/r2",
                            "default_branch": "main",
                            "archived": True,
                        },
                        {
                            "id": 3,
                            "path_with_namespace": "acme/r3",
                            "default_branch": "main",
                            "archived": False,
                        },
                    ],
                ),
            ]
        )
        clones: dict[str, Path] = {}

        def clone_fn(_: GitlabConnector, project: Mapping[str, Any]) -> Path:
            # Each project must get its own clone dir to avoid the
            # double-add path. We materialise a unique tmpdir per call.
            import tempfile

            d = Path(tempfile.mkdtemp(prefix="pleno-glt-"))
            (d / "f.txt").write_text(f"x={project['path_with_namespace']}\n")
            clones[project["path_with_namespace"]] = d
            return d

        transport = httpx.MockTransport(
            make_handler(
                [
                    ("/groups/", groups),
                ]
            )
        )
        c = GitlabConnector(
            GitlabConfig(group="acme"),
            credential=make_credential(),
            transport=transport,
            clone_fn=clone_fn,
        )
        try:
            refs = await _drain(c.discover(SourceFilter(), None))
            slugs = sorted({r.metadata["path_with_namespace"] for r in refs})
            # r2 was archived (filtered); the non-dict entry is skipped.
            assert slugs == ["acme/r1", "acme/r3"]
        finally:
            await c.close()

    async def test_group_with_visibility_filter(self, clone_dir: Path) -> None:
        seen_params: dict[str, str] = {}

        def groups(request: httpx.Request) -> httpx.Response:
            for k, v in request.url.params.items():
                seen_params[k] = v
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 1,
                        "path_with_namespace": "acme/private-repo",
                        "default_branch": "main",
                        "archived": False,
                    }
                ],
            )

        transport = httpx.MockTransport(
            make_handler(
                [
                    ("/groups/", groups),
                ]
            )
        )
        c = GitlabConnector(
            GitlabConfig(group="acme", visibility="private"),
            credential=make_credential(),
            transport=transport,
            clone_fn=stub_clone(clone_dir),
        )
        try:
            await _drain(c.discover(SourceFilter(), None))
            # Server-side `visibility=private` must have been sent.
            assert seen_params.get("visibility") == "private"
            assert seen_params.get("include_subgroups") == "true"
            # And `archived=false` since we did not set include_archived.
            assert seen_params.get("archived") == "false"
        finally:
            await c.close()

    async def test_group_404_yields_nothing(self) -> None:
        transport = httpx.MockTransport(
            make_handler(
                [
                    ("/groups/", lambda _: httpx.Response(404)),
                ]
            )
        )
        c = GitlabConnector(
            GitlabConfig(group="missing"),
            credential=make_credential(),
            transport=transport,
        )
        try:
            assert await _drain(c.discover(SourceFilter(), None)) == []
        finally:
            await c.close()

    async def test_group_malformed_response_yields_nothing(self) -> None:
        # GitLab returned a JSON object instead of a list (e.g. a
        # `{"message": "no token"}` 200 from a misconfigured proxy).
        transport = httpx.MockTransport(
            make_handler(
                [
                    ("/groups/", lambda _: httpx.Response(200, json={"oops": True})),
                ]
            )
        )
        c = GitlabConnector(
            GitlabConfig(group="acme"),
            credential=make_credential(),
            transport=transport,
        )
        try:
            assert await _drain(c.discover(SourceFilter(), None)) == []
        finally:
            await c.close()

    async def test_group_subgroup_path_walked(self, clone_dir: Path) -> None:
        # The point of include_subgroups=true: a subgroup project must
        # appear in the same enumeration as the top-level group's.
        def groups(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 1,
                        "path_with_namespace": "acme/team-a/proj",
                        "default_branch": "main",
                        "archived": False,
                    },
                    {
                        "id": 2,
                        "path_with_namespace": "acme/team-b/sub/proj",
                        "default_branch": "main",
                        "archived": False,
                    },
                ],
            )

        transport = httpx.MockTransport(
            make_handler(
                [
                    ("/groups/", groups),
                ]
            )
        )
        c = GitlabConnector(
            GitlabConfig(group="acme"),
            credential=make_credential(),
            transport=transport,
            clone_fn=stub_clone(clone_dir),
        )
        try:
            refs = await _drain(c.discover(SourceFilter(), None))
            slugs = {r.metadata["path_with_namespace"] for r in refs}
            assert slugs == {"acme/team-a/proj", "acme/team-b/sub/proj"}
        finally:
            await c.close()


# ---------------------------------------------------------------------
# discover — clone failure resilience
# ---------------------------------------------------------------------


class TestCloneFailure:
    async def test_clone_failure_skips_project(self, clone_dir: Path) -> None:
        # First project clone raises; second succeeds. The connector
        # must skip the failed one and still emit the second's refs.
        def groups(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 1,
                        "path_with_namespace": "acme/broken",
                        "default_branch": "main",
                        "archived": False,
                    },
                    {
                        "id": 2,
                        "path_with_namespace": "acme/good",
                        "default_branch": "main",
                        "archived": False,
                    },
                ],
            )

        def clone_fn(connector: GitlabConnector, project: Mapping[str, Any]) -> Path:
            if project["path_with_namespace"] == "acme/broken":
                raise subprocess.CalledProcessError(1, ["git", "clone"])
            return clone_dir

        transport = httpx.MockTransport(
            make_handler(
                [
                    ("/groups/", groups),
                ]
            )
        )
        c = GitlabConnector(
            GitlabConfig(group="acme"),
            credential=make_credential(),
            transport=transport,
            clone_fn=clone_fn,
        )
        try:
            refs = await _drain(c.discover(SourceFilter(), None))
            slugs = {r.metadata["path_with_namespace"] for r in refs}
            assert slugs == {"acme/good"}
        finally:
            await c.close()


# ---------------------------------------------------------------------
# discover — enumerate_fn injection
# ---------------------------------------------------------------------


class TestEnumerateFn:
    async def test_enumerate_fn_overrides_api_walk(self, clone_dir: Path) -> None:
        # The injected enumerator returns one project; the API
        # transport asserts on access — proving we never hit the wire.
        def boom(_: httpx.Request) -> httpx.Response:
            raise AssertionError("API should not be called")

        async def fake_enumerate(
            _: GitlabConnector,
        ) -> AsyncIterator[Mapping[str, Any]]:
            yield {
                "id": 99,
                "path_with_namespace": "fake/proj",
                "default_branch": "main",
                "archived": False,
            }

        transport = httpx.MockTransport(boom)
        c = GitlabConnector(
            GitlabConfig(group="acme"),
            credential=make_credential(),
            transport=transport,
            clone_fn=stub_clone(clone_dir),
            enumerate_fn=fake_enumerate,
        )
        try:
            refs = await _drain(c.discover(SourceFilter(), None))
            assert {r.metadata["path_with_namespace"] for r in refs} == {"fake/proj"}
        finally:
            await c.close()

    async def test_enumerate_fn_skips_malformed_entry(self) -> None:
        # path_with_namespace missing: defensive `continue`.
        async def fake_enumerate(
            _: GitlabConnector,
        ) -> AsyncIterator[Mapping[str, Any]]:
            yield {"id": 1}  # no path_with_namespace
            yield {"path_with_namespace": 123}  # non-string

        c = GitlabConnector(
            GitlabConfig(group="acme"),
            credential=make_credential(),
            transport=httpx.MockTransport(lambda _: httpx.Response(500)),
            enumerate_fn=fake_enumerate,
        )
        try:
            assert await _drain(c.discover(SourceFilter(), None)) == []
        finally:
            await c.close()


# ---------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------


class TestFetch:
    async def test_returns_decoded_text_from_clone(self, clone_dir: Path) -> None:
        def project(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "id": 1,
                    "path_with_namespace": "ns/repo",
                    "default_branch": "main",
                    "archived": False,
                },
            )

        transport = httpx.MockTransport(make_handler([("/projects/", project)]))
        c = GitlabConnector(
            GitlabConfig(project="ns/repo"),
            credential=make_credential(),
            transport=transport,
            clone_fn=stub_clone(clone_dir),
        )
        try:
            refs = await _drain(c.discover(SourceFilter(), None))
            secret_ref = next(r for r in refs if r.path.endswith("secret.env"))
            docs = [d async for d in c.fetch(secret_ref)]
            assert len(docs) == 1
            assert isinstance(docs[0], Document)
            assert docs[0].text is not None
            assert "AKIAIOSFODNN7EXAMPLE" in docs[0].text
            # The fetched_at must be populated even when the inner
            # connector did not (e.g. older DirConnector versions).
            assert docs[0].fetched_at is not None
        finally:
            await c.close()

    async def test_fetch_without_clone_yields_nothing(self) -> None:
        # No discover ran -> no clone -> fetch must be a no-op.
        c = GitlabConnector(
            GitlabConfig(project="ns/repo"),
            credential=make_credential(),
            transport=httpx.MockTransport(lambda _: httpx.Response(500)),
        )
        try:
            ghost = DocumentRef(
                source_id=c.id,
                source_kind=c.kind,
                path="ns/repo/x",
                metadata={
                    "path_with_namespace": "ns/repo",
                    "inner_path": "x",
                },
            )
            assert [d async for d in c.fetch(ghost)] == []
        finally:
            await c.close()

    async def test_fetch_missing_metadata_yields_nothing(self) -> None:
        c = GitlabConnector(
            GitlabConfig(project="ns/repo"),
            credential=make_credential(),
            transport=httpx.MockTransport(lambda _: httpx.Response(500)),
        )
        try:
            ghost = DocumentRef(
                source_id=c.id,
                source_kind=c.kind,
                path="x",
            )
            assert [d async for d in c.fetch(ghost)] == []
        finally:
            await c.close()


# ---------------------------------------------------------------------
# clone reuse + race-loser cleanup
# ---------------------------------------------------------------------


class TestCloneReuse:
    async def test_second_call_returns_cached_clone(self, clone_dir: Path) -> None:
        # A project that appears twice in the enumeration must only be
        # cloned once; the second discover() pass returns the cached path.
        calls = {"n": 0}

        def clone_fn(_: GitlabConnector, project: Mapping[str, Any]) -> Path:
            calls["n"] += 1
            return clone_dir

        async def fake_enumerate(
            _: GitlabConnector,
        ) -> AsyncIterator[Mapping[str, Any]]:
            yield {
                "id": 1,
                "path_with_namespace": "ns/dup",
                "default_branch": "main",
                "archived": False,
            }
            yield {
                "id": 1,
                "path_with_namespace": "ns/dup",
                "default_branch": "main",
                "archived": False,
            }

        c = GitlabConnector(
            GitlabConfig(group="acme"),
            credential=make_credential(),
            transport=httpx.MockTransport(lambda _: httpx.Response(500)),
            clone_fn=clone_fn,
            enumerate_fn=fake_enumerate,
        )
        try:
            await _drain(c.discover(SourceFilter(), None))
            # Exactly one clone for two yields of the same slug.
            assert calls["n"] == 1
        finally:
            await c.close()

    async def test_concurrent_clone_race_rmtrees_loser(self, tmp_path: Path) -> None:
        # Force the race window: two clones of the same project run
        # concurrently. The loser must rmtree its tempdir.
        import asyncio as _asyncio

        clone_a = tmp_path / "a"
        clone_a.mkdir()
        (clone_a / "f.txt").write_text("a")
        clone_b = tmp_path / "b"
        clone_b.mkdir()
        (clone_b / "f.txt").write_text("b")
        clones = iter([clone_a, clone_b])
        gate = _asyncio.Event()

        def slow_clone(_: GitlabConnector, project: Mapping[str, Any]) -> Path:
            # Block both threads on the gate so they both pass the
            # cache-miss check before either populates the dict.
            import time

            for _ in range(100):
                if gate.is_set():
                    break
                time.sleep(0.005)
            return next(clones)

        c = GitlabConnector(
            GitlabConfig(project="ns/race"),
            credential=make_credential(),
            transport=httpx.MockTransport(lambda _: httpx.Response(500)),
            clone_fn=slow_clone,
        )
        try:
            t1 = _asyncio.create_task(
                c._ensure_clone({"path_with_namespace": "ns/race"})
            )
            t2 = _asyncio.create_task(
                c._ensure_clone({"path_with_namespace": "ns/race"})
            )
            await _asyncio.sleep(0.02)
            gate.set()
            p1, p2 = await _asyncio.gather(t1, t2)
            # Both calls return the same path (the race winner).
            assert p1 == p2
            # The loser dir is gone (rmtree'd in the second-acquire branch).
            losers = [d for d in (clone_a, clone_b) if not d.exists()]
            assert len(losers) == 1
        finally:
            await c.close()


# ---------------------------------------------------------------------
# fetch — defensive against DirConnector contract changes
# ---------------------------------------------------------------------


class TestFetchDefensive:
    async def test_non_document_yield_skipped(self, clone_dir: Path) -> None:
        # Patch DirConnector.fetch to yield a DocumentChunk-like object
        # mid-stream; the connector must skip it without crashing.
        import pleno_pii_scanner_gitlab.connector as connector_mod

        real_dir_connector = connector_mod.DirConnector

        class FakeDirConnector(real_dir_connector):
            async def fetch(self, ref):  # type: ignore[override]
                # Yield a non-Document (DocumentChunk) and one Document.
                from pleno_pii_scanner.sources.base import DocumentChunk

                yield DocumentChunk(
                    ref=ref,
                    byte_range=(0, 1),
                    is_final=True,
                    text="x",
                )
                async for doc in real_dir_connector.fetch(self, ref):
                    yield doc

        # Inject the fake into the connector module.
        from unittest.mock import patch

        def project(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "id": 1,
                    "path_with_namespace": "ns/repo",
                    "default_branch": "main",
                    "archived": False,
                },
            )

        transport = httpx.MockTransport(make_handler([("/projects/", project)]))
        c = GitlabConnector(
            GitlabConfig(project="ns/repo"),
            credential=make_credential(),
            transport=transport,
            clone_fn=stub_clone(clone_dir),
        )
        try:
            refs = await _drain(c.discover(SourceFilter(), None))
            target = next(r for r in refs if r.path.endswith("README.md"))
            with patch.object(connector_mod, "DirConnector", FakeDirConnector):
                docs = [d async for d in c.fetch(target)]
            # The DocumentChunk was skipped; only the real Document came through.
            assert len(docs) == 1
            assert isinstance(docs[0], Document)
        finally:
            await c.close()


# ---------------------------------------------------------------------
# rate-limit propagation through discover
# ---------------------------------------------------------------------


class TestRateLimitPropagation:
    async def test_429_during_group_walk_surfaces_rate_limited(self) -> None:
        transport = httpx.MockTransport(
            make_handler(
                [
                    (
                        "/groups/",
                        lambda _: httpx.Response(429, headers={"Retry-After": "5"}),
                    ),
                ]
            )
        )
        c = GitlabConnector(
            GitlabConfig(group="acme"),
            credential=make_credential(),
            transport=transport,
        )
        try:
            with pytest.raises(RateLimited):
                await _drain(c.discover(SourceFilter(), None))
        finally:
            await c.close()


# ---------------------------------------------------------------------
# close lifecycle
# ---------------------------------------------------------------------


class TestClose:
    async def test_close_rmtrees_clones(self, tmp_path: Path) -> None:
        clone = tmp_path / "clone"
        clone.mkdir()
        (clone / "f.txt").write_text("x")

        # Single-project + injected clone_fn returning `clone`.
        def project(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "id": 1,
                    "path_with_namespace": "ns/repo",
                    "default_branch": "main",
                    "archived": False,
                },
            )

        transport = httpx.MockTransport(make_handler([("/projects/", project)]))
        c = GitlabConnector(
            GitlabConfig(project="ns/repo"),
            credential=make_credential(),
            transport=transport,
            clone_fn=stub_clone(clone),
        )
        try:
            await _drain(c.discover(SourceFilter(), None))
            assert clone.exists()
        finally:
            await c.close()
        # close() must rmtree the clone.
        assert not clone.exists()

    async def test_close_idempotent(self) -> None:
        c = GitlabConnector(
            GitlabConfig(project="ns/p"),
            credential=make_credential(),
        )
        await c.close()
        # Second close must not raise even though all state is empty.
        await c.close()

    async def test_close_swallows_rmtree_errors(self, tmp_path: Path) -> None:
        # A clone path that no longer exists when close() runs would
        # raise from rmtree without ignore_errors. Pre-delete to simulate.
        clone = tmp_path / "vanishing"
        clone.mkdir()

        def project(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "id": 1,
                    "path_with_namespace": "ns/repo",
                    "default_branch": "main",
                    "archived": False,
                },
            )

        transport = httpx.MockTransport(make_handler([("/projects/", project)]))
        c = GitlabConnector(
            GitlabConfig(project="ns/repo"),
            credential=make_credential(),
            transport=transport,
            clone_fn=stub_clone(clone),
        )
        await _drain(c.discover(SourceFilter(), None))
        # Vanish the dir behind the connector's back.
        import shutil

        shutil.rmtree(clone)
        # close() must not raise.
        await c.close()


# ---------------------------------------------------------------------
# clone helper — argv shape
# ---------------------------------------------------------------------


class TestCloneIntoTempdir:
    async def test_invokes_git_clone_with_depth_one(
        self, monkeypatch, tmp_path
    ) -> None:
        captured: dict[str, Any] = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            target = Path(cmd[-1])
            (target / ".git").mkdir(parents=True, exist_ok=True)
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        c = GitlabConnector(
            GitlabConfig(project="ns/repo", base_url="https://gitlab.com"),
            credential=make_credential(token="glpat-secret"),
            transport=httpx.MockTransport(lambda _: httpx.Response(500)),
        )
        try:
            path = _clone_into_tempdir(c, {"path_with_namespace": "ns/repo"})
            assert "--depth=1" in captured["cmd"]
            assert "clone" in captured["cmd"]
            url = next(a for a in captured["cmd"] if a.startswith("https://"))
            assert "oauth2:glpat-secret@" in url
            assert "ns/repo.git" in url
            assert path.exists()
        finally:
            import shutil

            shutil.rmtree(path, ignore_errors=True)
            await c.close()

    def test_clone_failure_rmtrees_tempdir(self, monkeypatch) -> None:
        # If `git clone` raises, the tempdir must be rmtree'd so we
        # do not leak under /tmp.
        created: list[Path] = []
        real_mkdtemp = __import__("tempfile").mkdtemp

        def tracking_mkdtemp(prefix=None):
            d = Path(real_mkdtemp(prefix=prefix or "pleno-gl-"))
            created.append(d)
            return str(d)

        monkeypatch.setattr(
            "pleno_pii_scanner_gitlab.connector.tempfile.mkdtemp", tracking_mkdtemp
        )

        def fake_run(*args, **kwargs):
            raise subprocess.CalledProcessError(128, ["git", "clone"])

        monkeypatch.setattr(subprocess, "run", fake_run)
        c = GitlabConnector(
            GitlabConfig(project="ns/repo"),
            credential=make_credential(),
            transport=httpx.MockTransport(lambda _: httpx.Response(500)),
        )
        with pytest.raises(subprocess.CalledProcessError):
            _clone_into_tempdir(c, {"path_with_namespace": "ns/repo"})
        # The tempdir must have been cleaned up.
        for d in created:
            assert not d.exists()


# ---------------------------------------------------------------------
# CA bundle wiring
# ---------------------------------------------------------------------


class TestCABundle:
    def test_ca_bundle_path_stored_on_config(self, tmp_path: Path) -> None:
        # We do not exercise the real TLS init here because constructing
        # an AsyncClient with `verify=<path-to-fake-pem>` actually tries
        # to load it. The plumbing is exercised by the api.py test
        # (`TestVerify::test_verify_path_accepted`) which uses a real
        # fake bundle. Here we just confirm the config field round-trips.
        cfg = GitlabConfig(project="ns/p", ca_bundle_path="/etc/ssl/ca.pem")
        assert cfg.ca_bundle_path == "/etc/ssl/ca.pem"

    def test_ca_bundle_path_forwarded_to_api(self, tmp_path: Path, monkeypatch) -> None:
        # Wire-test: when the connector is built with `ca_bundle_path`,
        # the GitlabApi constructor must receive `verify=<path>`.
        captured: dict[str, Any] = {}
        from pleno_pii_scanner_gitlab import connector as connector_mod

        real_api = connector_mod.GitlabApi

        def spy_api(**kwargs):
            captured.update(kwargs)
            # Force a transport so TLS init is bypassed.
            kwargs["transport"] = httpx.MockTransport(lambda _: httpx.Response(200))
            return real_api(**kwargs)

        monkeypatch.setattr(connector_mod, "GitlabApi", spy_api)
        GitlabConnector(
            GitlabConfig(project="ns/p", ca_bundle_path="/etc/ssl/ca.pem"),
            credential=make_credential(),
        )
        assert captured["verify"] == "/etc/ssl/ca.pem"

    def test_ca_bundle_default_is_true(self, monkeypatch) -> None:
        # When ca_bundle_path is None, verify must default to True so
        # httpx falls back to the system trust store.
        captured: dict[str, Any] = {}
        from pleno_pii_scanner_gitlab import connector as connector_mod

        real_api = connector_mod.GitlabApi

        def spy_api(**kwargs):
            captured.update(kwargs)
            kwargs["transport"] = httpx.MockTransport(lambda _: httpx.Response(200))
            return real_api(**kwargs)

        monkeypatch.setattr(connector_mod, "GitlabApi", spy_api)
        GitlabConnector(
            GitlabConfig(project="ns/p"),
            credential=make_credential(),
        )
        assert captured["verify"] is True


# ---------------------------------------------------------------------
# SPEC + factory
# ---------------------------------------------------------------------


class TestSpec:
    def test_spec_metadata(self) -> None:
        assert SPEC.kind == "gitlab"
        assert KIND == "gitlab"
        assert SPEC.required_scopes == ("read_api", "read_repository")
        assert SPEC.capabilities.incremental is True

    def test_factory_builds_project_connector(self, monkeypatch) -> None:
        # We bypass TLS init by spying GitlabApi to inject a transport;
        # otherwise constructing with `ca_bundle_path=/etc/ssl/ca.pem`
        # triggers a real load_verify_locations call.
        from pleno_pii_scanner_gitlab import connector as connector_mod

        real_api = connector_mod.GitlabApi

        def spy_api(**kwargs):
            kwargs["transport"] = httpx.MockTransport(lambda _: httpx.Response(200))
            return real_api(**kwargs)

        monkeypatch.setattr(connector_mod, "GitlabApi", spy_api)
        cred = make_credential()
        c = SPEC.factory(
            {
                "project": "ns/p",
                "_credential": cred,
                "base_url": "https://gitlab.example.com",
                "include_archived": True,
                "visibility": "private",
                "ca_bundle_path": "/etc/ssl/ca.pem",
                "id": "my-source",
            }
        )
        assert isinstance(c, GitlabConnector)
        assert c.id == "my-source"

    def test_factory_builds_group_connector(self) -> None:
        c = SPEC.factory({"group": "acme", "_credential": make_credential()})
        assert isinstance(c, GitlabConnector)
        assert c.id == "gitlab-group:acme"

    def test_factory_requires_credential(self) -> None:
        with pytest.raises(ValueError, match="Credential"):
            SPEC.factory({"project": "ns/p"})

    def test_factory_credential_must_be_credential_instance(self) -> None:
        with pytest.raises(ValueError, match="Credential"):
            SPEC.factory({"project": "ns/p", "_credential": "not-a-cred"})


# ---------------------------------------------------------------------
# package __init__ re-exports
# ---------------------------------------------------------------------


class TestPackageInit:
    def test_top_level_exports(self) -> None:
        import pleno_pii_scanner_gitlab as pkg

        assert pkg.SPEC is SPEC
        assert pkg.KIND == "gitlab"
        assert pkg.GitlabConfig is GitlabConfig
        assert pkg.GitlabConnector is GitlabConnector
        assert hasattr(pkg, "GitlabAuthMode")
        assert hasattr(pkg, "parse_auth_mode")
        assert pkg.__version__ == "0.1.0"
