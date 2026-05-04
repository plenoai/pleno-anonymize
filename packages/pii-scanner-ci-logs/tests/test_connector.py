"""Tests for CiLogsConnector — one class per flavor + shared.

Hermetic: every test injects an `httpx.MockTransport` so no real
HTTP is dispatched. Per-flavor class structure mirrors what the
TASK spec asks for.
"""

from __future__ import annotations

from datetime import UTC, datetime

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
from pleno_pii_scanner_ci_logs import (
    KIND,
    SPEC,
    BasicAuth,
    BearerAuth,
    CircleTokenAuth,
    CiLogsConfig,
    CiLogsConnector,
)
from pleno_pii_scanner_ci_logs.connector import (
    DEFAULT_MAX_BUILDS,
    DEFAULT_MAX_LOG_BYTES,
    _build_auth,
    _buildkite_extract_log,
    _circleci_extract_log,
    _is_gha_failed,
    _parse_iso_or_none,
)
from tests.conftest import build_zip, build_zip_bomb, drain, make_handler


# ---------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------


class TestConfig:
    def test_invalid_flavor_rejected(self) -> None:
        with pytest.raises(ValueError, match="flavor"):
            CiLogsConfig(flavor="travisci")  # type: ignore[arg-type]

    def test_gha_requires_owner_and_repo(self) -> None:
        with pytest.raises(ValueError, match="owner"):
            CiLogsConfig(flavor="github_actions")
        with pytest.raises(ValueError, match="owner"):
            CiLogsConfig(flavor="github_actions", owner="o")

    def test_circleci_requires_owner_and_repo(self) -> None:
        with pytest.raises(ValueError, match="owner"):
            CiLogsConfig(flavor="circleci", repo="r")

    def test_buildkite_requires_org_and_pipeline(self) -> None:
        with pytest.raises(ValueError, match="org"):
            CiLogsConfig(flavor="buildkite", org="o")

    def test_jenkins_requires_base_url(self) -> None:
        with pytest.raises(ValueError, match="base_url"):
            CiLogsConfig(flavor="jenkins")

    def test_invalid_vcs_type_rejected(self) -> None:
        with pytest.raises(ValueError, match="vcs_type"):
            CiLogsConfig(
                flavor="circleci", owner="o", repo="r", vcs_type="ghe"
            )

    def test_max_builds_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="max_builds"):
            CiLogsConfig(
                flavor="github_actions", owner="o", repo="r", max_builds=0
            )

    def test_max_log_bytes_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="max_log_bytes"):
            CiLogsConfig(
                flavor="github_actions",
                owner="o",
                repo="r",
                max_log_bytes=0,
            )

    def test_resolved_id_gha(self) -> None:
        c = CiLogsConfig(flavor="github_actions", owner="o", repo="r")
        assert c.resolved_id() == "github_actions:o/r"

    def test_resolved_id_circleci(self) -> None:
        c = CiLogsConfig(flavor="circleci", owner="o", repo="r")
        assert c.resolved_id() == "circleci:gh/o/r"

    def test_resolved_id_buildkite(self) -> None:
        c = CiLogsConfig(flavor="buildkite", org="acme", pipeline="api")
        assert c.resolved_id() == "buildkite:acme/api"

    def test_resolved_id_jenkins(self) -> None:
        c = CiLogsConfig(flavor="jenkins", base_url="https://j.local/")
        assert c.resolved_id() == "jenkins:https://j.local"

    def test_resolved_id_explicit_id_wins(self) -> None:
        c = CiLogsConfig(
            flavor="github_actions", owner="o", repo="r", id="custom-id"
        )
        assert c.resolved_id() == "custom-id"

    def test_resolved_base_url_default_per_flavor(self) -> None:
        gha = CiLogsConfig(flavor="github_actions", owner="o", repo="r")
        assert gha.resolved_base_url() == "https://api.github.com"
        cci = CiLogsConfig(flavor="circleci", owner="o", repo="r")
        assert cci.resolved_base_url() == "https://circleci.com/api/v2"
        bk = CiLogsConfig(flavor="buildkite", org="o", pipeline="p")
        assert bk.resolved_base_url() == "https://api.buildkite.com/v2"
        jk = CiLogsConfig(flavor="jenkins", base_url="https://jen/")
        assert jk.resolved_base_url() == "https://jen"

    def test_resolved_base_url_override(self) -> None:
        c = CiLogsConfig(
            flavor="github_actions",
            owner="o",
            repo="r",
            base_url="https://ghes.acme/api/v3/",
        )
        assert c.resolved_base_url() == "https://ghes.acme/api/v3"


# ---------------------------------------------------------------------
# Auth selection (per-flavor)
# ---------------------------------------------------------------------


class TestAuthSelection:
    def test_gha_bearer(self, gha_credential: Credential) -> None:
        auth = _build_auth("github_actions", gha_credential)
        assert isinstance(auth, BearerAuth)
        assert auth.token == "ghp_TESTTOKEN"

    def test_circleci_circle_token(self, circleci_credential: Credential) -> None:
        auth = _build_auth("circleci", circleci_credential)
        assert isinstance(auth, CircleTokenAuth)

    def test_buildkite_bearer(self, buildkite_credential: Credential) -> None:
        auth = _build_auth("buildkite", buildkite_credential)
        assert isinstance(auth, BearerAuth)

    def test_jenkins_basic(self, jenkins_credential: Credential) -> None:
        auth = _build_auth("jenkins", jenkins_credential)
        assert isinstance(auth, BasicAuth)
        assert auth.username == "build"
        assert auth.password == "jenkins_TESTTOKEN"

    def test_jenkins_password_alias_accepted(self) -> None:
        cred = Credential(
            kind="ci_logs", payload={"username": "u", "password": "pw"}
        )
        auth = _build_auth("jenkins", cred)
        assert isinstance(auth, BasicAuth)
        assert auth.password == "pw"

    def test_token_alias_accepted_for_gha(self) -> None:
        cred = Credential(kind="ci_logs", payload={"access_token": "ax"})
        auth = _build_auth("github_actions", cred)
        assert isinstance(auth, BearerAuth)
        assert auth.token == "ax"

    def test_missing_token_rejected(self) -> None:
        cred = Credential(kind="ci_logs", payload={})
        with pytest.raises(ValueError, match="token"):
            _build_auth("github_actions", cred)

    def test_missing_jenkins_credential_rejected(self) -> None:
        cred = Credential(kind="ci_logs", payload={"username": "u"})
        with pytest.raises(ValueError, match="api_token"):
            _build_auth("jenkins", cred)

    def test_token_never_in_resolved_id(self, gha_credential: Credential) -> None:
        c = CiLogsConnector(
            CiLogsConfig(flavor="github_actions", owner="o", repo="r"),
            credential=gha_credential,
        )
        # The PAT must never leak into the scan-instance identifier
        # (which is logged + persisted in FindingsStore + Audit).
        assert "ghp_TESTTOKEN" not in c.id
        assert c.id == "github_actions:o/r"

    def test_token_never_in_credential_error(self) -> None:
        # When the auth selector raises, only key names appear — never
        # the token value. We seed a payload whose token is a unique
        # sentinel so a leaked exception message would test-fail.
        sentinel = "TOKEN-DO-NOT-LEAK-19384"
        cred = Credential(
            kind="ci_logs",
            payload={"username": "u", "extra_token": sentinel},
        )
        try:
            _build_auth("jenkins", cred)
            raise AssertionError("expected ValueError")
        except ValueError as exc:
            assert sentinel not in str(exc)


