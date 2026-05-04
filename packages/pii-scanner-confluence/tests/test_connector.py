"""ConfluenceConnector tests.

Cloud + Data Center get **separate test classes** (`TestCloud`,
`TestDatacenter`) so the matrix is explicit — both flavors are
exercised against the full discover/fetch/cursor pipeline. Shared
behavior (config validation, factory, lifecycle, helpers) lives in
its own classes.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from pleno_pii_scanner.credentials.broker import Credential
from pleno_pii_scanner.scheduler.rate_limit import BucketKey, RateLimited
from pleno_pii_scanner.sources.base import (
    Capabilities,
    Document,
    DocumentRef,
    SourceConnector,
    SourceFilter,
)
from pleno_pii_scanner_confluence.connector import (
    KIND,
    SPEC,
    ConfluenceConfig,
    ConfluenceConnector,
    _PageBundle,
    _build_auth,
    _decode_cursor,
    _encode_cursor,
    _factory,
    _host_from_base_url,
    _is_archived,
    _parse_iso,
    _resolve_link,
    _serialise_bundle,
    _string_tuple,
)

from .conftest import json_response, make_handler


_CLOUD_BASE = "https://acme.atlassian.net/wiki"
_DC_BASE = "https://confluence.acme.internal"


def _bearer_credential(token: str = "ya29") -> Credential:
    return Credential(kind="confluence", payload={"access_token": token})


def _basic_credential(**kwargs: str) -> Credential:
    return Credential(kind="confluence", payload=dict(kwargs))


def _page_payload(
    page_id: str,
    *,
    title: str = "Page",
    body: str = "<p>hello</p>",
    when: str = "2026-05-04T00:00:00.000Z",
    space_key: str = "ENG",
    status: str | None = None,
    webui: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": page_id,
        "title": title,
        "version": {"when": when},
        "space": {"key": space_key},
        "body": {"storage": {"value": body}},
        "_links": {"webui": webui or f"/spaces/{space_key}/pages/{page_id}"},
    }
    if status is not None:
        payload["status"] = status
    return payload


def _comment_payload(body: str) -> dict[str, Any]:
    return {"body": {"storage": {"value": body}}}


def _attachment_payload(title: str, *, download: str = "/download/x.pdf") -> dict[str, Any]:
    return {"title": title, "_links": {"download": download}}


async def _drain(it: AsyncIterator[DocumentRef]) -> list[DocumentRef]:
    return [r async for r in it]


# ---------------------------------------------------------------------
# Config + construction
# ---------------------------------------------------------------------


class TestConfig:
    def test_resolved_id_default_strips_scheme_and_path(self) -> None:
        cfg = ConfluenceConfig(flavor="cloud", base_url=_CLOUD_BASE)
        assert cfg.resolved_id() == "confluence-cloud:acme.atlassian.net"

    def test_resolved_id_explicit_wins(self) -> None:
        cfg = ConfluenceConfig(
            flavor="cloud", base_url=_CLOUD_BASE, id="custom"
        )
        assert cfg.resolved_id() == "custom"

    def test_resolved_tenant_id_falls_back_to_resolved_id(self) -> None:
        cfg = ConfluenceConfig(flavor="cloud", base_url=_CLOUD_BASE)
        assert cfg.resolved_tenant_id() == cfg.resolved_id()

    def test_resolved_tenant_id_explicit_wins(self) -> None:
        cfg = ConfluenceConfig(
            flavor="cloud", base_url=_CLOUD_BASE, tenant_id="acme"
        )
        assert cfg.resolved_tenant_id() == "acme"

    def test_invalid_flavor_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be 'cloud' or 'datacenter'"):
            ConfluenceConfig(flavor="server", base_url=_CLOUD_BASE)  # type: ignore[arg-type]

    def test_missing_base_url_rejected(self) -> None:
        with pytest.raises(ValueError, match="base_url is required"):
            ConfluenceConfig(flavor="cloud", base_url="")

    def test_invalid_page_size_rejected(self) -> None:
        with pytest.raises(ValueError, match="page_size"):
            ConfluenceConfig(flavor="cloud", base_url=_CLOUD_BASE, page_size=0)
        with pytest.raises(ValueError, match="page_size"):
            ConfluenceConfig(
                flavor="cloud", base_url=_CLOUD_BASE, page_size=10_000
            )

    def test_invalid_timeout_rejected(self) -> None:
        with pytest.raises(ValueError, match="request_timeout"):
            ConfluenceConfig(
                flavor="cloud", base_url=_CLOUD_BASE, request_timeout=0
            )


class TestConstruction:
    def test_runtime_protocol_isinstance(self) -> None:
        c = ConfluenceConnector(
            ConfluenceConfig(flavor="cloud", base_url=_CLOUD_BASE),
            credential=_bearer_credential(),
        )
        assert isinstance(c, SourceConnector)
        assert c.kind == KIND

    def test_capabilities(self) -> None:
        c = ConfluenceConnector(
            ConfluenceConfig(flavor="cloud", base_url=_CLOUD_BASE),
            credential=_bearer_credential(),
        )
        caps = c.capabilities()
        assert caps == Capabilities(
            incremental=True,
            binary=False,
            content_hash_delta=False,
            max_concurrent_fetches=4,
            streaming=False,
        )

    def test_bucket_key(self) -> None:
        c = ConfluenceConnector(
            ConfluenceConfig(
                flavor="cloud", base_url=_CLOUD_BASE, tenant_id="acme"
            ),
            credential=_bearer_credential(),
        )
        assert c.bucket_key() == BucketKey(connector_kind="confluence", tenant_id="acme")

    def test_api_and_config_properties(self) -> None:
        # Both accessors are how the default-enumerate path reaches
        # the connector's HTTP client; cover them so refactors do not
        # silently break re-use.
        from pleno_pii_scanner_confluence.api import ConfluenceApi as _Api

        c = ConfluenceConnector(
            ConfluenceConfig(flavor="cloud", base_url=_CLOUD_BASE),
            credential=_bearer_credential(),
        )
        assert isinstance(c.api, _Api)
        assert c.config.flavor == "cloud"


# ---------------------------------------------------------------------
# Cloud
# ---------------------------------------------------------------------


class TestCloud:
    async def test_discover_yields_pages_with_auth_header(self) -> None:
        seen_auth: list[str] = []

        def page_handler(request: httpx.Request) -> httpx.Response:
            seen_auth.append(request.headers.get("authorization", ""))
            return json_response(
                {"results": [_page_payload("p1")], "_links": {}}
            )

        def comment_handler(_: httpx.Request) -> httpx.Response:
            return json_response({"results": [], "_links": {}})

        def attachment_handler(_: httpx.Request) -> httpx.Response:
            return json_response({"results": [], "_links": {}})

        transport = httpx.MockTransport(
            make_handler(
                [
                    ("/content/p1/child/comment", comment_handler),
                    ("/content/p1/child/attachment", attachment_handler),
                    ("/content/page", page_handler),
                ]
            )
        )
        c = ConfluenceConnector(
            ConfluenceConfig(
                flavor="cloud",
                base_url=_CLOUD_BASE,
                spaces=("ENG",),
            ),
            credential=_bearer_credential("ya29"),
            transport=transport,
        )
        try:
            refs = await _drain(c.discover(SourceFilter(), None))
            assert len(refs) == 1
            assert refs[0].source_kind == KIND
            assert refs[0].metadata["page_id"] == "p1"
            assert refs[0].metadata["space_key"] == "ENG"
            assert refs[0].metadata["flavor"] == "cloud"
            assert refs[0].native_url == f"{_CLOUD_BASE}/spaces/ENG/pages/p1"
            assert refs[0].last_modified is not None
            assert all(h == "Bearer ya29" for h in seen_auth)
        finally:
            await c.close()

    async def test_basic_email_apitoken_auth(self) -> None:
        seen_headers: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_headers.update(request.headers)
            return json_response({"results": [], "_links": {}})

        transport = httpx.MockTransport(handler)
        cred = Credential(
            kind="confluence",
            payload={"email": "alice@x.test", "api_token": "atl-secret"},
        )
        c = ConfluenceConnector(
            ConfluenceConfig(
                flavor="cloud", base_url=_CLOUD_BASE, spaces=("ENG",)
            ),
            credential=cred,
            transport=transport,
        )
        try:
            await _drain(c.discover(SourceFilter(), None))
            # The api_token must NEVER appear in clear in any captured
            # header — the connector encodes basic auth into the
            # `Authorization: Basic <b64>` header only.
            for v in seen_headers.values():
                assert "atl-secret" not in v
            assert seen_headers["authorization"].startswith("Basic ")
        finally:
            await c.close()

    async def test_discover_paginates_pages(self) -> None:
        # Two pages of /space/ENG/content/page; the paginator must
        # follow `_links.next`.
        page_responses = iter(
            [
                {
                    "results": [_page_payload("p1")],
                    "_links": {"next": "/rest/api/space/ENG/content/page?start=1"},
                },
                {"results": [_page_payload("p2")], "_links": {}},
            ]
        )

        def page_handler(_: httpx.Request) -> httpx.Response:
            return json_response(next(page_responses))

        def empty(_: httpx.Request) -> httpx.Response:
            return json_response({"results": [], "_links": {}})

        transport = httpx.MockTransport(
            make_handler(
                [
                    ("/child/comment", empty),
                    ("/child/attachment", empty),
                    ("/content/page", page_handler),
                ]
            )
        )
        c = ConfluenceConnector(
            ConfluenceConfig(
                flavor="cloud", base_url=_CLOUD_BASE, spaces=("ENG",)
            ),
            credential=_bearer_credential(),
            transport=transport,
        )
        try:
            refs = await _drain(c.discover(SourceFilter(), None))
            assert [r.metadata["page_id"] for r in refs] == ["p1", "p2"]
        finally:
            await c.close()

    async def test_discover_filters_by_cursor(self) -> None:
        # First page predates cursor → skipped; second is newer →
        # emitted.
        old = _page_payload("old", when="2026-01-01T00:00:00Z")
        new = _page_payload("new", when="2026-12-31T00:00:00Z")

        def page_handler(_: httpx.Request) -> httpx.Response:
            return json_response({"results": [old, new], "_links": {}})

        def empty(_: httpx.Request) -> httpx.Response:
            return json_response({"results": [], "_links": {}})

        transport = httpx.MockTransport(
            make_handler(
                [
                    ("/child/comment", empty),
                    ("/child/attachment", empty),
                    ("/content/page", page_handler),
                ]
            )
        )
        c = ConfluenceConnector(
            ConfluenceConfig(
                flavor="cloud", base_url=_CLOUD_BASE, spaces=("ENG",)
            ),
            credential=_bearer_credential(),
            transport=transport,
        )
        try:
            cursor = _encode_cursor(datetime(2026, 6, 1, tzinfo=UTC))
            refs = await _drain(c.discover(SourceFilter(), cursor))
            assert [r.metadata["page_id"] for r in refs] == ["new"]
        finally:
            await c.close()

    async def test_discover_skips_archived_by_default(self) -> None:
        kept = _page_payload("kept")
        gone = _page_payload("gone", status="archived")

        def page_handler(_: httpx.Request) -> httpx.Response:
            return json_response({"results": [kept, gone], "_links": {}})

        def empty(_: httpx.Request) -> httpx.Response:
            return json_response({"results": [], "_links": {}})

        transport = httpx.MockTransport(
            make_handler(
                [
                    ("/child/comment", empty),
                    ("/child/attachment", empty),
                    ("/content/page", page_handler),
                ]
            )
        )
        c = ConfluenceConnector(
            ConfluenceConfig(
                flavor="cloud", base_url=_CLOUD_BASE, spaces=("ENG",)
            ),
            credential=_bearer_credential(),
            transport=transport,
        )
        try:
            refs = await _drain(c.discover(SourceFilter(), None))
            assert [r.metadata["page_id"] for r in refs] == ["kept"]
        finally:
            await c.close()

    async def test_discover_skips_page_with_non_string_id(self) -> None:
        bad = {"id": 12345, "version": {}, "_links": {}}

        def page_handler(_: httpx.Request) -> httpx.Response:
            return json_response({"results": [bad], "_links": {}})

        transport = httpx.MockTransport(
            make_handler([("/content/page", page_handler)])
        )
        c = ConfluenceConnector(
            ConfluenceConfig(
                flavor="cloud", base_url=_CLOUD_BASE, spaces=("ENG",)
            ),
            credential=_bearer_credential(),
            transport=transport,
        )
        try:
            assert await _drain(c.discover(SourceFilter(), None)) == []
        finally:
            await c.close()

    async def test_discover_page_with_no_version_when_yields_no_cursor(
        self,
    ) -> None:
        # Page payload with `version` present but `version.when` missing:
        # the high-water update + cursor metadata branches must skip
        # silently rather than crashing on a None timestamp.
        page = _page_payload("p1")
        page["version"] = {}

        def page_handler(_: httpx.Request) -> httpx.Response:
            return json_response({"results": [page], "_links": {}})

        def empty(_: httpx.Request) -> httpx.Response:
            return json_response({"results": [], "_links": {}})

        transport = httpx.MockTransport(
            make_handler(
                [
                    ("/child/comment", empty),
                    ("/child/attachment", empty),
                    ("/content/page", page_handler),
                ]
            )
        )
        c = ConfluenceConnector(
            ConfluenceConfig(
                flavor="cloud", base_url=_CLOUD_BASE, spaces=("ENG",)
            ),
            credential=_bearer_credential(),
            transport=transport,
        )
        try:
            refs = await _drain(c.discover(SourceFilter(), None))
            assert len(refs) == 1
            assert refs[0].last_modified is None
            assert "_cursor" not in refs[0].metadata
            # No high-water seen → no cursor to checkpoint either.
            assert c.cursor_after_run() is None
        finally:
            await c.close()

    async def test_fetch_returns_nothing_when_bundle_text_empty(self) -> None:
        # Manually inject an empty bundle into the page cache so the
        # `text` falsy branch of fetch() runs (the production path
        # always serialises at least the title, so we have to coax it).
        c = ConfluenceConnector(
            ConfluenceConfig(flavor="cloud", base_url=_CLOUD_BASE),
            credential=_bearer_credential(),
            transport=httpx.MockTransport(make_handler([])),
        )
        try:
            from pleno_pii_scanner_confluence.connector import _PageBundle as PB

            empty_bundle = PB(
                page_id="p1",
                space_key="",
                title="",
                body_storage="",
                version_when=None,
                comments=(),
                attachments=(),
            )
            # Patch `_serialise_bundle` to return empty so we hit the
            # `if not text: return` guard inside fetch().
            from pleno_pii_scanner_confluence import connector as conn_mod

            original = conn_mod._serialise_bundle
            conn_mod._serialise_bundle = lambda _bundle: ""  # type: ignore[assignment]
            try:
                c._page_cache["p1"] = empty_bundle
                ref = DocumentRef(
                    source_id=c.id,
                    source_kind=c.kind,
                    path="confluence://x/p1",
                    metadata={"page_id": "p1"},
                )
                assert [d async for d in c.fetch(ref)] == []
            finally:
                conn_mod._serialise_bundle = original  # type: ignore[assignment]
        finally:
            await c.close()

    async def test_fetch_emits_document_with_body_comment_attachment(self) -> None:
        def page_handler(_: httpx.Request) -> httpx.Response:
            return json_response(
                {
                    "results": [
                        _page_payload(
                            "p1",
                            title="Runbook",
                            body=(
                                '<ac:structured-macro ac:name="info">'
                                "<ac:rich-text-body><p>contact alice@x.test</p>"
                                "</ac:rich-text-body></ac:structured-macro>"
                            ),
                        )
                    ],
                    "_links": {},
                }
            )

        def comment_handler(_: httpx.Request) -> httpx.Response:
            return json_response(
                {
                    "results": [_comment_payload("<p>see also bob@x.test</p>")],
                    "_links": {},
                }
            )

        def attachment_handler(_: httpx.Request) -> httpx.Response:
            return json_response(
                {
                    "results": [_attachment_payload("creds.pdf")],
                    "_links": {},
                }
            )

        transport = httpx.MockTransport(
            make_handler(
                [
                    ("/content/p1/child/comment", comment_handler),
                    ("/content/p1/child/attachment", attachment_handler),
                    ("/content/page", page_handler),
                ]
            )
        )
        c = ConfluenceConnector(
            ConfluenceConfig(
                flavor="cloud", base_url=_CLOUD_BASE, spaces=("ENG",)
            ),
            credential=_bearer_credential(),
            transport=transport,
        )
        try:
            refs = await _drain(c.discover(SourceFilter(), None))
            assert len(refs) == 1
            docs = [d async for d in c.fetch(refs[0])]
            assert len(docs) == 1
            assert isinstance(docs[0], Document)
            text = docs[0].text or ""
            assert "title=Runbook" in text
            assert "space=ENG" in text
            assert "version=" in text
            assert "contact alice@x.test" in text
            assert "comment=see also bob@x.test" in text
            assert "attachment=creds.pdf" in text
            assert f"url={_CLOUD_BASE}/download/x.pdf" in text
        finally:
            await c.close()

    async def test_fetch_unknown_page_yields_nothing(self) -> None:
        c = ConfluenceConnector(
            ConfluenceConfig(flavor="cloud", base_url=_CLOUD_BASE),
            credential=_bearer_credential(),
            transport=httpx.MockTransport(make_handler([])),
        )
        try:
            ghost = DocumentRef(
                source_id=c.id,
                source_kind=c.kind,
                path="confluence://ENG/missing",
                metadata={"page_id": "missing"},
            )
            assert [d async for d in c.fetch(ghost)] == []
        finally:
            await c.close()

    async def test_fetch_missing_page_id_metadata_yields_nothing(self) -> None:
        c = ConfluenceConnector(
            ConfluenceConfig(flavor="cloud", base_url=_CLOUD_BASE),
            credential=_bearer_credential(),
            transport=httpx.MockTransport(make_handler([])),
        )
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind=c.kind,
                path="confluence://ENG/x",
                metadata={},
            )
            assert [d async for d in c.fetch(ref)] == []
        finally:
            await c.close()

    async def test_429_during_discover_surfaces_rate_limited(self) -> None:
        async def fake_sleep(_: float) -> None:
            return None

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"Retry-After": "1"})

        transport = httpx.MockTransport(handler)
        c = ConfluenceConnector(
            ConfluenceConfig(
                flavor="cloud", base_url=_CLOUD_BASE, spaces=("ENG",)
            ),
            credential=_bearer_credential(),
            transport=transport,
            sleep=fake_sleep,
        )
        try:
            with pytest.raises(RateLimited):
                await _drain(c.discover(SourceFilter(), None))
        finally:
            await c.close()

    async def test_space_enumeration_when_no_allowlist(self) -> None:
        # No `spaces` config → connector calls `/space` to list all
        # visible keys.
        def space_handler(_: httpx.Request) -> httpx.Response:
            return json_response(
                {"results": [{"key": "ENG"}, {"key": "SEC"}], "_links": {}}
            )

        def page_handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/space/ENG/" in url:
                return json_response(
                    {"results": [_page_payload("p1", space_key="ENG")], "_links": {}}
                )
            return json_response(
                {"results": [_page_payload("p2", space_key="SEC")], "_links": {}}
            )

        def empty(_: httpx.Request) -> httpx.Response:
            return json_response({"results": [], "_links": {}})

        transport = httpx.MockTransport(
            make_handler(
                [
                    ("/child/comment", empty),
                    ("/child/attachment", empty),
                    ("/content/page", page_handler),
                    ("/rest/api/space?", space_handler),
                    ("/rest/api/space&", space_handler),
                    # Cover the bare `?limit=...` URL httpx renders.
                    ("/rest/api/space", space_handler),
                ]
            )
        )
        c = ConfluenceConnector(
            ConfluenceConfig(flavor="cloud", base_url=_CLOUD_BASE),
            credential=_bearer_credential(),
            transport=transport,
        )
        try:
            refs = await _drain(c.discover(SourceFilter(), None))
            ids = sorted(r.metadata["page_id"] for r in refs)
            assert ids == ["p1", "p2"]
        finally:
            await c.close()

    async def test_space_enumeration_skips_entries_without_string_key(self) -> None:
        def space_handler(_: httpx.Request) -> httpx.Response:
            return json_response(
                {
                    "results": [
                        {"key": "ENG"},
                        {"key": 123},
                        {},
                    ],
                    "_links": {},
                }
            )

        def empty(_: httpx.Request) -> httpx.Response:
            return json_response({"results": [], "_links": {}})

        transport = httpx.MockTransport(
            make_handler(
                [
                    ("/child/comment", empty),
                    ("/child/attachment", empty),
                    ("/content/page", empty),
                    ("/rest/api/space", space_handler),
                ]
            )
        )
        c = ConfluenceConnector(
            ConfluenceConfig(flavor="cloud", base_url=_CLOUD_BASE),
            credential=_bearer_credential(),
            transport=transport,
        )
        try:
            # No pages emitted because the `/content/page` mock returns
            # empty for every space; we only assert the space-walk did
            # not crash on the malformed entries.
            assert await _drain(c.discover(SourceFilter(), None)) == []
        finally:
            await c.close()


# ---------------------------------------------------------------------
# Datacenter
# ---------------------------------------------------------------------


class TestDatacenter:
    async def test_pat_bearer_auth(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("authorization", ""))
            return json_response({"results": [], "_links": {}})

        transport = httpx.MockTransport(handler)
        c = ConfluenceConnector(
            ConfluenceConfig(
                flavor="datacenter", base_url=_DC_BASE, spaces=("ENG",)
            ),
            credential=_bearer_credential("dc-pat"),
            transport=transport,
        )
        try:
            await _drain(c.discover(SourceFilter(), None))
            assert all(h == "Bearer dc-pat" for h in seen)
        finally:
            await c.close()

    async def test_basic_username_password_auth(self) -> None:
        seen_headers: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_headers.update(request.headers)
            return json_response({"results": [], "_links": {}})

        transport = httpx.MockTransport(handler)
        cred = Credential(
            kind="confluence",
            payload={"username": "alice", "password": "dc-secret"},
        )
        c = ConfluenceConnector(
            ConfluenceConfig(
                flavor="datacenter", base_url=_DC_BASE, spaces=("ENG",)
            ),
            credential=cred,
            transport=transport,
        )
        try:
            await _drain(c.discover(SourceFilter(), None))
            assert seen_headers["authorization"].startswith("Basic ")
            for v in seen_headers.values():
                assert "dc-secret" not in v
        finally:
            await c.close()

    async def test_503_during_discover_retries_then_surfaces_rate_limited(
        self,
    ) -> None:
        async def fake_sleep(_: float) -> None:
            return None

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(503, headers={"Retry-After": "1"})

        transport = httpx.MockTransport(handler)
        c = ConfluenceConnector(
            ConfluenceConfig(
                flavor="datacenter", base_url=_DC_BASE, spaces=("ENG",)
            ),
            credential=_bearer_credential(),
            transport=transport,
            sleep=fake_sleep,
        )
        try:
            with pytest.raises(RateLimited):
                await _drain(c.discover(SourceFilter(), None))
        finally:
            await c.close()

    async def test_discover_paginates_with_start_limit(self) -> None:
        page_responses = iter(
            [
                {
                    "results": [_page_payload("p1")],
                    # DC's classic `_links.next` carries a relative
                    # path with `start=` query.
                    "_links": {
                        "next": "/rest/api/space/ENG/content/page?start=1&limit=100"
                    },
                },
                {"results": [_page_payload("p2")], "_links": {}},
            ]
        )
        seen_urls: list[str] = []

        def page_handler(request: httpx.Request) -> httpx.Response:
            seen_urls.append(str(request.url))
            return json_response(next(page_responses))

        def empty(_: httpx.Request) -> httpx.Response:
            return json_response({"results": [], "_links": {}})

        transport = httpx.MockTransport(
            make_handler(
                [
                    ("/child/comment", empty),
                    ("/child/attachment", empty),
                    ("/content/page", page_handler),
                ]
            )
        )
        c = ConfluenceConnector(
            ConfluenceConfig(
                flavor="datacenter", base_url=_DC_BASE, spaces=("ENG",)
            ),
            credential=_bearer_credential(),
            transport=transport,
        )
        try:
            refs = await _drain(c.discover(SourceFilter(), None))
            assert [r.metadata["page_id"] for r in refs] == ["p1", "p2"]
            # Second request honored the `start=1` carry-over.
            assert any("start=1" in u for u in seen_urls)
        finally:
            await c.close()

    async def test_fetch_renders_storage_text_and_attachments(self) -> None:
        def page_handler(_: httpx.Request) -> httpx.Response:
            return json_response(
                {
                    "results": [
                        _page_payload(
                            "p1",
                            body=(
                                "<h1>Title</h1>"
                                "<table><tr><td>SSN</td><td>123-45-6789</td></tr></table>"
                            ),
                        )
                    ],
                    "_links": {},
                }
            )

        def comment_handler(_: httpx.Request) -> httpx.Response:
            return json_response({"results": [], "_links": {}})

        def attachment_handler(_: httpx.Request) -> httpx.Response:
            return json_response(
                {
                    "results": [
                        _attachment_payload("notes.txt"),
                        # Defensive: missing both download + webui.
                        {"title": "no-href"},
                        # Defensive: title is None.
                        {"_links": {"download": "/x"}},
                    ],
                    "_links": {},
                }
            )

        transport = httpx.MockTransport(
            make_handler(
                [
                    ("/content/p1/child/comment", comment_handler),
                    ("/content/p1/child/attachment", attachment_handler),
                    ("/content/page", page_handler),
                ]
            )
        )
        c = ConfluenceConnector(
            ConfluenceConfig(
                flavor="datacenter", base_url=_DC_BASE, spaces=("ENG",)
            ),
            credential=_bearer_credential(),
            transport=transport,
        )
        try:
            refs = await _drain(c.discover(SourceFilter(), None))
            docs = [d async for d in c.fetch(refs[0])]
            text = docs[0].text or ""
            assert "Title" in text
            assert "SSN" in text
            assert "123-45-6789" in text
            assert "attachment=notes.txt" in text
            # Malformed attachment entries dropped.
            assert "no-href" not in text
        finally:
            await c.close()

    async def test_include_archived_keeps_them(self) -> None:
        def page_handler(_: httpx.Request) -> httpx.Response:
            return json_response(
                {
                    "results": [_page_payload("p1", status="archived")],
                    "_links": {},
                }
            )

        def empty(_: httpx.Request) -> httpx.Response:
            return json_response({"results": [], "_links": {}})

        transport = httpx.MockTransport(
            make_handler(
                [
                    ("/child/comment", empty),
                    ("/child/attachment", empty),
                    ("/content/page", page_handler),
                ]
            )
        )
        c = ConfluenceConnector(
            ConfluenceConfig(
                flavor="datacenter",
                base_url=_DC_BASE,
                spaces=("ENG",),
                include_archived=True,
            ),
            credential=_bearer_credential(),
            transport=transport,
        )
        try:
            refs = await _drain(c.discover(SourceFilter(), None))
            assert [r.metadata["page_id"] for r in refs] == ["p1"]
        finally:
            await c.close()


# ---------------------------------------------------------------------
# Cursor round-trip
# ---------------------------------------------------------------------


class TestCursor:
    def test_encode_decode_round_trip(self) -> None:
        when = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)
        encoded = _encode_cursor(when)
        decoded = _decode_cursor(encoded)
        assert decoded == when

    def test_decode_handles_none(self) -> None:
        assert _decode_cursor(None) is None
        assert _decode_cursor("") is None

    def test_decode_handles_garbage(self) -> None:
        # Malformed cursor is silently treated as "fresh scan".
        assert _decode_cursor("not-json") is None
        assert _decode_cursor("[1, 2, 3]") is None
        assert _decode_cursor('{"high_water": "not-a-date"}') is None
        assert _decode_cursor('{"high_water": 42}') is None
        assert _decode_cursor('{"other": "x"}') is None

    async def test_cursor_after_run_returns_high_water(self) -> None:
        when = "2026-12-31T00:00:00Z"

        def page_handler(_: httpx.Request) -> httpx.Response:
            return json_response(
                {"results": [_page_payload("p1", when=when)], "_links": {}}
            )

        def empty(_: httpx.Request) -> httpx.Response:
            return json_response({"results": [], "_links": {}})

        transport = httpx.MockTransport(
            make_handler(
                [
                    ("/child/comment", empty),
                    ("/child/attachment", empty),
                    ("/content/page", page_handler),
                ]
            )
        )
        c = ConfluenceConnector(
            ConfluenceConfig(
                flavor="cloud", base_url=_CLOUD_BASE, spaces=("ENG",)
            ),
            credential=_bearer_credential(),
            transport=transport,
        )
        try:
            await _drain(c.discover(SourceFilter(), None))
            cur = c.cursor_after_run()
            assert isinstance(cur, str)
            decoded = _decode_cursor(cur)
            assert decoded == datetime(2026, 12, 31, tzinfo=UTC)
        finally:
            await c.close()

    async def test_cursor_after_run_none_when_nothing_seen(self) -> None:
        c = ConfluenceConnector(
            ConfluenceConfig(flavor="cloud", base_url=_CLOUD_BASE),
            credential=_bearer_credential(),
        )
        try:
            assert c.cursor_after_run() is None
        finally:
            await c.close()


# ---------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------


class TestAuthHelper:
    def test_bearer_preferred_for_cloud(self) -> None:
        cred = Credential(
            kind="confluence",
            payload={"access_token": "bearer", "email": "x", "api_token": "y"},
        )
        from pleno_pii_scanner_confluence.api import BearerAuth

        auth = _build_auth("cloud", cred)
        assert isinstance(auth, BearerAuth)

    def test_basic_email_apitoken_for_cloud(self) -> None:
        from pleno_pii_scanner_confluence.api import BasicAuth

        cred = Credential(
            kind="confluence",
            payload={"email": "alice@x.test", "api_token": "atl"},
        )
        auth = _build_auth("cloud", cred)
        assert isinstance(auth, BasicAuth)
        assert auth.username == "alice@x.test"
        assert auth.password == "atl"

    def test_basic_username_password_for_cloud(self) -> None:
        # Cloud also accepts the legacy `username` + `password` shape
        # for installs that are migrating off basic auth.
        from pleno_pii_scanner_confluence.api import BasicAuth

        cred = Credential(
            kind="confluence",
            payload={"username": "alice", "password": "p"},
        )
        auth = _build_auth("cloud", cred)
        assert isinstance(auth, BasicAuth)

    def test_bearer_for_dc_pat(self) -> None:
        from pleno_pii_scanner_confluence.api import BearerAuth

        cred = Credential(kind="confluence", payload={"token": "pat"})
        auth = _build_auth("datacenter", cred)
        assert isinstance(auth, BearerAuth)
        assert auth.token == "pat"

    def test_basic_for_dc(self) -> None:
        from pleno_pii_scanner_confluence.api import BasicAuth

        cred = Credential(
            kind="confluence",
            payload={"username": "alice", "password": "p"},
        )
        auth = _build_auth("datacenter", cred)
        assert isinstance(auth, BasicAuth)

    def test_missing_credentials_raises(self) -> None:
        cred = Credential(kind="confluence", payload={})
        with pytest.raises(ValueError, match="credential.payload requires"):
            _build_auth("cloud", cred)
        with pytest.raises(ValueError, match="credential.payload requires"):
            _build_auth("datacenter", cred)

    def test_partial_credentials_rejected(self) -> None:
        # Username present but no password — both must be present.
        cred = Credential(kind="confluence", payload={"username": "alice"})
        with pytest.raises(ValueError):
            _build_auth("cloud", cred)


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


class TestHelpers:
    def test_parse_iso_handles_z(self) -> None:
        ts = _parse_iso("2026-05-04T00:00:00Z")
        assert ts is not None and ts.year == 2026

    def test_parse_iso_returns_none_on_garbage(self) -> None:
        assert _parse_iso("not-a-date") is None
        assert _parse_iso(None) is None
        assert _parse_iso(42) is None
        assert _parse_iso("") is None

    def test_is_archived_true_for_known_states(self) -> None:
        assert _is_archived({"status": "archived"}) is True
        assert _is_archived({"status": "trashed"}) is True

    def test_is_archived_false_otherwise(self) -> None:
        assert _is_archived({}) is False
        assert _is_archived({"status": "current"}) is False
        assert _is_archived({"status": 42}) is False  # type: ignore[dict-item]

    def test_host_from_base_url_strips_scheme_and_path(self) -> None:
        assert _host_from_base_url("https://acme.atlassian.net/wiki") == "acme.atlassian.net"
        assert _host_from_base_url("http://host/") == "host"
        assert _host_from_base_url("https://host/sub/path") == "host"

    def test_resolve_link_absolute_passthrough(self) -> None:
        cfg = ConfluenceConfig(flavor="cloud", base_url=_CLOUD_BASE)
        assert _resolve_link(cfg, "https://other/a") == "https://other/a"

    def test_resolve_link_relative_prefixes_base(self) -> None:
        cfg = ConfluenceConfig(flavor="cloud", base_url=_CLOUD_BASE)
        assert _resolve_link(cfg, "/download/x") == f"{_CLOUD_BASE}/download/x"
        assert _resolve_link(cfg, "download/x") == f"{_CLOUD_BASE}/download/x"

    def test_browse_url_returns_none_when_webui_missing(self) -> None:
        # `_browse_url` short-circuits when `_links.webui` is missing /
        # not a string. Cover both branches.
        from pleno_pii_scanner_confluence.connector import _browse_url

        cfg = ConfluenceConfig(flavor="cloud", base_url=_CLOUD_BASE)
        assert _browse_url(cfg, {}) is None
        assert _browse_url(cfg, {"_links": {}}) is None
        assert _browse_url(cfg, {"_links": {"webui": ""}}) is None
        assert _browse_url(cfg, {"_links": {"webui": 42}}) is None  # type: ignore[dict-item]
        assert _browse_url(cfg, {"_links": {"webui": "/x"}}) == f"{_CLOUD_BASE}/x"

    def test_string_tuple_accepts_lists(self) -> None:
        assert _string_tuple(["a", "b"]) == ("a", "b")
        assert _string_tuple(("a",)) == ("a",)
        assert _string_tuple(None) == ()

    def test_string_tuple_rejects_bare_string(self) -> None:
        with pytest.raises(ValueError, match="bare string"):
            _string_tuple("ENG")

    def test_string_tuple_rejects_non_iterable(self) -> None:
        with pytest.raises(ValueError, match="iterable"):
            _string_tuple(42)

    def test_string_tuple_rejects_empty_or_non_string_items(self) -> None:
        with pytest.raises(ValueError, match="non-empty strings"):
            _string_tuple(["", "ok"])
        with pytest.raises(ValueError, match="non-empty strings"):
            _string_tuple([123])

    def test_serialise_bundle_minimal(self) -> None:
        bundle = _PageBundle(
            page_id="p1",
            space_key="ENG",
            title="t",
            body_storage="",
            version_when=None,
        )
        out = _serialise_bundle(bundle)
        assert "title=t" in out
        assert "space=ENG" in out
        # No version line when version_when is None.
        assert "version=" not in out

    def test_serialise_bundle_skips_empty_comment(self) -> None:
        bundle = _PageBundle(
            page_id="p1",
            space_key="ENG",
            title="t",
            body_storage="",
            version_when=None,
            comments=("", "real"),
        )
        out = _serialise_bundle(bundle)
        # Empty-string comment must NOT emit a `comment=` line.
        assert out.count("comment=") == 1
        assert "comment=real" in out


# ---------------------------------------------------------------------
# Factory + Spec
# ---------------------------------------------------------------------


class TestFactoryAndSpec:
    def test_spec_metadata(self) -> None:
        assert SPEC.kind == KIND == "confluence"
        assert SPEC.capabilities.incremental is True
        assert SPEC.capabilities.max_concurrent_fetches == 4
        assert any("read:confluence" in s for s in SPEC.required_scopes)

    def test_factory_minimal_cloud(self) -> None:
        cred = _bearer_credential()
        c = SPEC.factory({"flavor": "cloud", "base_url": _CLOUD_BASE, "_credential": cred})
        assert isinstance(c, ConfluenceConnector)
        assert c.config.flavor == "cloud"

    def test_factory_full_config(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `ca_bundle_path` resolution loads the file in the api
        # constructor via `ssl.create_default_context(cafile=...)`. We
        # stub that out with a no-op so the test does not need a real
        # PEM (which would otherwise require either checked-in fixture
        # data or a `cryptography` test-only dependency).
        import ssl as _ssl

        from pleno_pii_scanner_confluence import api as api_mod

        original = api_mod.ssl.create_default_context
        monkeypatch.setattr(
            api_mod.ssl,
            "create_default_context",
            lambda **kwargs: original(),
        )
        ca = tmp_path / "ca.pem"
        ca.write_text("")
        cred = _bearer_credential()
        c = _factory(
            {
                "_credential": cred,
                "flavor": "datacenter",
                "base_url": _DC_BASE,
                "spaces": ["ENG"],
                "include_archived": True,
                "page_size": 50,
                "request_timeout": 10.0,
                "ca_bundle_path": str(ca),
                "id": "custom",
                "tenant_id": "tenant-1",
            }
        )
        assert c.id == "custom"
        assert c.config.spaces == ("ENG",)
        assert c.config.include_archived is True
        assert c.config.page_size == 50
        assert c.config.ca_bundle_path == str(ca)
        assert c.config.tenant_id == "tenant-1"

    def test_factory_rejects_invalid_flavor(self) -> None:
        cred = _bearer_credential()
        with pytest.raises(ValueError, match="must be 'cloud' or 'datacenter'"):
            _factory({"_credential": cred, "flavor": "server", "base_url": _CLOUD_BASE})

    def test_factory_rejects_missing_credential(self) -> None:
        with pytest.raises(ValueError, match="resolved Credential"):
            _factory({"flavor": "cloud", "base_url": _CLOUD_BASE})

    def test_factory_rejects_missing_base_url(self) -> None:
        cred = _bearer_credential()
        with pytest.raises(ValueError, match="base_url"):
            _factory({"_credential": cred, "flavor": "cloud"})

    def test_factory_rejects_bare_string_spaces(self) -> None:
        cred = _bearer_credential()
        with pytest.raises(ValueError, match="bare string"):
            _factory(
                {
                    "_credential": cred,
                    "flavor": "cloud",
                    "base_url": _CLOUD_BASE,
                    "spaces": "ENG",
                }
            )


# ---------------------------------------------------------------------
# Package init
# ---------------------------------------------------------------------


class TestPackageInit:
    def test_top_level_exports(self) -> None:
        import pleno_pii_scanner_confluence as pkg

        assert pkg.SPEC is SPEC
        assert pkg.KIND == "confluence"
        assert pkg.ConfluenceConnector is ConfluenceConnector
        assert pkg.ConfluenceConfig is ConfluenceConfig
        assert callable(pkg.storage_to_text)
        assert pkg.__version__ == "0.1.0"


# ---------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------


class TestLifecycle:
    async def test_close_clears_cache(self) -> None:
        def page_handler(_: httpx.Request) -> httpx.Response:
            return json_response(
                {"results": [_page_payload("p1")], "_links": {}}
            )

        def empty(_: httpx.Request) -> httpx.Response:
            return json_response({"results": [], "_links": {}})

        transport = httpx.MockTransport(
            make_handler(
                [
                    ("/child/comment", empty),
                    ("/child/attachment", empty),
                    ("/content/page", page_handler),
                ]
            )
        )
        c = ConfluenceConnector(
            ConfluenceConfig(
                flavor="cloud", base_url=_CLOUD_BASE, spaces=("ENG",)
            ),
            credential=_bearer_credential(),
            transport=transport,
        )
        await _drain(c.discover(SourceFilter(), None))
        assert c._page_cache  # populated by discover
        await c.close()
        assert c._page_cache == {}

    async def test_close_can_be_called_twice(self) -> None:
        c = ConfluenceConnector(
            ConfluenceConfig(flavor="cloud", base_url=_CLOUD_BASE),
            credential=_bearer_credential(),
        )
        await c.close()
        try:
            await c.close()
        except RuntimeError:
            # httpx may raise on use-after-close; we only care that
            # the first close did not leak resources.
            pass


# ---------------------------------------------------------------------
# Token leak audit
# ---------------------------------------------------------------------


class TestNoLeakedTokens:
    async def test_credentials_never_appear_in_request_urls_or_bodies(
        self,
    ) -> None:
        seen: list[tuple[str, bytes]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((str(request.url), request.content))
            return json_response({"results": [], "_links": {}})

        transport = httpx.MockTransport(handler)
        secret_token = "very-secret-token-xyz"
        secret_password = "very-secret-pwd-xyz"
        # Cover both auth shapes by switching connectors mid-test.
        for cred in (
            Credential(
                kind="confluence", payload={"access_token": secret_token}
            ),
            Credential(
                kind="confluence",
                payload={"email": "a@x.test", "api_token": secret_password},
            ),
        ):
            c = ConfluenceConnector(
                ConfluenceConfig(
                    flavor="cloud", base_url=_CLOUD_BASE, spaces=("ENG",)
                ),
                credential=cred,
                transport=transport,
            )
            try:
                await _drain(c.discover(SourceFilter(), None))
            finally:
                await c.close()
        for url, body in seen:
            assert secret_token not in url
            assert secret_password not in url
            assert secret_token.encode() not in body
            assert secret_password.encode() not in body
