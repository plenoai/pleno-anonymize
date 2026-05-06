"""Tests for SalesforceConnector — uses httpx.MockTransport doubles.

Authentication is bypassed by monkeypatching `_acquire_token` so tests
do not have to mint a real RS256 JWT against a deterministic clock.
The JWT bearer flow itself is exercised in a dedicated test that
swaps in a fake `_rs256_sign` and asserts the POST body shape.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from pleno_pii_scanner.sources import (
    Capabilities,
    Document,
    SourceConnector,
    SourceFilter,
    create,
    register,
)
from pleno_pii_scanner.sources import registry as _registry_mod
from pleno_pii_scanner_salesforce import (
    SPEC,
    SalesforceConfig,
    SalesforceConnector,
)
from pleno_pii_scanner_salesforce import connector as _connector_mod


_INSTANCE = "https://acme.my.salesforce.com"
_FAKE_PEM = "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----\n"


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_registry_mod, "entry_points", lambda **_: [])
    _registry_mod._reset_for_tests()
    yield
    _registry_mod._reset_for_tests()


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=_INSTANCE,
        transport=httpx.MockTransport(handler),
    )


def _make_config(**overrides: object) -> SalesforceConfig:
    base: dict[str, object] = dict(
        instance_url=_INSTANCE,
        client_id="3MVG9...",
        username="scanner@acme.com",
        private_key_pem=_FAKE_PEM,
    )
    base.update(overrides)
    return SalesforceConfig(**base)  # type: ignore[arg-type]


def _stub_token(c: SalesforceConnector) -> None:
    """Bypass real JWT signing — return a fixed bearer indefinitely."""

    async def fake() -> str:
        return "ACCESS-TOKEN"

    c._acquire_token = fake  # type: ignore[method-assign]


def _describe_response(*field_names: str) -> dict[str, object]:
    return {"fields": [{"name": n, "type": "string"} for n in field_names]}


# --- config --------------------------------------------------------


class TestConfig:
    def test_rejects_empty_instance_url(self) -> None:
        with pytest.raises(ValueError, match="instance_url"):
            _make_config(instance_url="")

    def test_rejects_empty_client_id(self) -> None:
        with pytest.raises(ValueError, match="client_id"):
            _make_config(client_id="")

    def test_rejects_empty_username(self) -> None:
        with pytest.raises(ValueError, match="username"):
            _make_config(username="")

    def test_rejects_empty_private_key_pem(self) -> None:
        with pytest.raises(ValueError, match="private_key_pem"):
            _make_config(private_key_pem="")

    def test_rejects_empty_sobjects(self) -> None:
        with pytest.raises(ValueError, match="sobjects"):
            _make_config(sobjects=())

    def test_explicit_id(self) -> None:
        cfg = _make_config(id="custom-id")
        assert cfg.resolved_id() == "custom-id"

    def test_default_id_no_pem_leak(self) -> None:
        cfg = _make_config(private_key_pem="VERY-SECRET-PEM")
        rid = cfg.resolved_id()
        assert "VERY-SECRET-PEM" not in rid
        assert rid.startswith("salesforce:")

    def test_default_id_stable(self) -> None:
        a = _make_config()
        b = _make_config()
        assert a.resolved_id() == b.resolved_id()

    def test_default_sobjects(self) -> None:
        cfg = _make_config()
        assert cfg.sobjects == ("Case", "Account", "Opportunity", "User")


# --- protocol ------------------------------------------------------


class TestProtocol:
    def test_runtime_isinstance(self) -> None:
        c = SalesforceConnector(_make_config())
        assert isinstance(c, SourceConnector)

    def test_capabilities(self) -> None:
        c = SalesforceConnector(_make_config())
        assert c.capabilities() == Capabilities(
            incremental=True,
            binary=False,
            content_hash_delta=False,
            max_concurrent_fetches=4,
            streaming=False,
        )


# --- discover end-to-end ------------------------------------------


def _sobject_handler(
    *,
    fields_by_sobject: dict[str, tuple[str, ...]],
    pages_by_sobject: dict[str, list[dict[str, object]]],
    seen_authorisations: list[str] | None = None,
    seen_paths: list[str] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Build a MockTransport handler that serves describe + paged query
    responses keyed by sobject name."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen_authorisations is not None:
            seen_authorisations.append(request.headers.get("Authorization", ""))
        path_with_query = request.url.path
        if request.url.query:
            path_with_query = f"{request.url.path}?{request.url.query.decode()}"
        if seen_paths is not None:
            seen_paths.append(path_with_query)
        # describe
        for sobject, fields in fields_by_sobject.items():
            if request.url.path.endswith(f"/sobjects/{sobject}/describe"):
                return httpx.Response(200, json=_describe_response(*fields))
        # initial query — match by sobject name in the q= param
        if request.url.path.endswith("/query"):
            q = request.url.params.get("q", "")
            for sobject, pages in pages_by_sobject.items():
                if f"FROM {sobject}" in q:
                    return httpx.Response(200, json=pages[0])
            return httpx.Response(404)
        # nextRecordsUrl pagination — Salesforce returns paths shaped
        # like `/services/data/v60.0/query/01g...-2000`
        if "/query/" in request.url.path:
            for sobject, pages in pages_by_sobject.items():
                for page in pages:
                    npath = page.get("nextRecordsUrl")
                    if isinstance(npath, str) and request.url.path.endswith(
                        npath.rsplit("/", 1)[-1]
                    ):
                        idx = pages.index(page)
                        return httpx.Response(200, json=pages[idx + 1])
            return httpx.Response(404)
        return httpx.Response(404, content=str(request.url).encode())

    return handler


class TestDiscover:
    async def test_yields_one_ref_per_record(self) -> None:
        handler = _sobject_handler(
            fields_by_sobject={
                "Case": ("Subject", "Description", "LastModifiedDate"),
            },
            pages_by_sobject={
                "Case": [
                    {
                        "done": True,
                        "totalSize": 1,
                        "records": [
                            {
                                "attributes": {
                                    "type": "Case",
                                    "url": "/services/data/v60.0/sobjects/Case/500X",
                                },
                                "Id": "500X",
                                "Subject": "Customer call",
                                "Description": "alice@example.com phoned",
                                "LastModifiedDate": "2026-01-02T03:04:05.000+0000",
                            }
                        ],
                    }
                ]
            },
        )
        async with _client(handler) as client:
            c = SalesforceConnector(_make_config(sobjects=("Case",)), client=client)
            _stub_token(c)
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                assert len(refs) == 1
                ref = refs[0]
                assert ref.path == "Case/500X"
                assert ref.metadata["sobject"] == "Case"
                assert ref.metadata["record_id"] == "500X"
                assert ref.metadata["lastModifiedDate"].startswith("2026")
                assert ref.content_type == "application/json"
                assert ref.last_modified is not None
                assert ref.native_url and "Case/500X" in ref.native_url
            finally:
                await c.close()

    async def test_authorization_header_sent_as_bearer(self) -> None:
        seen: list[str] = []
        handler = _sobject_handler(
            fields_by_sobject={"Case": ("Subject",)},
            pages_by_sobject={"Case": [{"done": True, "totalSize": 0, "records": []}]},
            seen_authorisations=seen,
        )
        async with _client(handler) as client:
            c = SalesforceConnector(_make_config(sobjects=("Case",)), client=client)
            _stub_token(c)
            try:
                _ = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert seen, "expected at least one request"
        assert all(h == "Bearer ACCESS-TOKEN" for h in seen)

    async def test_describe_drives_select_field_list(self) -> None:
        seen_paths: list[str] = []
        handler = _sobject_handler(
            fields_by_sobject={
                "Case": ("Subject", "Description", "OwnerId"),
            },
            pages_by_sobject={"Case": [{"done": True, "totalSize": 0, "records": []}]},
            seen_paths=seen_paths,
        )
        async with _client(handler) as client:
            c = SalesforceConnector(_make_config(sobjects=("Case",)), client=client)
            _stub_token(c)
            try:
                _ = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        # Expect exactly one describe call followed by one query whose
        # SOQL covers Id + every described field.
        described = [p for p in seen_paths if "/describe" in p]
        assert len(described) == 1
        queried = [p for p in seen_paths if "/query?" in p]
        assert len(queried) == 1
        assert "SELECT" in queried[0]
        for col in ("Id", "Subject", "Description", "OwnerId"):
            assert col in queried[0]
        assert "FROM%20Case" in queried[0] or "FROM+Case" in queried[0]

    async def test_pagination_via_next_records_url(self) -> None:
        next_path = "/services/data/v60.0/query/01g0000000abc-200"
        page_one = {
            "done": False,
            "totalSize": 2,
            "nextRecordsUrl": next_path,
            "records": [
                {
                    "attributes": {"type": "Case"},
                    "Id": "500A",
                    "Subject": "first",
                }
            ],
        }
        page_two = {
            "done": True,
            "totalSize": 2,
            "records": [
                {
                    "attributes": {"type": "Case"},
                    "Id": "500B",
                    "Subject": "second",
                }
            ],
        }
        handler = _sobject_handler(
            fields_by_sobject={"Case": ("Subject",)},
            pages_by_sobject={"Case": [page_one, page_two]},
        )
        async with _client(handler) as client:
            c = SalesforceConnector(_make_config(sobjects=("Case",)), client=client)
            _stub_token(c)
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                assert [r.path for r in refs] == ["Case/500A", "Case/500B"]
                # Cursor cleared after a clean walk.
                assert c.cursor_after_run() is None
            finally:
                await c.close()

    async def test_cursor_after_run_carries_partial_progress(self) -> None:
        # Simulate an incomplete walk: the connector saw a
        # nextRecordsUrl on page 1 but the consumer stopped iterating
        # before page 2 — cursor must still be the last seen URL so the
        # next run resumes mid-paginate.
        next_path = "/services/data/v60.0/query/01g-200"
        page_one = {
            "done": False,
            "totalSize": 100,
            "nextRecordsUrl": next_path,
            "records": [
                {
                    "attributes": {"type": "Case"},
                    "Id": "500A",
                    "Subject": "first",
                }
            ],
        }
        handler = _sobject_handler(
            fields_by_sobject={"Case": ("Subject",)},
            pages_by_sobject={"Case": [page_one, {"done": True, "records": []}]},
        )
        async with _client(handler) as client:
            c = SalesforceConnector(_make_config(sobjects=("Case",)), client=client)
            _stub_token(c)
            try:
                gen = c.discover(SourceFilter(), None)
                # Pull only the first record, then stop.
                first = await gen.__anext__()
                assert first.path == "Case/500A"
                await gen.aclose()
                cursor = c.cursor_after_run()
                assert cursor is not None
                assert json.loads(cursor) == {"Case": next_path}
            finally:
                await c.close()

    async def test_cursor_resume_hits_next_records_url(self) -> None:
        resume_path = "/services/data/v60.0/query/01g0000000abc-200"
        page_two = {
            "done": True,
            "totalSize": 1,
            "records": [
                {
                    "attributes": {"type": "Case"},
                    "Id": "500Z",
                    "Subject": "resumed",
                }
            ],
        }
        seen_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_paths.append(request.url.path)
            if request.url.path == resume_path:
                return httpx.Response(200, json=page_two)
            return httpx.Response(
                500, content=b"unexpected describe/query call on resume"
            )

        async with _client(handler) as client:
            c = SalesforceConnector(_make_config(sobjects=("Case",)), client=client)
            _stub_token(c)
            cursor = json.dumps({"Case": resume_path}, sort_keys=True)
            try:
                refs = [r async for r in c.discover(SourceFilter(), cursor)]
                assert [r.path for r in refs] == ["Case/500Z"]
            finally:
                await c.close()
        # Only the resume URL should be hit — no describe, no initial query.
        assert seen_paths == [resume_path]

    async def test_record_text_strips_attributes(self) -> None:
        handler = _sobject_handler(
            fields_by_sobject={"Case": ("Subject",)},
            pages_by_sobject={
                "Case": [
                    {
                        "done": True,
                        "totalSize": 1,
                        "records": [
                            {
                                "attributes": {"type": "Case", "url": "/x"},
                                "Id": "500X",
                                "Subject": "hello",
                            }
                        ],
                    }
                ]
            },
        )
        async with _client(handler) as client:
            c = SalesforceConnector(_make_config(sobjects=("Case",)), client=client)
            _stub_token(c)
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                docs = [d async for d in c.fetch(refs[0])]
                assert len(docs) == 1
                doc = docs[0]
                assert isinstance(doc, Document)
                payload = json.loads(doc.text or "")
                assert "attributes" not in payload
                assert payload["Id"] == "500X"
                assert payload["Subject"] == "hello"
                assert doc.extra["sobject"] == "Case"
            finally:
                await c.close()

    async def test_record_without_id_skipped(self) -> None:
        handler = _sobject_handler(
            fields_by_sobject={"Case": ("Subject",)},
            pages_by_sobject={
                "Case": [
                    {
                        "done": True,
                        "totalSize": 2,
                        "records": [
                            {"attributes": {"type": "Case"}, "Subject": "no-id"},
                            {
                                "attributes": {"type": "Case"},
                                "Id": "500X",
                                "Subject": "ok",
                            },
                        ],
                    }
                ]
            },
        )
        async with _client(handler) as client:
            c = SalesforceConnector(_make_config(sobjects=("Case",)), client=client)
            _stub_token(c)
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                assert [r.path for r in refs] == ["Case/500X"]
            finally:
                await c.close()


# --- multi-sobject -----------------------------------------------


class TestMultiSObject:
    async def test_only_configured_sobjects_queried(self) -> None:
        described: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if "/describe" in request.url.path:
                described.append(request.url.path)
                return httpx.Response(200, json=_describe_response("Id"))
            if request.url.path.endswith("/query"):
                return httpx.Response(
                    200, json={"done": True, "totalSize": 0, "records": []}
                )
            return httpx.Response(404)

        async with _client(handler) as client:
            c = SalesforceConnector(
                _make_config(sobjects=("Account", "User")), client=client
            )
            _stub_token(c)
            try:
                _ = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        names = sorted(p.split("/sobjects/")[1].split("/", 1)[0] for p in described)
        assert names == ["Account", "User"]


# --- filter ------------------------------------------------------


class TestFilter:
    async def test_include_filter_keeps_matching_only(self) -> None:
        records = [
            {"attributes": {"type": "Case"}, "Id": "500A", "Subject": "a"},
            {"attributes": {"type": "Case"}, "Id": "500B", "Subject": "b"},
        ]
        handler = _sobject_handler(
            fields_by_sobject={"Case": ("Subject",)},
            pages_by_sobject={
                "Case": [{"done": True, "totalSize": 2, "records": records}]
            },
        )
        async with _client(handler) as client:
            c = SalesforceConnector(_make_config(sobjects=("Case",)), client=client)
            _stub_token(c)
            try:
                refs = [
                    r
                    async for r in c.discover(
                        SourceFilter(include=("Case/500A",)), None
                    )
                ]
                assert [r.path for r in refs] == ["Case/500A"]
            finally:
                await c.close()

    async def test_exclude_filter_drops_matching(self) -> None:
        records = [
            {"attributes": {"type": "Case"}, "Id": "500A", "Subject": "a"},
            {"attributes": {"type": "Case"}, "Id": "500B", "Subject": "b"},
        ]
        handler = _sobject_handler(
            fields_by_sobject={"Case": ("Subject",)},
            pages_by_sobject={
                "Case": [{"done": True, "totalSize": 2, "records": records}]
            },
        )
        async with _client(handler) as client:
            c = SalesforceConnector(_make_config(sobjects=("Case",)), client=client)
            _stub_token(c)
            try:
                refs = [
                    r
                    async for r in c.discover(
                        SourceFilter(exclude=("Case/500B",)), None
                    )
                ]
                assert [r.path for r in refs] == ["Case/500A"]
            finally:
                await c.close()


# --- JWT bearer flow --------------------------------------------


class TestJwtBearerFlow:
    async def test_token_post_uses_jwt_bearer_grant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Bypass real RS256 signing — emit a deterministic byte string.
        monkeypatch.setattr(
            _connector_mod,
            "_rs256_sign",
            lambda pem, data: b"FAKE-SIGNATURE-BYTES",
        )

        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/services/oauth2/token"):
                captured["body"] = request.content.decode()
                captured["url"] = str(request.url)
                return httpx.Response(
                    200,
                    json={
                        "access_token": "FRESH-TOKEN",
                        "token_type": "Bearer",
                        "instance_url": _INSTANCE,
                        "expires_in": 3600,
                    },
                )
            return httpx.Response(404)

        async with _client(handler) as client:
            c = SalesforceConnector(_make_config(), client=client)
            try:
                token = await c._acquire_token()
                assert token == "FRESH-TOKEN"
                # Cached: a second call returns the same token without
                # hitting the endpoint again.
                token_again = await c._acquire_token()
                assert token_again == "FRESH-TOKEN"
            finally:
                await c.close()
        body = captured["body"]
        assert isinstance(body, str)
        assert (
            "grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer" in body
        )
        assert "assertion=" in body
        assertion = body.split("assertion=", 1)[1].split("&", 1)[0]
        # Three parts: header.payload.signature
        parts = assertion.split(".")
        assert len(parts) == 3
        header = json.loads(_b64url_decode(parts[0]))
        payload = json.loads(_b64url_decode(parts[1]))
        assert header == {"alg": "RS256", "typ": "JWT"}
        assert payload["iss"] == "3MVG9..."
        assert payload["sub"] == "scanner@acme.com"
        assert payload["aud"] == "https://login.salesforce.com"
        assert payload["exp"] > 0

    async def test_token_response_missing_access_token_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"token_type": "Bearer"})

        async with _client(handler) as client:
            c = SalesforceConnector(_make_config(), client=client)
            # Stub signing so we don't need cryptography here.
            c._token = None  # belt-and-braces

            async def fake_assertion(*_: object, **__: object) -> str:
                return "x.y.z"

            # monkeypatch the module-level signer
            import pleno_pii_scanner_salesforce.connector as mod

            original = mod._sign_jwt_bearer_assertion
            mod._sign_jwt_bearer_assertion = lambda **_: "x.y.z"  # type: ignore[assignment]
            try:
                with pytest.raises(ValueError, match="access_token"):
                    await c._acquire_token()
            finally:
                mod._sign_jwt_bearer_assertion = original  # type: ignore[assignment]
                await c.close()

    async def test_cached_token_returned_within_expiry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Pre-populate cache with a non-expired token; ensure no HTTP
        # round-trip is made on `_acquire_token`.
        async with _client(
            lambda r: httpx.Response(500, content=b"should not be called")
        ) as client:
            c = SalesforceConnector(_make_config(), client=client)
            c._token = _connector_mod._CachedToken(
                value="CACHED",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            try:
                assert await c._acquire_token() == "CACHED"
            finally:
                await c.close()


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


# --- SOQL helper -------------------------------------------------


class TestSoql:
    def test_id_always_first_and_deduplicated(self) -> None:
        sql = _connector_mod._build_soql("Case", ("Id", "Subject", "id", "Description"))
        # Id appears once, first, regardless of describe casing.
        assert sql.startswith("SELECT Id, ")
        # 'id' duplicate suppressed; other fields preserved in order.
        cols = sql.split("SELECT ", 1)[1].split(" FROM", 1)[0].split(", ")
        assert cols == ["Id", "Subject", "Description"]
        assert sql.endswith("FROM Case")


# --- cursor codec -----------------------------------------------


class TestCursorCodec:
    def test_decode_empty(self) -> None:
        assert _connector_mod._decode_cursor(None) == {}
        assert _connector_mod._decode_cursor("") == {}

    def test_decode_malformed_json(self) -> None:
        assert _connector_mod._decode_cursor("not-json") == {}

    def test_decode_non_object(self) -> None:
        assert _connector_mod._decode_cursor("[1,2,3]") == {}

    def test_decode_drops_non_string_values(self) -> None:
        decoded = _connector_mod._decode_cursor(
            json.dumps({"Case": "/path", "Bad": 42})
        )
        assert decoded == {"Case": "/path"}

    def test_encode_sort_keys_stable(self) -> None:
        a = _connector_mod._encode_cursor({"B": "/b", "A": "/a"})
        b = _connector_mod._encode_cursor({"A": "/a", "B": "/b"})
        assert a == b


# --- helpers ------------------------------------------------------


class TestHelpers:
    def test_parse_iso_handles_z_suffix(self) -> None:
        dt = _connector_mod._parse_iso("2026-01-02T03:04:05Z")
        assert dt is not None
        assert dt.year == 2026

    def test_parse_iso_returns_none_on_garbage(self) -> None:
        assert _connector_mod._parse_iso("nope") is None
        assert _connector_mod._parse_iso(None) is None
        assert _connector_mod._parse_iso("") is None
        assert _connector_mod._parse_iso(42) is None

    def test_record_url_uses_lightning_path(self) -> None:
        url = _connector_mod._record_url(_INSTANCE, "Case", "500X")
        assert url == f"{_INSTANCE}/lightning/r/Case/500X/view"

    def test_describe_skips_non_mapping_field_entries(self) -> None:
        # Defensive: malformed describe payload must not crash.
        async def run() -> tuple[str, ...]:
            def handler(request: httpx.Request) -> httpx.Response:
                if "/describe" in request.url.path:
                    return httpx.Response(
                        200,
                        json={
                            "fields": [
                                "not-a-mapping",
                                {"name": "Subject"},
                                {"name": 42},  # non-string name
                                {},  # missing name
                            ]
                        },
                    )
                return httpx.Response(404)

            async with _client(handler) as client:
                c = SalesforceConnector(_make_config(sobjects=("Case",)), client=client)
                _stub_token(c)
                try:
                    return await c._describe_fields("Case")
                finally:
                    await c.close()

        import asyncio

        fields = asyncio.run(run())
        assert fields == ("Subject",)


# --- fetch edges --------------------------------------------------


class TestFetchEdges:
    async def test_fetch_unknown_path_returns_empty(self) -> None:
        from pleno_pii_scanner.sources.base import DocumentRef

        async with _client(lambda _r: httpx.Response(404)) as client:
            c = SalesforceConnector(_make_config(), client=client)
            try:
                ref = DocumentRef(source_id=c.id, source_kind=c.kind, path="x")
                docs = [d async for d in c.fetch(ref)]
                assert docs == []
            finally:
                await c.close()


# --- spec / factory ----------------------------------------------


class TestSpec:
    def test_metadata(self) -> None:
        assert SPEC.kind == "salesforce"
        assert SPEC.version == "0.1.0"
        assert SPEC.required_scopes == ("api refresh_token",)
        assert SPEC.capabilities.incremental is True

    def test_factory_minimal(self) -> None:
        register(SPEC)
        c = create(
            "salesforce",
            {
                "instance_url": _INSTANCE,
                "client_id": "k",
                "username": "u@example.com",
                "private_key_pem": _FAKE_PEM,
            },
        )
        assert isinstance(c, SalesforceConnector)

    def test_factory_full(self) -> None:
        register(SPEC)
        c = create(
            "salesforce",
            {
                "instance_url": _INSTANCE,
                "client_id": "k",
                "username": "u@example.com",
                "private_key_pem": _FAKE_PEM,
                "sobjects": ["Account"],
                "api_version": "v59.0",
                "page_size": 100,
                "id": "x",
            },
        )
        assert c.id == "x"

    def test_factory_rejects_missing_fields(self) -> None:
        for missing in (
            "instance_url",
            "client_id",
            "username",
            "private_key_pem",
        ):
            cfg = {
                "instance_url": _INSTANCE,
                "client_id": "k",
                "username": "u",
                "private_key_pem": _FAKE_PEM,
            }
            cfg.pop(missing)
            with pytest.raises(ValueError, match=missing):
                SPEC.factory(cfg)


# --- close --------------------------------------------------------


class TestClose:
    async def test_close_owns_client(self) -> None:
        c = SalesforceConnector(_make_config())
        await c.close()

    async def test_close_external_client_not_closed(self) -> None:
        client = httpx.AsyncClient()
        c = SalesforceConnector(_make_config(), client=client)
        await c.close()
        assert not client.is_closed
        await client.aclose()


# --- RS256 signer (real cryptography) ---------------------------


class TestRsaSigning:
    """Exercise the real `_rs256_sign` path so we don't ship a code
    path that only ever runs in production."""

    def test_sign_round_trips_with_generated_key(self) -> None:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa

        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8")
        sig = _connector_mod._rs256_sign(pem, b"hello")
        # Verify with the matching public key.
        private.public_key().verify(sig, b"hello", padding.PKCS1v15(), hashes.SHA256())

    def test_assertion_layout(self) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8")
        token = _connector_mod._sign_jwt_bearer_assertion(
            client_id="cid",
            username="u@example.com",
            private_key_pem=pem,
            audience="https://login.salesforce.com",
            now=datetime(2026, 1, 1, tzinfo=UTC),
        )
        header_b64, payload_b64, _sig_b64 = token.split(".")
        header = json.loads(_b64url_decode(header_b64))
        payload = json.loads(_b64url_decode(payload_b64))
        assert header == {"alg": "RS256", "typ": "JWT"}
        assert payload["iss"] == "cid"
        assert payload["sub"] == "u@example.com"
        assert payload["aud"] == "https://login.salesforce.com"
        # `exp` exactly 180 s after `iat` (Salesforce 5-min cap, our 3-min
        # safety margin).
        assert payload["exp"] == int(datetime(2026, 1, 1, tzinfo=UTC).timestamp()) + 180
