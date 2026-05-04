"""Tests for GdriveConnector — uses httpx.MockTransport doubles.

Token acquisition is bypassed by monkeypatching `_acquire_token` so we
do not need a real RSA private key during the test run.
"""

from __future__ import annotations

import json
from collections.abc import Callable

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
from pleno_pii_scanner.sources.base import DocumentRef
from pleno_pii_scanner_gdrive import (
    GdriveConfig,
    GdriveConnector,
    SPEC,
)


def _make_sa_json() -> str:
    """Build a JSON SA blob with a real (throwaway) RSA private key.

    Generated once per test session — `cryptography` rejects the
    obvious "FAKE" placeholder, and a real key keeps the signing path
    exercised end-to-end.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return json.dumps(
        {
            "type": "service_account",
            "client_email": "audit@pleno.iam.gserviceaccount.com",
            "private_key": pem,
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    )


SA_JSON = _make_sa_json()


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_registry_mod, "entry_points", lambda **_: [])
    _registry_mod._reset_for_tests()
    yield
    _registry_mod._reset_for_tests()


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _make(
    handler: Callable[[httpx.Request], httpx.Response],
    monkeypatch: pytest.MonkeyPatch,
    *,
    cfg: GdriveConfig | None = None,
) -> tuple[GdriveConnector, httpx.AsyncClient]:
    """Build a connector wired to a MockTransport client.

    `_acquire_token` is monkeypatched to a constant so we never sign a
    JWT or call the token endpoint by accident — the rare token tests
    override `monkeypatch` themselves.
    """
    client = _client(handler)
    config = cfg or GdriveConfig(
        service_account_json=SA_JSON, impersonate="audit@pleno.com"
    )
    c = GdriveConnector(config, client=client)

    async def _fake_token(self) -> str:
        return "test-tok"

    monkeypatch.setattr(GdriveConnector, "_acquire_token", _fake_token)
    return c, client


# --- config --------------------------------------------------------


class TestConfig:
    def test_rejects_empty_sa_json(self) -> None:
        with pytest.raises(ValueError, match="service_account_json"):
            GdriveConfig(service_account_json="", impersonate="x@y.com")

    def test_requires_impersonate_when_shared_drives(self) -> None:
        with pytest.raises(ValueError, match="impersonate"):
            GdriveConfig(
                service_account_json=SA_JSON, include_shared_drives=True
            )

    def test_allows_no_impersonate_when_shared_drives_off(self) -> None:
        cfg = GdriveConfig(
            service_account_json=SA_JSON, include_shared_drives=False
        )
        assert cfg.impersonate is None

    def test_rejects_bad_export_mime(self) -> None:
        with pytest.raises(ValueError, match="export_google_docs_as"):
            GdriveConfig(
                service_account_json=SA_JSON,
                impersonate="x@y.com",
                export_google_docs_as="text/html",  # type: ignore[arg-type]
            )

    def test_rejects_zero_max_size(self) -> None:
        with pytest.raises(ValueError, match="max_file_size_bytes"):
            GdriveConfig(
                service_account_json=SA_JSON,
                impersonate="x@y.com",
                max_file_size_bytes=0,
            )

    def test_rejects_invalid_json(self) -> None:
        with pytest.raises(ValueError, match="valid JSON"):
            GdriveConnector(
                GdriveConfig(
                    service_account_json="not-json{",
                    impersonate="x@y.com",
                )
            )

    def test_explicit_id(self) -> None:
        cfg = GdriveConfig(
            service_account_json=SA_JSON,
            impersonate="x@y.com",
            id="my-id",
        )
        assert cfg.resolved_id() == "my-id"

    def test_default_id_no_key_leak(self) -> None:
        cfg = GdriveConfig(
            service_account_json=SA_JSON,
            impersonate="x@y.com",
            drives=("d1", "d2"),
        )
        rid = cfg.resolved_id()
        assert "FAKE" not in rid
        assert rid.startswith("gdrive:")

    def test_default_id_order_independent(self) -> None:
        a = GdriveConfig(
            service_account_json=SA_JSON,
            impersonate="x@y.com",
            drives=("a", "b"),
        )
        b = GdriveConfig(
            service_account_json=SA_JSON,
            impersonate="x@y.com",
            drives=("b", "a"),
        )
        assert a.resolved_id() == b.resolved_id()


# --- protocol ------------------------------------------------------


class TestProtocol:
    def test_runtime_isinstance(self) -> None:
        c = GdriveConnector(
            GdriveConfig(
                service_account_json=SA_JSON, impersonate="x@y.com"
            )
        )
        assert isinstance(c, SourceConnector)

    def test_capabilities(self) -> None:
        c = GdriveConnector(
            GdriveConfig(
                service_account_json=SA_JSON, impersonate="x@y.com"
            )
        )
        assert c.capabilities() == Capabilities(
            incremental=True,
            binary=True,
            content_hash_delta=True,
            max_concurrent_fetches=4,
            streaming=False,
        )


# --- discover ------------------------------------------------------


def _drives_response(*ids: str) -> dict:
    return {"drives": [{"id": i, "name": i} for i in ids]}


def _files_response(*items: dict, next_page: str | None = None) -> dict:
    body: dict = {"files": list(items)}
    if next_page:
        body["nextPageToken"] = next_page
    return body


def _file(
    file_id: str,
    *,
    name: str = "f",
    mime: str = "text/plain",
    size: str | None = "10",
    md5: str | None = "abc",
    modified: str = "2026-04-01T00:00:00Z",
) -> dict:
    out: dict = {
        "id": file_id,
        "name": name,
        "mimeType": mime,
        "modifiedTime": modified,
    }
    if size is not None:
        out["size"] = size
    if md5 is not None:
        out["md5Checksum"] = md5
    return out


class TestDiscover:
    async def test_lists_my_drive_and_shared(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers.get("Authorization") == "Bearer test-tok"
            path = request.url.path
            seen_paths.append(path)
            if path.endswith("/drives"):
                return httpx.Response(200, json=_drives_response("d1"))
            if path.endswith("/files"):
                drive_id = request.url.params.get("driveId")
                if drive_id == "d1":
                    return httpx.Response(
                        200,
                        json=_files_response(_file("f-d1", name="shared.txt")),
                    )
                # My Drive (corpora=user, no driveId)
                return httpx.Response(
                    200,
                    json=_files_response(_file("f-root", name="mine.txt")),
                )
            return httpx.Response(404)

        c, client = _make(handler, monkeypatch)
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            paths = sorted(r.path for r in refs)
            assert paths == ["d1/f-d1", "root/f-root"]
            ref = next(r for r in refs if r.path == "d1/f-d1")
            assert ref.metadata["drive_id"] == "d1"
            assert ref.metadata["mime_type"] == "text/plain"
            assert ref.metadata["md5"] == "abc"
            assert ref.etag == "abc"
            assert ref.size == 10
            assert ref.last_modified is not None
            assert ref.native_url == "https://drive.google.com/file/d/f-d1/view"
        finally:
            await c.close()
            await client.aclose()
        # /drives was hit
        assert any(p.endswith("/drives") for p in seen_paths)

    async def test_pagination(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        page_calls: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/files"):
                token = request.url.params.get("pageToken")
                page_calls.append(token)
                if token is None:
                    return httpx.Response(
                        200,
                        json=_files_response(
                            _file("f1"), next_page="page2"
                        ),
                    )
                if token == "page2":
                    return httpx.Response(
                        200, json=_files_response(_file("f2"))
                    )
            return httpx.Response(404)

        cfg = GdriveConfig(
            service_account_json=SA_JSON,
            impersonate="x@y.com",
            drives=("root",),
        )
        c, client = _make(handler, monkeypatch, cfg=cfg)
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            assert sorted(r.path for r in refs) == ["root/f1", "root/f2"]
            # First call had no token, second had page2
            assert page_calls == [None, "page2"]
        finally:
            await c.close()
            await client.aclose()

    async def test_skip_shared_drives_listing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hit_drives = {"x": False}

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/drives"):
                hit_drives["x"] = True
                return httpx.Response(500)
            if path.endswith("/files"):
                return httpx.Response(200, json=_files_response(_file("f1")))
            return httpx.Response(404)

        cfg = GdriveConfig(
            service_account_json=SA_JSON,
            include_shared_drives=False,
        )
        c, client = _make(handler, monkeypatch, cfg=cfg)
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            assert [r.path for r in refs] == ["root/f1"]
            assert not hit_drives["x"]
        finally:
            await c.close()
            await client.aclose()

    async def test_drives_allowlist_skips_listing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hit_drives = {"x": False}

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/drives"):
                hit_drives["x"] = True
                return httpx.Response(500)
            if path.endswith("/files"):
                drive_id = request.url.params.get("driveId")
                return httpx.Response(
                    200,
                    json=_files_response(
                        _file(f"f-{drive_id or 'root'}")
                    ),
                )
            return httpx.Response(404)

        cfg = GdriveConfig(
            service_account_json=SA_JSON,
            impersonate="x@y.com",
            drives=("alpha", "beta"),
        )
        c, client = _make(handler, monkeypatch, cfg=cfg)
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            assert sorted(r.path for r in refs) == [
                "alpha/f-alpha",
                "beta/f-beta",
            ]
            assert not hit_drives["x"]
        finally:
            await c.close()
            await client.aclose()

    async def test_drives_pagination(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        drives_pages = [
            {"drives": [{"id": "d1"}], "nextPageToken": "p2"},
            {"drives": [{"id": "d2", "name": "n2"}, "garbage", {}]},
        ]
        idx = {"i": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/drives"):
                resp = drives_pages[idx["i"]]
                idx["i"] += 1
                return httpx.Response(200, json=resp)
            if path.endswith("/files"):
                drive_id = request.url.params.get("driveId") or "root"
                return httpx.Response(
                    200, json=_files_response(_file(f"f-{drive_id}"))
                )
            return httpx.Response(404)

        c, client = _make(handler, monkeypatch)
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            paths = sorted(r.path for r in refs)
            # root + d1 + d2 (garbage entries skipped)
            assert paths == ["d1/f-d1", "d2/f-d2", "root/f-root"]
        finally:
            await c.close()
            await client.aclose()

    async def test_filter_include_exclude(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/files"):
                return httpx.Response(
                    200,
                    json=_files_response(
                        _file("keep-me"),
                        _file("drop-me"),
                    ),
                )
            return httpx.Response(404)

        cfg = GdriveConfig(
            service_account_json=SA_JSON,
            impersonate="x@y.com",
            drives=("root",),
        )
        c, client = _make(handler, monkeypatch, cfg=cfg)
        try:
            refs = [
                r
                async for r in c.discover(
                    SourceFilter(include=("root/keep-*",)), None
                )
            ]
            assert [r.path for r in refs] == ["root/keep-me"]
        finally:
            await c.close()
            await client.aclose()

        c2, client2 = _make(handler, monkeypatch, cfg=cfg)
        try:
            refs = [
                r
                async for r in c2.discover(
                    SourceFilter(exclude=("root/drop-*",)), None
                )
            ]
            assert [r.path for r in refs] == ["root/keep-me"]
        finally:
            await c2.close()
            await client2.aclose()

    async def test_files_with_garbage_entries_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/files"):
                return httpx.Response(
                    200,
                    json={
                        "files": [
                            "not-a-mapping",
                            {"name": "no-id"},
                            {"id": 42, "name": "int-id"},
                            _file("ok-id"),
                            _file("bad-size", size="not-a-number"),
                        ]
                    },
                )
            return httpx.Response(404)

        cfg = GdriveConfig(
            service_account_json=SA_JSON,
            impersonate="x@y.com",
            drives=("root",),
        )
        c, client = _make(handler, monkeypatch, cfg=cfg)
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            paths = sorted(r.path for r in refs)
            assert paths == ["root/bad-size", "root/ok-id"]
            bad = next(r for r in refs if r.path == "root/bad-size")
            assert bad.size is None
        finally:
            await c.close()
            await client.aclose()

    async def test_file_with_no_md5_or_modified(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/files"):
                return httpx.Response(
                    200,
                    json={
                        "files": [
                            {
                                "id": "fx",
                                "name": "n",
                                "mimeType": "text/plain",
                                "modifiedTime": "garbage",
                            }
                        ]
                    },
                )
            return httpx.Response(404)

        cfg = GdriveConfig(
            service_account_json=SA_JSON,
            impersonate="x@y.com",
            drives=("root",),
        )
        c, client = _make(handler, monkeypatch, cfg=cfg)
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            assert refs[0].etag is None
            assert refs[0].size is None
            assert refs[0].last_modified is None
        finally:
            await c.close()
            await client.aclose()


# --- cursor --------------------------------------------------------


class TestCursor:
    async def test_resume_skips_done_drive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called_files = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/files"):
                called_files["n"] += 1
                return httpx.Response(
                    200, json=_files_response(_file("after-resume"))
                )
            return httpx.Response(404)

        cfg = GdriveConfig(
            service_account_json=SA_JSON,
            impersonate="x@y.com",
            drives=("root", "d2"),
        )
        c, client = _make(handler, monkeypatch, cfg=cfg)
        try:
            cursor = json.dumps({"root": "__done__"})
            refs = [
                r async for r in c.discover(SourceFilter(), cursor)
            ]
            # Only d2 walked
            assert [r.path for r in refs] == ["d2/after-resume"]
            assert called_files["n"] == 1
            # Each ref carries the post-page cursor
            decoded = json.loads(refs[0].metadata["_cursor"])
            assert decoded["d2"] == "__done__"
            assert decoded["root"] == "__done__"
        finally:
            await c.close()
            await client.aclose()

    async def test_resume_uses_page_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        observed_token = {"v": None}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/files"):
                observed_token["v"] = request.url.params.get("pageToken")
                return httpx.Response(
                    200, json=_files_response(_file("late"))
                )
            return httpx.Response(404)

        cfg = GdriveConfig(
            service_account_json=SA_JSON,
            impersonate="x@y.com",
            drives=("root",),
        )
        c, client = _make(handler, monkeypatch, cfg=cfg)
        try:
            cursor = json.dumps({"root": "carry-me"})
            refs = [r async for r in c.discover(SourceFilter(), cursor)]
            assert refs and refs[0].path == "root/late"
            assert observed_token["v"] == "carry-me"
        finally:
            await c.close()
            await client.aclose()

    async def test_unparseable_cursor_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = GdriveConfig(
            service_account_json=SA_JSON,
            impersonate="x@y.com",
            drives=("root",),
        )
        c, client = _make(lambda _: httpx.Response(404), monkeypatch, cfg=cfg)
        try:
            with pytest.raises(ValueError, match="unparseable cursor"):
                _ = [r async for r in c.discover(SourceFilter(), "{not-json")]
        finally:
            await c.close()
            await client.aclose()

    async def test_cursor_must_decode_to_dict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = GdriveConfig(
            service_account_json=SA_JSON,
            impersonate="x@y.com",
            drives=("root",),
        )
        c, client = _make(lambda _: httpx.Response(404), monkeypatch, cfg=cfg)
        try:
            with pytest.raises(ValueError, match="cursor must decode"):
                _ = [r async for r in c.discover(SourceFilter(), "[]")]
        finally:
            await c.close()
            await client.aclose()

    async def test_cursor_drops_non_string_entries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        observed = {"v": "unset"}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/files"):
                observed["v"] = request.url.params.get("pageToken")
                return httpx.Response(
                    200, json=_files_response(_file("ok"))
                )
            return httpx.Response(404)

        cfg = GdriveConfig(
            service_account_json=SA_JSON,
            impersonate="x@y.com",
            drives=("root",),
        )
        c, client = _make(handler, monkeypatch, cfg=cfg)
        try:
            cursor = json.dumps({"root": 42})  # int → dropped
            _ = [r async for r in c.discover(SourceFilter(), cursor)]
            # No saved token → first page request had no pageToken
            assert observed["v"] is None
        finally:
            await c.close()
            await client.aclose()


# --- fetch ---------------------------------------------------------


class TestFetch:
    async def test_native_doc_text_export(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/files"):
                return httpx.Response(
                    200,
                    json=_files_response(
                        _file(
                            "doc-1",
                            mime="application/vnd.google-apps.document",
                            size=None,
                            md5=None,
                        )
                    ),
                )
            if path.endswith("/files/doc-1/export"):
                assert request.url.params.get("mimeType") == "text/plain"
                return httpx.Response(200, text="hello plain world")
            return httpx.Response(404)

        cfg = GdriveConfig(
            service_account_json=SA_JSON,
            impersonate="x@y.com",
            drives=("root",),
        )
        c, client = _make(handler, monkeypatch, cfg=cfg)
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            docs = [d async for d in c.fetch(refs[0])]
            assert len(docs) == 1
            assert isinstance(docs[0], Document)
            assert docs[0].text == "hello plain world"
        finally:
            await c.close()
            await client.aclose()

    async def test_native_doc_pdf_export(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pdf_bytes = b"%PDF-1.7 ... bytes ..."

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/files"):
                return httpx.Response(
                    200,
                    json=_files_response(
                        _file(
                            "sheet-1",
                            mime="application/vnd.google-apps.spreadsheet",
                            size=None,
                            md5=None,
                        )
                    ),
                )
            if path.endswith("/files/sheet-1/export"):
                assert (
                    request.url.params.get("mimeType")
                    == "application/pdf"
                )
                return httpx.Response(
                    200, content=pdf_bytes, headers={"content-type": "application/pdf"}
                )
            return httpx.Response(404)

        cfg = GdriveConfig(
            service_account_json=SA_JSON,
            impersonate="x@y.com",
            drives=("root",),
            export_google_docs_as="application/pdf",
        )
        c, client = _make(handler, monkeypatch, cfg=cfg)
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            docs = [d async for d in c.fetch(refs[0])]
            assert isinstance(docs[0], Document)
            assert docs[0].binary == pdf_bytes
        finally:
            await c.close()
            await client.aclose()

    async def test_binary_alt_media(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body = b"\x89PNG\r\n\x1a\n binary"

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/files"):
                return httpx.Response(
                    200,
                    json=_files_response(
                        _file("png-1", mime="image/png", size="20")
                    ),
                )
            if path.endswith("/files/png-1"):
                assert request.url.params.get("alt") == "media"
                return httpx.Response(200, content=body)
            return httpx.Response(404)

        cfg = GdriveConfig(
            service_account_json=SA_JSON,
            impersonate="x@y.com",
            drives=("root",),
        )
        c, client = _make(handler, monkeypatch, cfg=cfg)
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            docs = [d async for d in c.fetch(refs[0])]
            assert isinstance(docs[0], Document)
            assert docs[0].binary == body
            assert docs[0].content_hash == "abc"
        finally:
            await c.close()
            await client.aclose()

    async def test_max_size_skip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called_get = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/files"):
                return httpx.Response(
                    200,
                    json=_files_response(
                        _file("big-1", mime="image/png", size="9999999")
                    ),
                )
            if path.endswith("/files/big-1"):
                called_get["n"] += 1
                return httpx.Response(200, content=b"never-served")
            return httpx.Response(404)

        cfg = GdriveConfig(
            service_account_json=SA_JSON,
            impersonate="x@y.com",
            drives=("root",),
            max_file_size_bytes=100,
        )
        c, client = _make(handler, monkeypatch, cfg=cfg)
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            assert len(refs) == 1
            docs = [d async for d in c.fetch(refs[0])]
            assert docs == []
            assert called_get["n"] == 0
        finally:
            await c.close()
            await client.aclose()

    async def test_fetch_size_unparseable_does_not_skip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/files"):
                # size is a non-int string — fetch should still proceed
                return httpx.Response(
                    200,
                    json=_files_response(
                        _file("ok", mime="text/plain", size="not-an-int")
                    ),
                )
            if path.endswith("/files/ok"):
                return httpx.Response(200, content=b"served")
            return httpx.Response(404)

        cfg = GdriveConfig(
            service_account_json=SA_JSON,
            impersonate="x@y.com",
            drives=("root",),
        )
        c, client = _make(handler, monkeypatch, cfg=cfg)
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            docs = [d async for d in c.fetch(refs[0])]
            assert docs and docs[0].binary == b"served"
        finally:
            await c.close()
            await client.aclose()

    async def test_fetch_missing_file_id_yields_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        c, client = _make(lambda _: httpx.Response(404), monkeypatch)
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind=c.kind,
                path="x/y",
                metadata={},
            )
            docs = [d async for d in c.fetch(ref)]
            assert docs == []
        finally:
            await c.close()
            await client.aclose()


# --- token ---------------------------------------------------------


class TestToken:
    async def test_token_acquired_once_and_cached(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        token_calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/token"):
                token_calls["n"] += 1
                return httpx.Response(
                    200,
                    json={"access_token": "tok-fresh", "expires_in": 3600},
                )
            if path.endswith("/files"):
                assert (
                    request.headers.get("Authorization")
                    == "Bearer tok-fresh"
                )
                return httpx.Response(
                    200, json=_files_response(_file("a"), _file("b"))
                )
            return httpx.Response(404)

        # NOTE: do NOT inject `_acquire_token` here — exercise the real path.
        client = _client(handler)
        cfg = GdriveConfig(
            service_account_json=SA_JSON,
            impersonate="x@y.com",
            drives=("root",),
        )
        c = GdriveConnector(cfg, client=client)

        # Make signing deterministic by stubbing `_build_jwt_assertion`
        # so we do not require the cryptography backend in tests.
        monkeypatch.setattr(
            GdriveConnector, "_build_jwt_assertion", lambda self, now: "stub.jwt.assertion"
        )
        try:
            # Two iterations exhaust files; token should refresh only once.
            refs = [r async for r in c.discover(SourceFilter(), None)]
            assert len(refs) == 2
            # Force a second discover — token still cached.
            _ = [
                r
                async for r in c.discover(
                    SourceFilter(),
                    json.dumps({"root": "__done__"}),
                )
            ]
            assert token_calls["n"] == 1
        finally:
            await c.close()
            await client.aclose()

    async def test_token_refresh_after_expiry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        token_calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/token"):
                token_calls["n"] += 1
                return httpx.Response(
                    200,
                    json={
                        "access_token": f"tok-{token_calls['n']}",
                        "expires_in": 60,  # 60s TTL → cache TTL clamps to 1s
                    },
                )
            if request.url.path.endswith("/files"):
                return httpx.Response(
                    200, json=_files_response(_file("x"))
                )
            return httpx.Response(404)

        client = _client(handler)
        cfg = GdriveConfig(
            service_account_json=SA_JSON,
            impersonate="x@y.com",
            drives=("root",),
        )
        c = GdriveConnector(cfg, client=client)
        monkeypatch.setattr(
            GdriveConnector, "_build_jwt_assertion", lambda self, now: "stub"
        )
        try:
            await c._acquire_token()
            # Force expiry by rolling the clock forward.
            c._token_expires_at = 0.0
            await c._acquire_token()
            assert token_calls["n"] == 2
        finally:
            await c.close()
            await client.aclose()

    async def test_token_endpoint_missing_field_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/token"):
                return httpx.Response(200, json={"expires_in": 60})
            return httpx.Response(404)

        client = _client(handler)
        cfg = GdriveConfig(
            service_account_json=SA_JSON,
            impersonate="x@y.com",
        )
        c = GdriveConnector(cfg, client=client)
        monkeypatch.setattr(
            GdriveConnector, "_build_jwt_assertion", lambda self, now: "stub"
        )
        try:
            with pytest.raises(RuntimeError, match="access_token"):
                await c._acquire_token()
        finally:
            await c.close()
            await client.aclose()

    async def test_cached_token_returns_immediately(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        token_calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/token"):
                token_calls["n"] += 1
                return httpx.Response(
                    200,
                    json={"access_token": "cached-tok", "expires_in": 3600},
                )
            return httpx.Response(404)

        client = _client(handler)
        c = GdriveConnector(
            GdriveConfig(
                service_account_json=SA_JSON, impersonate="x@y.com"
            ),
            client=client,
        )
        monkeypatch.setattr(
            GdriveConnector, "_build_jwt_assertion", lambda self, now: "stub"
        )
        try:
            t1 = await c._acquire_token()
            t2 = await c._acquire_token()
            assert t1 == t2 == "cached-tok"
            assert token_calls["n"] == 1
        finally:
            await c.close()
            await client.aclose()

    def test_jwt_assertion_includes_subject(self) -> None:
        cfg = GdriveConfig(
            service_account_json=SA_JSON,
            impersonate="audit@pleno.com",
        )
        c = GdriveConnector(cfg)
        # We can't easily verify the RSA signature without
        # `cryptography`, but we can at least confirm the assertion is
        # a 3-part JWT and the claim segment carries our subject.
        assertion = c._build_jwt_assertion(1700000000.0)
        parts = assertion.split(".")
        assert len(parts) == 3
        # Decode the claim segment — base64url, may need padding.
        import base64

        claim_b = parts[1] + "=" * (-len(parts[1]) % 4)
        claim = json.loads(base64.urlsafe_b64decode(claim_b))
        assert claim["sub"] == "audit@pleno.com"
        assert claim["scope"] == "https://www.googleapis.com/auth/drive.readonly"
        assert claim["iss"] == "audit@pleno.iam.gserviceaccount.com"


# --- helpers -------------------------------------------------------


class TestHelpers:
    def test_parse_iso_utc_handles_none_and_naive(self) -> None:
        from pleno_pii_scanner_gdrive.connector import _parse_iso_utc

        assert _parse_iso_utc(None) is None
        assert _parse_iso_utc("") is None
        # Naive datetime → tzinfo defaults to UTC.
        dt = _parse_iso_utc("2026-04-01T00:00:00")
        assert dt is not None and dt.tzinfo is not None


# --- spec / factory ------------------------------------------------


class TestSpec:
    def test_metadata(self) -> None:
        assert SPEC.kind == "gdrive"
        assert SPEC.version == "0.1.0"
        assert "https://www.googleapis.com/auth/drive.readonly" in SPEC.required_scopes

    def test_factory_minimal(self) -> None:
        register(SPEC)
        c = create(
            "gdrive",
            {
                "service_account_json": SA_JSON,
                "impersonate": "x@y.com",
            },
        )
        assert isinstance(c, GdriveConnector)

    def test_factory_full(self) -> None:
        register(SPEC)
        c = create(
            "gdrive",
            {
                "service_account_json": SA_JSON,
                "impersonate": "x@y.com",
                "drives": ["d1", "d2"],
                "include_shared_drives": False,
                "max_file_size_bytes": 1234,
                "export_google_docs_as": "application/pdf",
                "id": "explicit-id",
            },
        )
        assert c.id == "explicit-id"

    def test_factory_rejects_missing_sa_json(self) -> None:
        with pytest.raises(ValueError, match="service_account_json"):
            SPEC.factory({})


# --- close ---------------------------------------------------------


class TestClose:
    async def test_close_owns_client(self) -> None:
        c = GdriveConnector(
            GdriveConfig(
                service_account_json=SA_JSON, impersonate="x@y.com"
            )
        )
        await c.close()

    async def test_close_external_client_not_closed(self) -> None:
        client = httpx.AsyncClient()
        c = GdriveConnector(
            GdriveConfig(
                service_account_json=SA_JSON, impersonate="x@y.com"
            ),
            client=client,
        )
        await c.close()
        assert not client.is_closed
        await client.aclose()