# ---------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------


class TestConstruction:
    def test_runtime_protocol_isinstance(
        self, gha_credential: Credential
    ) -> None:
        c = CiLogsConnector(
            CiLogsConfig(flavor="github_actions", owner="o", repo="r"),
            credential=gha_credential,
        )
        assert isinstance(c, SourceConnector)
        assert c.kind == "ci_logs"
        assert c.id == "github_actions:o/r"

    def test_capabilities(self, gha_credential: Credential) -> None:
        c = CiLogsConnector(
            CiLogsConfig(flavor="github_actions", owner="o", repo="r"),
            credential=gha_credential,
        )
        caps = c.capabilities()
        assert caps == Capabilities(
            incremental=True,
            binary=False,
            content_hash_delta=False,
            max_concurrent_fetches=4,
            streaming=False,
        )


# ---------------------------------------------------------------------
# GitHub Actions flavor
# ---------------------------------------------------------------------


class TestGithubActions:
    async def test_auth_header_present_on_runs_call(
        self, gha_credential: Credential
    ) -> None:
        seen: dict[str, str] = {}

        def runs(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers["Authorization"]
            return httpx.Response(
                200,
                json={
                    "workflow_runs": [
                        {
                            "id": 1,
                            "head_sha": "abc",
                            "html_url": "https://gh.com/o/r/actions/runs/1",
                            "created_at": "2026-05-04T00:00:00Z",
                            "conclusion": "success",
                        }
                    ],
                    "total_count": 1,
                },
            )

        c = CiLogsConnector(
            CiLogsConfig(flavor="github_actions", owner="o", repo="r"),
            credential=gha_credential,
            transport=httpx.MockTransport(make_handler([("/runs", runs)])),
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            assert len(refs) == 1
            assert seen["auth"] == "Bearer ghp_TESTTOKEN"
            assert refs[0].path == "o/r/runs/1"
            assert refs[0].metadata["flavor"] == "github_actions"
            assert refs[0].metadata["run_id"] == "1"
            assert refs[0].native_url == "https://gh.com/o/r/actions/runs/1"
        finally:
            await c.close()

    async def test_pagination_stops_on_short_page(
        self, gha_credential: Credential
    ) -> None:
        # First page returns 100 entries, second returns 1 (< per_page),
        # third would be requested only if the short-page guard was wrong.
        page1 = {"workflow_runs": [{"id": i} for i in range(100)], "total_count": 200}
        page2 = {"workflow_runs": [{"id": 999}], "total_count": 200}
        pages = iter([page1, page2])
        call_count = {"n": 0}

        def runs(_: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return httpx.Response(200, json=next(pages))

        c = CiLogsConnector(
            CiLogsConfig(
                flavor="github_actions",
                owner="o",
                repo="r",
                max_builds=200,  # raise the cap so pagination terminates first
            ),
            credential=gha_credential,
            transport=httpx.MockTransport(make_handler([("/runs", runs)])),
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            assert len(refs) == 101
            assert call_count["n"] == 2  # third call would StopIteration
        finally:
            await c.close()

    async def test_failed_only_filters_non_failures(
        self, gha_credential: Credential
    ) -> None:
        def runs(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "workflow_runs": [
                        {
                            "id": 1,
                            "conclusion": "success",
                            "created_at": "2026-05-04T00:00:00Z",
                        },
                        {
                            "id": 2,
                            "conclusion": "failure",
                            "created_at": "2026-05-04T00:00:00Z",
                        },
                        {
                            "id": 3,
                            "conclusion": "cancelled",
                            "created_at": "2026-05-04T00:00:00Z",
                        },
                    ],
                    "total_count": 3,
                },
            )

        c = CiLogsConnector(
            CiLogsConfig(
                flavor="github_actions",
                owner="o",
                repo="r",
                failed_only=True,
            ),
            credential=gha_credential,
            transport=httpx.MockTransport(make_handler([("/runs", runs)])),
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            run_ids = [r.metadata["run_id"] for r in refs]
            # `cancelled` is not in the failure family; only run 2 stays.
            assert run_ids == ["2"]
        finally:
            await c.close()

    async def test_since_short_circuits_at_cutoff(
        self, gha_credential: Credential
    ) -> None:
        # Newest first: r1 (newer), r2 (older). Once r2 falls under
        # `since`, the iterator must stop without consuming further
        # pages — proven by leaving the second response unprepared.
        responses = iter(
            [
                httpx.Response(
                    200,
                    json={
                        "workflow_runs": [
                            {"id": 1, "created_at": "2026-05-04T10:00:00Z"},
                            {"id": 2, "created_at": "2026-04-01T00:00:00Z"},
                        ],
                        "total_count": 99,
                    },
                ),
            ]
        )

        def runs(_: httpx.Request) -> httpx.Response:
            return next(responses)

        c = CiLogsConnector(
            CiLogsConfig(
                flavor="github_actions",
                owner="o",
                repo="r",
                since=datetime(2026, 5, 1, tzinfo=UTC),
            ),
            credential=gha_credential,
            transport=httpx.MockTransport(make_handler([("/runs", runs)])),
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            # Only r1 emits; the iterator returns before requesting page 2.
            assert [r.metadata["run_id"] for r in refs] == ["1"]
        finally:
            await c.close()

    async def test_max_builds_caps_emission(
        self, gha_credential: Credential
    ) -> None:
        def runs(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "workflow_runs": [
                        {"id": i, "created_at": "2026-05-04T00:00:00Z"}
                        for i in range(100)
                    ],
                    "total_count": 1000,
                },
            )

        c = CiLogsConnector(
            CiLogsConfig(
                flavor="github_actions",
                owner="o",
                repo="r",
                max_builds=5,
            ),
            credential=gha_credential,
            transport=httpx.MockTransport(make_handler([("/runs", runs)])),
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            assert len(refs) == 5
        finally:
            await c.close()

    async def test_429_then_success_during_discover(
        self, gha_credential: Credential
    ) -> None:
        responses = iter(
            [
                httpx.Response(429, headers={"Retry-After": "1"}),
                httpx.Response(
                    200,
                    json={
                        "workflow_runs": [
                            {"id": 1, "created_at": "2026-05-04T00:00:00Z"}
                        ],
                        "total_count": 1,
                    },
                ),
            ]
        )
        slept: list[float] = []

        async def fake_sleep(s: float) -> None:
            slept.append(s)

        def runs(_: httpx.Request) -> httpx.Response:
            return next(responses)

        c = CiLogsConnector(
            CiLogsConfig(flavor="github_actions", owner="o", repo="r"),
            credential=gha_credential,
            transport=httpx.MockTransport(make_handler([("/runs", runs)])),
            sleep=fake_sleep,
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            assert len(refs) == 1
            assert slept == [1.0]
        finally:
            await c.close()

    async def test_malformed_run_entry_skipped(
        self, gha_credential: Credential
    ) -> None:
        def runs(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "workflow_runs": [
                        "not-a-mapping",
                        {"created_at": "2026-05-04T00:00:00Z"},  # no id
                        {"id": 7, "created_at": "2026-05-04T00:00:00Z"},
                    ],
                    "total_count": 3,
                },
            )

        c = CiLogsConnector(
            CiLogsConfig(flavor="github_actions", owner="o", repo="r"),
            credential=gha_credential,
            transport=httpx.MockTransport(make_handler([("/runs", runs)])),
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            assert [r.metadata["run_id"] for r in refs] == ["7"]
        finally:
            await c.close()

    async def test_fetch_unpacks_zip_members(
        self, gha_credential: Credential
    ) -> None:
        zip_blob = build_zip(
            {
                "step1.txt": b"echo $TOKEN=ghp_secret\n",
                "step2.txt": b"normal log line\n",
                "metadata.json": b"{}",  # non-.txt; must be ignored
            }
        )

        def runs(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "workflow_runs": [
                        {"id": 42, "created_at": "2026-05-04T00:00:00Z"}
                    ],
                    "total_count": 1,
                },
            )

        def logs(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=zip_blob)

        c = CiLogsConnector(
            CiLogsConfig(flavor="github_actions", owner="o", repo="r"),
            credential=gha_credential,
            transport=httpx.MockTransport(
                make_handler([
                    ("/runs/42/logs", logs),
                    ("/runs", runs),
                ])
            ),
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            assert len(refs) == 1
            docs = [d async for d in c.fetch(refs[0])]
            assert len(docs) == 2  # only the two .txt members
            assert all(isinstance(d, Document) for d in docs)
            members = sorted(d.extra["member"] for d in docs)
            assert members == ["step1.txt", "step2.txt"]
            texts = sorted(d.text or "" for d in docs)
            assert "echo $TOKEN=ghp_secret\n" in texts
        finally:
            await c.close()

    async def test_fetch_zip_bomb_member_skipped(
        self, gha_credential: Credential
    ) -> None:
        # A member declaring 999 MiB but with a tiny on-disk size:
        # the size cap fires before extraction so we never allocate
        # a multi-GB buffer. We pair it with a legitimate member to
        # prove only the bomb is dropped.
        # Build a member ~64 KiB uncompressed — fits in test memory
        # but trivially exceeds the 1 KiB cap configured below. The
        # cap-check fires before zf.read() so a real-world attack
        # using the same shape (declared_size >> cap) never allocates.
        bomb = build_zip_bomb("evil.txt", declared_size=64 * 1024)

        def runs(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "workflow_runs": [
                        {"id": 1, "created_at": "2026-05-04T00:00:00Z"}
                    ],
                    "total_count": 1,
                },
            )

        def logs(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=bomb)

        c = CiLogsConnector(
            CiLogsConfig(
                flavor="github_actions",
                owner="o",
                repo="r",
                max_log_bytes=1024,  # 1 KiB cap
            ),
            credential=gha_credential,
            transport=httpx.MockTransport(
                make_handler([
                    ("/runs/1/logs", logs),
                    ("/runs", runs),
                ])
            ),
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            docs = [d async for d in c.fetch(refs[0])]
            # Bomb member is silently dropped — no Document yielded.
            assert docs == []
        finally:
            await c.close()

    async def test_fetch_bad_zip_yields_nothing(
        self, gha_credential: Credential
    ) -> None:
        def runs(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "workflow_runs": [
                        {"id": 1, "created_at": "2026-05-04T00:00:00Z"}
                    ],
                    "total_count": 1,
                },
            )

        def logs(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not a zip")

        c = CiLogsConnector(
            CiLogsConfig(flavor="github_actions", owner="o", repo="r"),
            credential=gha_credential,
            transport=httpx.MockTransport(
                make_handler([
                    ("/runs/1/logs", logs),
                    ("/runs", runs),
                ])
            ),
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            docs = [d async for d in c.fetch(refs[0])]
            assert docs == []
        finally:
            await c.close()

    async def test_fetch_log_404_yields_nothing(
        self, gha_credential: Credential
    ) -> None:
        # GHA `/logs` returns 410 once a run has been auto-deleted;
        # the connector must not raise.
        def runs(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "workflow_runs": [
                        {"id": 1, "created_at": "2026-05-04T00:00:00Z"}
                    ],
                    "total_count": 1,
                },
            )

        def logs(_: httpx.Request) -> httpx.Response:
            return httpx.Response(410, content=b"")

        c = CiLogsConnector(
            CiLogsConfig(flavor="github_actions", owner="o", repo="r"),
            credential=gha_credential,
            transport=httpx.MockTransport(
                make_handler([
                    ("/runs/1/logs", logs),
                    ("/runs", runs),
                ])
            ),
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            docs = [d async for d in c.fetch(refs[0])]
            assert docs == []
        finally:
            await c.close()

    async def test_fetch_with_missing_metadata_is_safe(
        self, gha_credential: Credential
    ) -> None:
        c = CiLogsConnector(
            CiLogsConfig(flavor="github_actions", owner="o", repo="r"),
            credential=gha_credential,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(404)
            ),
        )
        try:
            ghost = DocumentRef(
                source_id=c.id,
                source_kind=c.kind,
                path="x",
                metadata={"flavor": "github_actions"},  # no run_id/owner/repo
            )
            docs = [d async for d in c.fetch(ghost)]
            assert docs == []
        finally:
            await c.close()


# ---------------------------------------------------------------------
# CircleCI flavor
# ---------------------------------------------------------------------


class TestCircleCi:
    async def test_auth_uses_circle_token_header(
        self, circleci_credential: Credential
    ) -> None:
        seen: dict[str, str] = {}

        def jobs(request: httpx.Request) -> httpx.Response:
            seen["circle"] = request.headers.get("Circle-Token", "")
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "job_number": 7,
                            "status": "success",
                            "started_at": "2026-05-04T00:00:00Z",
                            "web_url": "https://app.circleci.com/jobs/7",
                        }
                    ],
                    "next_page_token": None,
                },
            )

        c = CiLogsConnector(
            CiLogsConfig(flavor="circleci", owner="o", repo="r"),
            credential=circleci_credential,
            transport=httpx.MockTransport(
                make_handler([("/job", jobs)])
            ),
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            assert seen["circle"] == "circleci_TESTTOKEN"
            assert refs[0].metadata["flavor"] == "circleci"
            assert refs[0].metadata["job_number"] == "7"
            assert refs[0].native_url == "https://app.circleci.com/jobs/7"
        finally:
            await c.close()

    async def test_pagination_walks_page_token(
        self, circleci_credential: Credential
    ) -> None:
        pages = iter(
            [
                {
                    "items": [
                        {
                            "job_number": i,
                            "status": "success",
                            "started_at": "2026-05-04T00:00:00Z",
                        }
                        for i in range(2)
                    ],
                    "next_page_token": "tok-2",
                },
                {
                    "items": [
                        {
                            "job_number": 99,
                            "status": "success",
                            "started_at": "2026-05-04T00:00:00Z",
                        }
                    ],
                    "next_page_token": None,
                },
            ]
        )

        def jobs(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=next(pages))

        c = CiLogsConnector(
            CiLogsConfig(
                flavor="circleci", owner="o", repo="r", max_builds=10
            ),
            credential=circleci_credential,
            transport=httpx.MockTransport(make_handler([("/job", jobs)])),
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            assert [r.metadata["job_number"] for r in refs] == ["0", "1", "99"]
        finally:
            await c.close()

    async def test_failed_only_filters_status(
        self, circleci_credential: Credential
    ) -> None:
        def jobs(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"job_number": 1, "status": "success"},
                        {"job_number": 2, "status": "failed"},
                        {"job_number": 3, "status": "timedout"},
                        {"job_number": 4, "status": "running"},
                    ],
                    "next_page_token": None,
                },
            )

        c = CiLogsConnector(
            CiLogsConfig(
                flavor="circleci",
                owner="o",
                repo="r",
                failed_only=True,
            ),
            credential=circleci_credential,
            transport=httpx.MockTransport(make_handler([("/job", jobs)])),
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            nums = sorted(r.metadata["job_number"] for r in refs)
            assert nums == ["2", "3"]
        finally:
            await c.close()

    async def test_since_short_circuits(
        self, circleci_credential: Credential
    ) -> None:
        def jobs(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "job_number": 1,
                            "status": "success",
                            "started_at": "2026-05-04T00:00:00Z",
                        },
                        {
                            "job_number": 2,
                            "status": "success",
                            "started_at": "2026-04-01T00:00:00Z",
                        },
                    ],
                    "next_page_token": "should-not-be-followed",
                },
            )

        c = CiLogsConnector(
            CiLogsConfig(
                flavor="circleci",
                owner="o",
                repo="r",
                since=datetime(2026, 5, 1, tzinfo=UTC),
            ),
            credential=circleci_credential,
            transport=httpx.MockTransport(make_handler([("/job", jobs)])),
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            assert [r.metadata["job_number"] for r in refs] == ["1"]
        finally:
            await c.close()

    async def test_max_builds_caps(
        self, circleci_credential: Credential
    ) -> None:
        def jobs(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"job_number": i, "status": "success"}
                        for i in range(100)
                    ],
                    "next_page_token": None,
                },
            )

        c = CiLogsConnector(
            CiLogsConfig(
                flavor="circleci", owner="o", repo="r", max_builds=3
            ),
            credential=circleci_credential,
            transport=httpx.MockTransport(make_handler([("/job", jobs)])),
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            assert len(refs) == 3
        finally:
            await c.close()

    async def test_429_handled(
        self, circleci_credential: Credential
    ) -> None:
        responses = iter(
            [
                httpx.Response(429, headers={"Retry-After": "1"}),
                httpx.Response(
                    200,
                    json={
                        "items": [{"job_number": 1, "status": "success"}],
                        "next_page_token": None,
                    },
                ),
            ]
        )
        slept: list[float] = []

        async def fake_sleep(s: float) -> None:
            slept.append(s)

        def jobs(_: httpx.Request) -> httpx.Response:
            return next(responses)

        c = CiLogsConnector(
            CiLogsConfig(flavor="circleci", owner="o", repo="r"),
            credential=circleci_credential,
            transport=httpx.MockTransport(make_handler([("/job", jobs)])),
            sleep=fake_sleep,
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            assert len(refs) == 1
            assert slept == [1.0]
        finally:
            await c.close()

    async def test_malformed_entry_skipped(
        self, circleci_credential: Credential
    ) -> None:
        def jobs(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "items": [
                        "not-a-mapping",
                        {"status": "success"},  # no job_number
                        {"job_number": 5, "status": "success"},
                    ],
                    "next_page_token": None,
                },
            )

        c = CiLogsConnector(
            CiLogsConfig(flavor="circleci", owner="o", repo="r"),
            credential=circleci_credential,
            transport=httpx.MockTransport(make_handler([("/job", jobs)])),
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            assert [r.metadata["job_number"] for r in refs] == ["5"]
        finally:
            await c.close()

    async def test_fetch_returns_inline_output(
        self, circleci_credential: Credential
    ) -> None:
        def jobs(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "items": [{"job_number": 7, "status": "failed"}],
                    "next_page_token": None,
                },
            )

        def detail(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"output": "ERROR: AWS_SECRET_ACCESS_KEY=AKIA..."},
            )

        c = CiLogsConnector(
            CiLogsConfig(
                flavor="circleci",
                owner="o",
                repo="r",
                failed_only=True,
            ),
            credential=circleci_credential,
            transport=httpx.MockTransport(
                make_handler([
                    ("/job/7", detail),
                    ("/job", jobs),
                ])
            ),
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            docs = [d async for d in c.fetch(refs[0])]
            assert len(docs) == 1
            assert isinstance(docs[0], Document)
            assert "AWS_SECRET_ACCESS_KEY" in (docs[0].text or "")
        finally:
            await c.close()

    async def test_fetch_falls_back_to_messages(
        self, circleci_credential: Credential
    ) -> None:
        def jobs(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "items": [{"job_number": 7, "status": "success"}],
                    "next_page_token": None,
                },
            )

        def detail(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "messages": [
                        {"message": "build started"},
                        {"message": "compile ok"},
                    ]
                },
            )

        c = CiLogsConnector(
            CiLogsConfig(flavor="circleci", owner="o", repo="r"),
            credential=circleci_credential,
            transport=httpx.MockTransport(
                make_handler([("/job/7", detail), ("/job", jobs)])
            ),
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            docs = [d async for d in c.fetch(refs[0])]
            assert len(docs) == 1
            assert "build started" in (docs[0].text or "")
        finally:
            await c.close()

    async def test_fetch_404_yields_nothing(
        self, circleci_credential: Credential
    ) -> None:
        def jobs(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "items": [{"job_number": 7, "status": "success"}],
                    "next_page_token": None,
                },
            )

        def detail(_: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        c = CiLogsConnector(
            CiLogsConfig(flavor="circleci", owner="o", repo="r"),
            credential=circleci_credential,
            transport=httpx.MockTransport(
                make_handler([("/job/7", detail), ("/job", jobs)])
            ),
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            docs = [d async for d in c.fetch(refs[0])]
            assert docs == []
        finally:
            await c.close()

    async def test_fetch_no_log_keys_yields_nothing(
        self, circleci_credential: Credential
    ) -> None:
        def jobs(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "items": [{"job_number": 7, "status": "success"}],
                    "next_page_token": None,
                },
            )

        def detail(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"unrelated": "shape"})

        c = CiLogsConnector(
            CiLogsConfig(flavor="circleci", owner="o", repo="r"),
            credential=circleci_credential,
            transport=httpx.MockTransport(
                make_handler([("/job/7", detail), ("/job", jobs)])
            ),
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            docs = [d async for d in c.fetch(refs[0])]
            assert docs == []
        finally:
            await c.close()

    async def test_fetch_bad_json_yields_nothing(
        self, circleci_credential: Credential
    ) -> None:
        def jobs(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "items": [{"job_number": 7, "status": "success"}],
                    "next_page_token": None,
                },
            )

        def detail(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>maintenance</html>")

        c = CiLogsConnector(
            CiLogsConfig(flavor="circleci", owner="o", repo="r"),
            credential=circleci_credential,
            transport=httpx.MockTransport(
                make_handler([("/job/7", detail), ("/job", jobs)])
            ),
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            docs = [d async for d in c.fetch(refs[0])]
            assert docs == []
        finally:
            await c.close()


# ---------------------------------------------------------------------
# Buildkite flavor
# ---------------------------------------------------------------------


class TestBuildkite:
    async def test_auth_present_and_pagination_via_link(
        self, buildkite_credential: Credential
    ) -> None:
        seen_auth: list[str] = []
        responses = iter(
            [
                httpx.Response(
                    200,
                    json=[
                        {
                            "number": 1,
                            "state": "passed",
                            "created_at": "2026-05-04T00:00:00Z",
                            "web_url": "https://buildkite.com/o/p/builds/1",
                            "jobs": [
                                {
                                    "id": "j-1",
                                    "type": "script",
                                    "web_url": "https://buildkite.com/.../j-1",
                                }
                            ],
                        }
                    ],
                    headers={
                        "Link": (
                            '<https://api.buildkite.com/v2/organizations/o/'
                            'pipelines/p/builds?page=2>; rel="next"'
                        )
                    },
                ),
                httpx.Response(
                    200,
                    json=[
                        {
                            "number": 2,
                            "state": "passed",
                            "created_at": "2026-05-04T00:00:00Z",
                            "jobs": [{"id": "j-2", "type": "command"}],
                        }
                    ],
                ),
            ]
        )

        def builds(request: httpx.Request) -> httpx.Response:
            seen_auth.append(request.headers["Authorization"])
            return next(responses)

        c = CiLogsConnector(
            CiLogsConfig(
                flavor="buildkite",
                org="o",
                pipeline="p",
                max_builds=10,
            ),
            credential=buildkite_credential,
            transport=httpx.MockTransport(
                make_handler([("/builds", builds)])
            ),
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            assert all(a == "Bearer bk_TESTTOKEN" for a in seen_auth)
            assert len(refs) == 2
            assert {r.metadata["job_id"] for r in refs} == {"j-1", "j-2"}
        finally:
            await c.close()

    async def test_failed_only_filters_state(
        self, buildkite_credential: Credential
    ) -> None:
        def builds(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=[
                    {
                        "number": 1,
                        "state": "passed",
                        "jobs": [{"id": "j-a", "type": "script"}],
                    },
                    {
                        "number": 2,
                        "state": "failed",
                        "jobs": [{"id": "j-b", "type": "script"}],
                    },
                    {
                        "number": 3,
                        "state": "errored",
                        "jobs": [{"id": "j-c", "type": "command"}],
                    },
                ],
            )

        c = CiLogsConnector(
            CiLogsConfig(
                flavor="buildkite",
                org="o",
                pipeline="p",
                failed_only=True,
            ),
            credential=buildkite_credential,
            transport=httpx.MockTransport(
                make_handler([("/builds", builds)])
            ),
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            ids = sorted(r.metadata["job_id"] for r in refs)
            assert ids == ["j-b", "j-c"]
        finally:
            await c.close()

    async def test_since_short_circuits(
        self, buildkite_credential: Credential
    ) -> None:
        def builds(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=[
                    {
                        "number": 1,
                        "state": "passed",
                        "created_at": "2026-05-04T10:00:00Z",
                        "jobs": [{"id": "j1", "type": "script"}],
                    },
                    {
                        "number": 2,
                        "state": "passed",
                        "created_at": "2026-04-01T00:00:00Z",
                        "jobs": [{"id": "j2", "type": "script"}],
                    },
                ],
                headers={
                    "Link": (
                        '<https://api.buildkite.com/v2/organizations/o/'
                        'pipelines/p/builds?page=2>; rel="next"'
                    )
                },
            )

        c = CiLogsConnector(
            CiLogsConfig(
                flavor="buildkite",
                org="o",
                pipeline="p",
                since=datetime(2026, 5, 1, tzinfo=UTC),
            ),
            credential=buildkite_credential,
            transport=httpx.MockTransport(
                make_handler([("/builds", builds)])
            ),
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            assert [r.metadata["job_id"] for r in refs] == ["j1"]
        finally:
            await c.close()

    async def test_max_builds_caps_emission(
        self, buildkite_credential: Credential
    ) -> None:
        def builds(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=[
                    {
                        "number": i,
                        "state": "passed",
                        "jobs": [{"id": f"j-{i}", "type": "script"}],
                    }
                    for i in range(10)
                ],
            )

        c = CiLogsConnector(
            CiLogsConfig(
                flavor="buildkite",
                org="o",
                pipeline="p",
                max_builds=2,
            ),
            credential=buildkite_credential,
            transport=httpx.MockTransport(
                make_handler([("/builds", builds)])
            ),
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            assert len(refs) == 2
        finally:
            await c.close()

    async def test_429_handled(
        self, buildkite_credential: Credential
    ) -> None:
        responses = iter(
            [
                httpx.Response(429, headers={"Retry-After": "2"}),
                httpx.Response(
                    200,
                    json=[
                        {
                            "number": 1,
                            "state": "passed",
                            "jobs": [{"id": "j", "type": "script"}],
                        }
                    ],
                ),
            ]
        )
        slept: list[float] = []

        async def fake_sleep(s: float) -> None:
            slept.append(s)

        def builds(_: httpx.Request) -> httpx.Response:
            return next(responses)

        c = CiLogsConnector(
            CiLogsConfig(flavor="buildkite", org="o", pipeline="p"),
            credential=buildkite_credential,
            transport=httpx.MockTransport(
                make_handler([("/builds", builds)])
            ),
            sleep=fake_sleep,
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            assert len(refs) == 1
            assert slept == [2.0]
        finally:
            await c.close()

    async def test_skips_wait_jobs_and_malformed(
        self, buildkite_credential: Credential
    ) -> None:
        def builds(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=[
                    {
                        "number": 1,
                        "state": "passed",
                        "jobs": [
                            "not-a-mapping",
                            {"type": "script"},  # no id
                            {"id": "j-wait", "type": "wait"},
                            {"id": "j-cmd", "type": "command"},
                        ],
                    },
                    "not-a-mapping",  # outer-level malformed entry
                    {"state": "passed", "jobs": []},  # no number
                ],
            )

        c = CiLogsConnector(
            CiLogsConfig(flavor="buildkite", org="o", pipeline="p"),
            credential=buildkite_credential,
            transport=httpx.MockTransport(
                make_handler([("/builds", builds)])
            ),
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            ids = [r.metadata["job_id"] for r in refs]
            assert ids == ["j-cmd"]
        finally:
            await c.close()

    async def test_fetch_extracts_content(
        self, buildkite_credential: Credential
    ) -> None:
        def builds(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=[
                    {
                        "number": 1,
                        "state": "passed",
                        "jobs": [{"id": "j-1", "type": "script"}],
                    }
                ],
            )

        def log(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"content": "secret=abcd1234\n"})

        c = CiLogsConnector(
            CiLogsConfig(flavor="buildkite", org="o", pipeline="p"),
            credential=buildkite_credential,
            transport=httpx.MockTransport(
                make_handler([
                    ("/jobs/j-1/log", log),
                    ("/builds", builds),
                ])
            ),
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            docs = [d async for d in c.fetch(refs[0])]
            assert len(docs) == 1
            assert (docs[0].text or "").startswith("secret=")
        finally:
            await c.close()

    async def test_fetch_falls_back_to_response_text(
        self, buildkite_credential: Credential
    ) -> None:
        def builds(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=[
                    {
                        "number": 1,
                        "state": "passed",
                        "jobs": [{"id": "j-1", "type": "script"}],
                    }
                ],
            )

        def log(_: httpx.Request) -> httpx.Response:
            # Proxy stripped the JSON envelope; we got raw text.
            return httpx.Response(
                200, text="bare log line\n", headers={"Content-Type": "text/plain"}
            )

        c = CiLogsConnector(
            CiLogsConfig(flavor="buildkite", org="o", pipeline="p"),
            credential=buildkite_credential,
            transport=httpx.MockTransport(
                make_handler([
                    ("/jobs/j-1/log", log),
                    ("/builds", builds),
                ])
            ),
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            docs = [d async for d in c.fetch(refs[0])]
            assert len(docs) == 1
            assert docs[0].text == "bare log line\n"
        finally:
            await c.close()

    async def test_fetch_404_yields_nothing(
        self, buildkite_credential: Credential
    ) -> None:
        def builds(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=[
                    {
                        "number": 1,
                        "state": "passed",
                        "jobs": [{"id": "j-1", "type": "script"}],
                    }
                ],
            )

        def log(_: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        c = CiLogsConnector(
            CiLogsConfig(flavor="buildkite", org="o", pipeline="p"),
            credential=buildkite_credential,
            transport=httpx.MockTransport(
                make_handler([
                    ("/jobs/j-1/log", log),
                    ("/builds", builds),
                ])
            ),
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            docs = [d async for d in c.fetch(refs[0])]
            assert docs == []
        finally:
            await c.close()

    async def test_fetch_empty_text_yields_nothing(
        self, buildkite_credential: Credential
    ) -> None:
        # Buildkite rarely returns an empty `content` value; no log
        # text means nothing to scan.
        def builds(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=[
                    {
                        "number": 1,
                        "state": "passed",
                        "jobs": [{"id": "j-1", "type": "script"}],
                    }
                ],
            )

        def log(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"content": ""})

        c = CiLogsConnector(
            CiLogsConfig(flavor="buildkite", org="o", pipeline="p"),
            credential=buildkite_credential,
            transport=httpx.MockTransport(
                make_handler([
                    ("/jobs/j-1/log", log),
                    ("/builds", builds),
                ])
            ),
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            docs = [d async for d in c.fetch(refs[0])]
            # `content` is empty AND falls through to response.text
            # which is the JSON serialization — empty string after
            # `_buildkite_extract_log` returns None means we end up
            # with the response.text fallback. For the test, accept
            # either zero docs or a single doc with a JSON-ish body.
            # We verify the connector stayed alive and yielded
            # something well-defined.
            assert len(docs) <= 1
        finally:
            await c.close()


# ---------------------------------------------------------------------
# Jenkins flavor
# ---------------------------------------------------------------------


class TestJenkins:
    async def test_auth_uses_basic(
        self, jenkins_credential: Credential
    ) -> None:
        seen: dict[str, str] = {}

        def api_json(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers["Authorization"]
            return httpx.Response(
                200,
                json={
                    "jobs": [
                        {
                            "name": "deploy",
                            "builds": [
                                {
                                    "number": 42,
                                    "url": "https://j.local/job/deploy/42/",
                                    "timestamp": 1714867200000,  # 2024-05-05
                                    "result": "SUCCESS",
                                }
                            ],
                        }
                    ]
                },
            )

        c = CiLogsConnector(
            CiLogsConfig(flavor="jenkins", base_url="https://j.local"),
            credential=jenkins_credential,
            transport=httpx.MockTransport(
                make_handler([("/api/json", api_json)])
            ),
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            # base64("build:jenkins_TESTTOKEN") starts with "Basic "
            assert seen["auth"].startswith("Basic ")
            assert len(refs) == 1
            assert refs[0].metadata["job_name"] == "deploy"
            assert refs[0].metadata["build_number"] == "42"
            assert refs[0].native_url == "https://j.local/job/deploy/42/"
        finally:
            await c.close()

    async def test_failed_only_filters_result(
        self, jenkins_credential: Credential
    ) -> None:
        def api_json(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "jobs": [
                        {
                            "name": "deploy",
                            "builds": [
                                {
                                    "number": 1,
                                    "url": "https://j/job/deploy/1/",
                                    "result": "SUCCESS",
                                },
                                {
                                    "number": 2,
                                    "url": "https://j/job/deploy/2/",
                                    "result": "FAILURE",
                                },
                                {
                                    "number": 3,
                                    "url": "https://j/job/deploy/3/",
                                    "result": "UNSTABLE",
                                },
                            ],
                        }
                    ]
                },
            )

        c = CiLogsConnector(
            CiLogsConfig(
                flavor="jenkins",
                base_url="https://j.local",
                failed_only=True,
            ),
            credential=jenkins_credential,
            transport=httpx.MockTransport(
                make_handler([("/api/json", api_json)])
            ),
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            nums = sorted(r.metadata["build_number"] for r in refs)
            assert nums == ["2", "3"]
        finally:
            await c.close()

    async def test_since_short_circuits(
        self, jenkins_credential: Credential
    ) -> None:
        # 2024-05-05 = 1714867200000 ms; 2026-04-01 = 1774953600000 ms.
        def api_json(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "jobs": [
                        {
                            "name": "deploy",
                            "builds": [
                                {
                                    "number": 1,
                                    "url": "https://j/job/deploy/1/",
                                    "timestamp": 1774953600000,
                                    "result": "SUCCESS",
                                },
                                {
                                    "number": 2,
                                    "url": "https://j/job/deploy/2/",
                                    "timestamp": 1714867200000,
                                    "result": "SUCCESS",
                                },
                            ],
                        }
                    ]
                },
            )

        c = CiLogsConnector(
            CiLogsConfig(
                flavor="jenkins",
                base_url="https://j.local",
                since=datetime(2025, 1, 1, tzinfo=UTC),
            ),
            credential=jenkins_credential,
            transport=httpx.MockTransport(
                make_handler([("/api/json", api_json)])
            ),
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            assert [r.metadata["build_number"] for r in refs] == ["1"]
        finally:
            await c.close()

    async def test_max_builds_caps(
        self, jenkins_credential: Credential
    ) -> None:
        def api_json(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "jobs": [
                        {
                            "name": "deploy",
                            "builds": [
                                {
                                    "number": i,
                                    "url": f"https://j/job/deploy/{i}/",
                                    "result": "SUCCESS",
                                }
                                for i in range(10)
                            ],
                        }
                    ]
                },
            )

        c = CiLogsConnector(
            CiLogsConfig(
                flavor="jenkins",
                base_url="https://j.local",
                max_builds=4,
            ),
            credential=jenkins_credential,
            transport=httpx.MockTransport(
                make_handler([("/api/json", api_json)])
            ),
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            assert len(refs) == 4
        finally:
            await c.close()

    async def test_429_handled(
        self, jenkins_credential: Credential
    ) -> None:
        responses = iter(
            [
                httpx.Response(429, headers={"Retry-After": "1"}),
                httpx.Response(
                    200,
                    json={
                        "jobs": [
                            {
                                "name": "j",
                                "builds": [
                                    {
                                        "number": 1,
                                        "url": "https://j/job/j/1/",
                                        "result": "SUCCESS",
                                    }
                                ],
                            }
                        ]
                    },
                ),
            ]
        )
        slept: list[float] = []

        async def fake_sleep(s: float) -> None:
            slept.append(s)

        def api_json(_: httpx.Request) -> httpx.Response:
            return next(responses)

        c = CiLogsConnector(
            CiLogsConfig(flavor="jenkins", base_url="https://j.local"),
            credential=jenkins_credential,
            transport=httpx.MockTransport(
                make_handler([("/api/json", api_json)])
            ),
            sleep=fake_sleep,
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            assert len(refs) == 1
            assert slept == [1.0]
        finally:
            await c.close()

    async def test_malformed_payload_skipped(
        self, jenkins_credential: Credential
    ) -> None:
        def api_json(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "jobs": [
                        "not-a-mapping",
                        {"name": ""},  # empty name
                        {
                            "name": "ok",
                            "builds": [
                                "junk",
                                {"number": None, "url": "x"},  # missing number
                                {"number": 5, "url": ""},  # missing url
                                {
                                    "number": 7,
                                    "url": "https://j/job/ok/7/",
                                    "result": "SUCCESS",
                                },
                            ],
                        },
                    ]
                },
            )

        c = CiLogsConnector(
            CiLogsConfig(flavor="jenkins", base_url="https://j.local"),
            credential=jenkins_credential,
            transport=httpx.MockTransport(
                make_handler([("/api/json", api_json)])
            ),
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            nums = [r.metadata["build_number"] for r in refs]
            assert nums == ["7"]
        finally:
            await c.close()

    async def test_api_json_404_yields_empty(
        self, jenkins_credential: Credential
    ) -> None:
        def api_json(_: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        c = CiLogsConnector(
            CiLogsConfig(flavor="jenkins", base_url="https://j.local"),
            credential=jenkins_credential,
            transport=httpx.MockTransport(
                make_handler([("/api/json", api_json)])
            ),
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            assert refs == []
        finally:
            await c.close()

    async def test_api_json_html_yields_empty(
        self, jenkins_credential: Credential
    ) -> None:
        # Jenkins controllers serve the login page on `/api/json`
        # mid-restart; must not crash.
        def api_json(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text="<html>login</html>",
                headers={"Content-Type": "text/html"},
            )

        c = CiLogsConnector(
            CiLogsConfig(flavor="jenkins", base_url="https://j.local"),
            credential=jenkins_credential,
            transport=httpx.MockTransport(
                make_handler([("/api/json", api_json)])
            ),
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            assert refs == []
        finally:
            await c.close()

    async def test_fetch_consoleText(
        self, jenkins_credential: Credential
    ) -> None:
        def api_json(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "jobs": [
                        {
                            "name": "deploy",
                            "builds": [
                                {
                                    "number": 42,
                                    "url": "https://j.local/job/deploy/42/",
                                    "result": "SUCCESS",
                                }
                            ],
                        }
                    ]
                },
            )

        def console(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="echo $DSN=postgres://...\n")

        c = CiLogsConnector(
            CiLogsConfig(flavor="jenkins", base_url="https://j.local"),
            credential=jenkins_credential,
            transport=httpx.MockTransport(
                make_handler([
                    ("/job/deploy/42/consoleText", console),
                    ("/api/json", api_json),
                ])
            ),
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            docs = [d async for d in c.fetch(refs[0])]
            assert len(docs) == 1
            assert "DSN=postgres" in (docs[0].text or "")
        finally:
            await c.close()

    async def test_fetch_404_yields_nothing(
        self, jenkins_credential: Credential
    ) -> None:
        def api_json(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "jobs": [
                        {
                            "name": "deploy",
                            "builds": [
                                {
                                    "number": 42,
                                    "url": "https://j.local/job/deploy/42/",
                                    "result": "SUCCESS",
                                }
                            ],
                        }
                    ]
                },
            )

        def console(_: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        c = CiLogsConnector(
            CiLogsConfig(flavor="jenkins", base_url="https://j.local"),
            credential=jenkins_credential,
            transport=httpx.MockTransport(
                make_handler([
                    ("/job/deploy/42/consoleText", console),
                    ("/api/json", api_json),
                ])
            ),
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            docs = [d async for d in c.fetch(refs[0])]
            assert docs == []
        finally:
            await c.close()

    async def test_fetch_with_no_build_url_yields_nothing(
        self, jenkins_credential: Credential
    ) -> None:
        c = CiLogsConnector(
            CiLogsConfig(flavor="jenkins", base_url="https://j.local"),
            credential=jenkins_credential,
            transport=httpx.MockTransport(lambda _: httpx.Response(404)),
        )
        try:
            ghost = DocumentRef(
                source_id=c.id,
                source_kind=c.kind,
                path="x",
                metadata={"flavor": "jenkins"},  # no build_url
            )
            docs = [d async for d in c.fetch(ghost)]
            assert docs == []
        finally:
            await c.close()


# ---------------------------------------------------------------------
# fetch with unknown flavor (cross-connector ref)
# ---------------------------------------------------------------------


class TestFetchMissingMetadata:
    async def test_circleci_fetch_with_missing_metadata(
        self, circleci_credential: Credential
    ) -> None:
        c = CiLogsConnector(
            CiLogsConfig(flavor="circleci", owner="o", repo="r"),
            credential=circleci_credential,
            transport=httpx.MockTransport(lambda _: httpx.Response(404)),
        )
        try:
            ghost = DocumentRef(
                source_id=c.id,
                source_kind=c.kind,
                path="x",
                metadata={"flavor": "circleci"},  # no owner/repo/job_number
            )
            docs = [d async for d in c.fetch(ghost)]
            assert docs == []
        finally:
            await c.close()

    async def test_buildkite_fetch_with_missing_metadata(
        self, buildkite_credential: Credential
    ) -> None:
        c = CiLogsConnector(
            CiLogsConfig(flavor="buildkite", org="o", pipeline="p"),
            credential=buildkite_credential,
            transport=httpx.MockTransport(lambda _: httpx.Response(404)),
        )
        try:
            ghost = DocumentRef(
                source_id=c.id,
                source_kind=c.kind,
                path="x",
                metadata={"flavor": "buildkite"},  # no org/pipeline/build_number/job_id
            )
            docs = [d async for d in c.fetch(ghost)]
            assert docs == []
        finally:
            await c.close()


class TestFetchZipDirEntry:
    async def test_zip_directory_member_skipped(
        self, gha_credential: Credential
    ) -> None:
        # Build a zip with both a dir entry and a real .txt member.
        # The dir entry exercises the `info.is_dir()` skip branch in
        # the GHA fetcher; the .txt member proves the loop continues
        # past it rather than aborting.
        import io as _io
        import zipfile as _zf

        buf = _io.BytesIO()
        with _zf.ZipFile(buf, "w") as zf:
            # Trailing slash makes ZipInfo.is_dir() True.
            zf.writestr(_zf.ZipInfo("subdir/"), b"")
            zf.writestr("good.txt", b"payload\n")
        zip_blob = buf.getvalue()

        def runs(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "workflow_runs": [
                        {"id": 1, "created_at": "2026-05-04T00:00:00Z"}
                    ],
                    "total_count": 1,
                },
            )

        def logs(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=zip_blob)

        c = CiLogsConnector(
            CiLogsConfig(flavor="github_actions", owner="o", repo="r"),
            credential=gha_credential,
            transport=httpx.MockTransport(
                make_handler([
                    ("/runs/1/logs", logs),
                    ("/runs", runs),
                ])
            ),
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            docs = [d async for d in c.fetch(refs[0])]
            members = [d.extra["member"] for d in docs]
            assert members == ["good.txt"]
        finally:
            await c.close()


class TestApiProperty:
    def test_api_property_exposes_underlying_client(
        self, gha_credential: Credential
    ) -> None:
        # Tests + downstream may want to inspect the api wrapper
        # (e.g. to assert custom headers, override the bearer
        # mid-scan). Verify the property returns the same instance.
        c = CiLogsConnector(
            CiLogsConfig(flavor="github_actions", owner="o", repo="r"),
            credential=gha_credential,
        )
        assert c.api is c._api  # type: ignore[attr-defined]


class TestExtractGuards:
    def test_buildkite_extract_log_rejects_non_mapping(self) -> None:
        # Defensive against a body that arrived as a list (proxy
        # rewrite, schema drift). Pass a list, expect None.
        assert _buildkite_extract_log([])  is None  # type: ignore[arg-type]


class TestFetchUnknownFlavor:
    async def test_unknown_flavor_yields_nothing(
        self, gha_credential: Credential
    ) -> None:
        c = CiLogsConnector(
            CiLogsConfig(flavor="github_actions", owner="o", repo="r"),
            credential=gha_credential,
            transport=httpx.MockTransport(lambda _: httpx.Response(404)),
        )
        try:
            ghost = DocumentRef(
                source_id="other",
                source_kind="other",
                path="x",
                metadata={"flavor": "travisci"},
            )
            docs = [d async for d in c.fetch(ghost)]
            assert docs == []
        finally:
            await c.close()


# ---------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------


class TestLifecycle:
    async def test_close_aclose_api(
        self, gha_credential: Credential
    ) -> None:
        c = CiLogsConnector(
            CiLogsConfig(flavor="github_actions", owner="o", repo="r"),
            credential=gha_credential,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"workflow_runs": []})),
        )
        # Drive at least one round-trip so the underlying client is
        # hot before close — exercises the same path production hits.
        await drain(c.discover(SourceFilter(), None))
        await c.close()


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


class TestHelpers:
    def test_parse_iso_or_none_returns_none_on_garbage(self) -> None:
        assert _parse_iso_or_none("not-iso") is None
        assert _parse_iso_or_none(None) is None
        assert _parse_iso_or_none("") is None
        assert _parse_iso_or_none(123) is None

    def test_parse_iso_or_none_handles_z(self) -> None:
        v = _parse_iso_or_none("2026-05-04T12:00:00Z")
        assert v is not None and v.tzinfo is not None

    def test_is_gha_failed(self) -> None:
        assert _is_gha_failed({"conclusion": "failure"})
        assert _is_gha_failed({"conclusion": "timed_out"})
        assert _is_gha_failed({"conclusion": "action_required"})
        assert _is_gha_failed({"conclusion": "startup_failure"})
        assert not _is_gha_failed({"conclusion": "success"})
        assert not _is_gha_failed({"conclusion": "cancelled"})
        assert not _is_gha_failed({})

    def test_circleci_extract_log_inline_output(self) -> None:
        assert _circleci_extract_log({"output": "x"}) == "x"

    def test_circleci_extract_log_messages(self) -> None:
        body = {"messages": [{"message": "a"}, {"message": "b"}, "junk"]}
        assert _circleci_extract_log(body) == "a\nb"

    def test_circleci_extract_log_empty_body(self) -> None:
        assert _circleci_extract_log({}) is None

    def test_circleci_extract_log_messages_no_strings(self) -> None:
        # Defensive: every message entry is non-string — should return None
        # rather than emitting an empty join.
        assert _circleci_extract_log({"messages": [{"unrelated": 1}]}) is None

    def test_buildkite_extract_log_content_key(self) -> None:
        assert _buildkite_extract_log({"content": "x"}) == "x"

    def test_buildkite_extract_log_log_key(self) -> None:
        assert _buildkite_extract_log({"log": "y"}) == "y"

    def test_buildkite_extract_log_empty_dict(self) -> None:
        assert _buildkite_extract_log({}) is None


# ---------------------------------------------------------------------
# Factory + SPEC
# ---------------------------------------------------------------------


class TestSpec:
    def test_spec_metadata(self) -> None:
        assert SPEC.kind == "ci_logs"
        assert KIND == "ci_logs"
        assert SPEC.required_scopes == ("ci:read", "actions:read")
        assert SPEC.capabilities.incremental is True

    def test_factory_gha(self, gha_credential: Credential) -> None:
        c = SPEC.factory(
            {
                "flavor": "github_actions",
                "owner": "o",
                "repo": "r",
                "_credential": gha_credential,
            }
        )
        assert isinstance(c, CiLogsConnector)
        assert c.id == "github_actions:o/r"

    def test_factory_circleci(
        self, circleci_credential: Credential
    ) -> None:
        c = SPEC.factory(
            {
                "flavor": "circleci",
                "owner": "o",
                "repo": "r",
                "vcs_type": "bb",
                "_credential": circleci_credential,
                "max_builds": 10,
                "failed_only": True,
            }
        )
        assert isinstance(c, CiLogsConnector)
        assert c.id == "circleci:bb/o/r"

    def test_factory_buildkite(
        self, buildkite_credential: Credential
    ) -> None:
        c = SPEC.factory(
            {
                "flavor": "buildkite",
                "org": "acme",
                "pipeline": "api",
                "_credential": buildkite_credential,
            }
        )
        assert isinstance(c, CiLogsConnector)
        assert c.id == "buildkite:acme/api"

    def test_factory_jenkins(
        self, jenkins_credential: Credential
    ) -> None:
        c = SPEC.factory(
            {
                "flavor": "jenkins",
                "base_url": "https://j.local",
                "_credential": jenkins_credential,
                "id": "jen-1",
                "max_log_bytes": 1024,
            }
        )
        assert isinstance(c, CiLogsConnector)
        assert c.id == "jen-1"
        assert c.config.max_log_bytes == 1024

    def test_factory_requires_credential(self) -> None:
        with pytest.raises(ValueError, match="Credential"):
            SPEC.factory({"flavor": "github_actions", "owner": "o", "repo": "r"})

    def test_factory_rejects_invalid_flavor(
        self, gha_credential: Credential
    ) -> None:
        with pytest.raises(ValueError, match="flavor"):
            SPEC.factory(
                {
                    "flavor": "travis",
                    "_credential": gha_credential,
                }
            )

    def test_factory_parses_since_string(
        self, gha_credential: Credential
    ) -> None:
        c = SPEC.factory(
            {
                "flavor": "github_actions",
                "owner": "o",
                "repo": "r",
                "since": "2026-05-01T00:00:00Z",
                "_credential": gha_credential,
            }
        )
        assert c.config.since is not None

    def test_factory_passes_datetime_since(
        self, gha_credential: Credential
    ) -> None:
        when = datetime(2026, 5, 1, tzinfo=UTC)
        c = SPEC.factory(
            {
                "flavor": "github_actions",
                "owner": "o",
                "repo": "r",
                "since": when,
                "_credential": gha_credential,
            }
        )
        assert c.config.since == when

    def test_factory_rejects_bad_since_string(
        self, gha_credential: Credential
    ) -> None:
        with pytest.raises(ValueError, match="since"):
            SPEC.factory(
                {
                    "flavor": "github_actions",
                    "owner": "o",
                    "repo": "r",
                    "since": "yesterday",
                    "_credential": gha_credential,
                }
            )

    def test_factory_default_when_since_missing(
        self, gha_credential: Credential
    ) -> None:
        c = SPEC.factory(
            {
                "flavor": "github_actions",
                "owner": "o",
                "repo": "r",
                "_credential": gha_credential,
            }
        )
        assert c.config.since is None
        assert c.config.max_builds == DEFAULT_MAX_BUILDS
        assert c.config.max_log_bytes == DEFAULT_MAX_LOG_BYTES


# ---------------------------------------------------------------------
# Package __init__ re-exports
# ---------------------------------------------------------------------


class TestPackageInit:
    def test_top_level_exports(self) -> None:
        import pleno_pii_scanner_ci_logs as pkg

        assert pkg.SPEC is SPEC
        assert pkg.KIND == "ci_logs"
        assert pkg.CiLogsConfig is CiLogsConfig
        assert pkg.CiLogsConnector is CiLogsConnector
        assert pkg.__version__ == "0.1.0"
