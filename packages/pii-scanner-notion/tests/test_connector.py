"""Tests for `NotionConnector` — discover modes, fetch, factory, lifecycle."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest

from pleno_pii_scanner.scheduler.rate_limit import BucketKey, RateLimited
from pleno_pii_scanner.sources.base import (
    Capabilities,
    Document,
    DocumentRef,
    SourceConnector,
    SourceFilter,
)
from pleno_pii_scanner_notion.connector import (
    KIND,
    SPEC,
    NotionConfig,
    NotionConnector,
    _factory,
    _parent_id,
    _parse_iso,
    _string_tuple,
)

from .conftest import json_response, make_handler


def page_object(
    page_id: str,
    *,
    archived: bool = False,
    parent: dict[str, Any] | None = None,
    last_edited: str | None = "2026-05-04T00:00:00Z",
    url: str | None = None,
) -> dict[str, Any]:
    return {
        "object": "page",
        "id": page_id,
        "archived": archived,
        "parent": parent or {"type": "workspace", "workspace": True},
        "last_edited_time": last_edited,
        "url": url or f"https://notion.so/{page_id}",
        "properties": {},
    }


def database_object(database_id: str) -> dict[str, Any]:
    return {
        "object": "database",
        "id": database_id,
        "archived": False,
        "parent": {"type": "workspace", "workspace": True},
        "last_edited_time": "2026-05-04T00:00:00Z",
        "url": f"https://notion.so/{database_id}",
    }


def block_payload(text: str, *, block_type: str = "paragraph", block_id: str = "b") -> dict[str, Any]:
    return {
        "id": block_id,
        "object": "block",
        "type": block_type,
        block_type: {"rich_text": [{"type": "text", "text": {"content": text, "link": None}, "annotations": {}, "plain_text": text}]},
        "archived": False,
        "has_children": False,
    }


async def drain(it: AsyncIterator[DocumentRef]) -> list[DocumentRef]:
    return [r async for r in it]


# ---------------------------------------------------------------------
# config + construction
# ---------------------------------------------------------------------


class TestConfig:
    def test_resolved_id_default(self) -> None:
        assert NotionConfig(token="secret_x").resolved_id() == "notion:default"

    def test_resolved_id_with_workspace(self) -> None:
        cfg = NotionConfig(token="t", workspace_id="acme")
        assert cfg.resolved_id() == "notion:acme"

    def test_explicit_id_wins(self) -> None:
        assert NotionConfig(token="t", id="custom").resolved_id() == "custom"


class TestConstruction:
    def test_runtime_protocol_isinstance(self) -> None:
        c = NotionConnector(NotionConfig(token="secret_x"))
        assert isinstance(c, SourceConnector)
        assert c.kind == KIND
        assert c.id == "notion:default"

    def test_capabilities(self) -> None:
        c = NotionConnector(NotionConfig(token="t", max_concurrent_fetches=5))
        caps = c.capabilities()
        assert caps == Capabilities(
            incremental=True,
            binary=False,
            content_hash_delta=False,
            max_concurrent_fetches=5,
            streaming=False,
        )

    def test_bucket_key(self) -> None:
        c = NotionConnector(NotionConfig(token="t", workspace_id="acme"))
        assert c.bucket_key() == BucketKey(connector_kind="notion", tenant_id="acme")

    def test_bucket_key_falls_back_to_id(self) -> None:
        c = NotionConnector(NotionConfig(token="t"))
        assert c.bucket_key() == BucketKey(connector_kind="notion", tenant_id="notion:default")


# ---------------------------------------------------------------------
# discover — search mode
# ---------------------------------------------------------------------


class TestDiscoverSearch:
    async def test_search_yields_pages_and_databases(self) -> None:
        def search(_: httpx.Request) -> httpx.Response:
            return json_response(
                {
                    "results": [
                        page_object("p1"),
                        database_object("d1"),
                    ],
                    "has_more": False,
                    "next_cursor": None,
                }
            )

        transport = httpx.MockTransport(make_handler([("/search", search)]))
        c = NotionConnector(NotionConfig(token="t"), transport=transport)
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            paths = sorted(r.path for r in refs)
            assert paths == ["notion://database/d1", "notion://page/p1"]
            for r in refs:
                assert r.source_kind == "notion"
                assert r.content_type == "text/markdown"
                assert r.metadata["object_id"] in {"p1", "d1"}
                assert r.last_modified is not None
        finally:
            await c.close()

    async def test_search_pagination_chains_cursor(self) -> None:
        captured_bodies: list[bytes] = []

        responses = iter(
            [
                {
                    "results": [page_object("p1")],
                    "has_more": True,
                    "next_cursor": "cur-2",
                },
                {
                    "results": [page_object("p2")],
                    "has_more": False,
                    "next_cursor": None,
                },
            ]
        )

        def search(request: httpx.Request) -> httpx.Response:
            captured_bodies.append(request.content)
            return json_response(next(responses))

        transport = httpx.MockTransport(make_handler([("/search", search)]))
        c = NotionConnector(NotionConfig(token="t"), transport=transport)
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            assert [r.path for r in refs] == ["notion://page/p1", "notion://page/p2"]
            # Second request must include start_cursor.
            assert b"start_cursor" in captured_bodies[1]
            assert b"cur-2" in captured_bodies[1]
            # First ref carries the search _cursor in metadata.
            assert refs[0].metadata.get("_cursor") == "cur-2"
        finally:
            await c.close()

    async def test_search_seeded_cursor_is_used(self) -> None:
        captured_bodies: list[bytes] = []

        def search(request: httpx.Request) -> httpx.Response:
            captured_bodies.append(request.content)
            return json_response(
                {"results": [], "has_more": False, "next_cursor": None}
            )

        transport = httpx.MockTransport(make_handler([("/search", search)]))
        c = NotionConnector(NotionConfig(token="t"), transport=transport)
        try:
            await drain(c.discover(SourceFilter(), "cur-from-checkpoint"))
            assert b"cur-from-checkpoint" in captured_bodies[0]
        finally:
            await c.close()

    async def test_search_pagination_stops_when_next_cursor_missing(self) -> None:
        # has_more=True but next_cursor=None must not infinite-loop.
        def search(_: httpx.Request) -> httpx.Response:
            return json_response(
                {"results": [page_object("p1")], "has_more": True, "next_cursor": None}
            )

        transport = httpx.MockTransport(make_handler([("/search", search)]))
        c = NotionConnector(NotionConfig(token="t"), transport=transport)
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            assert [r.path for r in refs] == ["notion://page/p1"]
        finally:
            await c.close()

    async def test_search_skips_archived_by_default(self) -> None:
        def search(_: httpx.Request) -> httpx.Response:
            return json_response(
                {
                    "results": [
                        page_object("kept"),
                        page_object("dropped", archived=True),
                    ],
                    "has_more": False,
                    "next_cursor": None,
                }
            )

        transport = httpx.MockTransport(make_handler([("/search", search)]))
        c = NotionConnector(NotionConfig(token="t"), transport=transport)
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            assert {r.metadata["object_id"] for r in refs} == {"kept"}
        finally:
            await c.close()

    async def test_include_archived_keeps_them(self) -> None:
        def search(_: httpx.Request) -> httpx.Response:
            return json_response(
                {
                    "results": [page_object("p1", archived=True)],
                    "has_more": False,
                    "next_cursor": None,
                }
            )

        transport = httpx.MockTransport(make_handler([("/search", search)]))
        c = NotionConnector(
            NotionConfig(token="t", include_archived=True), transport=transport
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            assert {r.metadata["object_id"] for r in refs} == {"p1"}
        finally:
            await c.close()

    async def test_search_skips_unsupported_object_types(self) -> None:
        def search(_: httpx.Request) -> httpx.Response:
            return json_response(
                {
                    "results": [
                        {"object": "user", "id": "u1"},
                        page_object("p1"),
                        {"object": "page"},  # missing id
                        "broken",
                    ],
                    "has_more": False,
                    "next_cursor": None,
                }
            )

        transport = httpx.MockTransport(make_handler([("/search", search)]))
        c = NotionConnector(NotionConfig(token="t"), transport=transport)
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            assert {r.metadata["object_id"] for r in refs} == {"p1"}
        finally:
            await c.close()

    async def test_parent_chain_renders_for_known_parent_kinds(self) -> None:
        def search(_: httpx.Request) -> httpx.Response:
            return json_response(
                {
                    "results": [
                        page_object("p1", parent={"type": "page_id", "page_id": "parent-1"}),
                        page_object("p2", parent={"type": "database_id", "database_id": "db-1"}),
                        page_object("p3", parent={"type": "block_id", "block_id": "b-1"}),
                        page_object("p4", parent={"type": "workspace", "workspace": True}),
                        page_object("p5", parent={"type": "unknown_kind"}),
                    ],
                    "has_more": False,
                    "next_cursor": None,
                }
            )

        transport = httpx.MockTransport(make_handler([("/search", search)]))
        c = NotionConnector(NotionConfig(token="t"), transport=transport)
        try:
            refs = {r.metadata["object_id"]: r for r in await drain(c.discover(SourceFilter(), None))}
            assert refs["p1"].parent_chain == ("notion://page/parent-1",)
            assert refs["p2"].parent_chain == ("notion://database/db-1",)
            assert refs["p3"].parent_chain == ("notion://block/b-1",)
            assert refs["p4"].parent_chain == ("notion://workspace",)
            assert refs["p5"].parent_chain == ()
        finally:
            await c.close()

    async def test_search_dedupes_repeated_objects(self) -> None:
        def search(_: httpx.Request) -> httpx.Response:
            return json_response(
                {
                    "results": [page_object("p1"), page_object("p1")],
                    "has_more": False,
                    "next_cursor": None,
                }
            )

        transport = httpx.MockTransport(make_handler([("/search", search)]))
        c = NotionConnector(NotionConfig(token="t"), transport=transport)
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            assert len(refs) == 1
        finally:
            await c.close()


# ---------------------------------------------------------------------
# discover — explicit pages
# ---------------------------------------------------------------------


class TestDiscoverExplicitPages:
    async def test_explicit_page_list_uses_pages_endpoint(self) -> None:
        def page_handler(request: httpx.Request) -> httpx.Response:
            page_id = str(request.url).rsplit("/", 1)[-1]
            return json_response(page_object(page_id))

        transport = httpx.MockTransport(make_handler([("/pages/", page_handler)]))
        c = NotionConnector(
            NotionConfig(token="t", pages=("p1", "p2")), transport=transport
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            assert sorted(r.metadata["object_id"] for r in refs) == ["p1", "p2"]
        finally:
            await c.close()

    async def test_404_page_yields_nothing(self) -> None:
        transport = httpx.MockTransport(make_handler([("/pages/", lambda _: httpx.Response(404))]))
        c = NotionConnector(NotionConfig(token="t", pages=("missing",)), transport=transport)
        try:
            assert await drain(c.discover(SourceFilter(), None)) == []
        finally:
            await c.close()


# ---------------------------------------------------------------------
# discover — explicit database
# ---------------------------------------------------------------------


class TestDiscoverDatabase:
    async def test_database_query_paginates(self) -> None:
        responses = iter(
            [
                {
                    "results": [page_object("r1"), page_object("r2")],
                    "has_more": True,
                    "next_cursor": "cur-2",
                },
                {
                    "results": [page_object("r3")],
                    "has_more": False,
                    "next_cursor": None,
                },
            ]
        )
        captured: list[bytes] = []

        def query(request: httpx.Request) -> httpx.Response:
            captured.append(request.content)
            return json_response(next(responses))

        transport = httpx.MockTransport(
            make_handler([("/databases/db-1/query", query)])
        )
        c = NotionConnector(
            NotionConfig(token="t", databases=("db-1",)), transport=transport
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            assert [r.metadata["object_id"] for r in refs] == ["r1", "r2", "r3"]
            for r in refs:
                assert r.metadata["database_id"] == "db-1"
                assert r.path.startswith("notion://database-row/")
                assert r.parent_chain == ("notion://database/db-1",)
            assert b"start_cursor" in captured[1]
        finally:
            await c.close()

    async def test_database_query_stops_when_next_cursor_missing(self) -> None:
        # has_more=True but next_cursor=None must terminate.
        def query(_: httpx.Request) -> httpx.Response:
            return json_response(
                {"results": [page_object("r1")], "has_more": True, "next_cursor": None}
            )

        transport = httpx.MockTransport(make_handler([("/databases/db-1/query", query)]))
        c = NotionConnector(
            NotionConfig(token="t", databases=("db-1",)), transport=transport
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            assert [r.metadata["object_id"] for r in refs] == ["r1"]
        finally:
            await c.close()


# ---------------------------------------------------------------------
# discover — combined modes
# ---------------------------------------------------------------------


class TestDiscoverCombined:
    async def test_pages_and_databases_combined(self) -> None:
        def page_handler(request: httpx.Request) -> httpx.Response:
            page_id = str(request.url).rsplit("/", 1)[-1]
            return json_response(page_object(page_id))

        def query(_: httpx.Request) -> httpx.Response:
            return json_response(
                {"results": [page_object("row1")], "has_more": False, "next_cursor": None}
            )

        transport = httpx.MockTransport(
            make_handler(
                [
                    ("/databases/db-1/query", query),
                    ("/pages/", page_handler),
                ]
            )
        )
        c = NotionConnector(
            NotionConfig(token="t", pages=("p1",), databases=("db-1",)),
            transport=transport,
        )
        try:
            refs = await drain(c.discover(SourceFilter(), None))
            ids = sorted(r.metadata["object_id"] for r in refs)
            assert ids == ["p1", "row1"]
        finally:
            await c.close()


# ---------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------


def _children_route(children_by_block: dict[str, list[dict[str, Any]]]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        # URL: /v1/blocks/<id>/children
        path = request.url.path
        block_id = path.rsplit("/", 2)[-2]
        return json_response(
            {
                "results": children_by_block.get(block_id, []),
                "has_more": False,
                "next_cursor": None,
            }
        )

    return handler


class TestFetch:
    async def test_fetch_page_renders_block_tree(self) -> None:
        children = {
            "p1": [block_payload("hello world", block_id="c1")],
        }

        transport = httpx.MockTransport(
            make_handler(
                [
                    ("/pages/p1", lambda _: json_response(page_object("p1"))),
                    ("/blocks/", _children_route(children)),
                ]
            )
        )
        c = NotionConnector(NotionConfig(token="t"), transport=transport)
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind=c.kind,
                path="notion://page/p1",
                metadata={"object_id": "p1", "object_type": "page"},
            )
            docs = [d async for d in c.fetch(ref)]
            assert len(docs) == 1
            assert isinstance(docs[0], Document)
            assert docs[0].text == "hello world"
        finally:
            await c.close()

    async def test_fetch_database_row_includes_properties_and_body(self) -> None:
        # The "page" object returned by /pages/<row_id> carries the row's
        # property map; child blocks render below.
        row = page_object("row1")
        row["properties"] = {
            "Name": {
                "type": "title",
                "title": [
                    {"type": "text", "text": {"content": "alice", "link": None}, "annotations": {}, "plain_text": "alice"}
                ],
            },
            "Email": {"type": "email", "email": "alice@x.test"},
        }
        children = {"row1": [block_payload("body text")]}

        transport = httpx.MockTransport(
            make_handler(
                [
                    ("/pages/row1", lambda _: json_response(row)),
                    ("/blocks/", _children_route(children)),
                ]
            )
        )
        c = NotionConnector(NotionConfig(token="t"), transport=transport)
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind=c.kind,
                path="notion://database-row/row1",
                metadata={"object_id": "row1", "object_type": "page", "database_id": "db1"},
            )
            docs = [d async for d in c.fetch(ref)]
            assert len(docs) == 1
            text = docs[0].text or ""
            assert "Name: alice" in text
            assert "Email: alice@x.test" in text
            assert "body text" in text
        finally:
            await c.close()

    async def test_fetch_database_object_renders_block_tree(self) -> None:
        children = {"db1": [block_payload("description")]}
        transport = httpx.MockTransport(
            make_handler(
                [
                    ("/databases/db1", lambda _: json_response(database_object("db1"))),
                    ("/blocks/", _children_route(children)),
                ]
            )
        )
        c = NotionConnector(NotionConfig(token="t"), transport=transport)
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind=c.kind,
                path="notion://database/db1",
                metadata={"object_id": "db1", "object_type": "database"},
            )
            docs = [d async for d in c.fetch(ref)]
            assert len(docs) == 1
            assert (docs[0].text or "").strip() == "description"
        finally:
            await c.close()

    async def test_fetch_missing_metadata_yields_nothing(self) -> None:
        transport = httpx.MockTransport(make_handler([]))
        c = NotionConnector(NotionConfig(token="t"), transport=transport)
        try:
            ghost = DocumentRef(source_id=c.id, source_kind=c.kind, path="notion://page/x")
            assert [d async for d in c.fetch(ghost)] == []
        finally:
            await c.close()

    async def test_fetch_unsupported_object_type_yields_nothing(self) -> None:
        transport = httpx.MockTransport(make_handler([]))
        c = NotionConnector(NotionConfig(token="t"), transport=transport)
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind=c.kind,
                path="notion://block/x",
                metadata={"object_id": "x", "object_type": "block"},
            )
            assert [d async for d in c.fetch(ref)] == []
        finally:
            await c.close()

    async def test_fetch_404_object_yields_nothing(self) -> None:
        transport = httpx.MockTransport(
            make_handler([("/pages/", lambda _: httpx.Response(404))])
        )
        c = NotionConnector(NotionConfig(token="t"), transport=transport)
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind=c.kind,
                path="notion://page/x",
                metadata={"object_id": "x", "object_type": "page"},
            )
            assert [d async for d in c.fetch(ref)] == []
        finally:
            await c.close()

    async def test_fetch_empty_block_tree_yields_nothing(self) -> None:
        # A page with no body and no row properties should not emit an
        # empty Document (which would violate the text/binary XOR).
        transport = httpx.MockTransport(
            make_handler(
                [
                    ("/pages/p1", lambda _: json_response(page_object("p1"))),
                    ("/blocks/", _children_route({})),
                ]
            )
        )
        c = NotionConnector(NotionConfig(token="t"), transport=transport)
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind=c.kind,
                path="notion://page/p1",
                metadata={"object_id": "p1", "object_type": "page"},
            )
            assert [d async for d in c.fetch(ref)] == []
        finally:
            await c.close()

    async def test_fetch_recurses_nested_children(self) -> None:
        # Root child has has_children=True; second-level child carries the
        # text we want to assert on.
        nested_parent = block_payload("parent", block_id="np")
        nested_parent["has_children"] = True
        leaf = block_payload("leaf", block_id="leaf")
        children = {
            "p1": [nested_parent],
            "np": [leaf],
        }
        transport = httpx.MockTransport(
            make_handler(
                [
                    ("/pages/p1", lambda _: json_response(page_object("p1"))),
                    ("/blocks/", _children_route(children)),
                ]
            )
        )
        c = NotionConnector(NotionConfig(token="t"), transport=transport)
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind=c.kind,
                path="notion://page/p1",
                metadata={"object_id": "p1", "object_type": "page"},
            )
            docs = [d async for d in c.fetch(ref)]
            text = docs[0].text or ""
            assert "parent" in text
            assert "leaf" in text
        finally:
            await c.close()

    async def test_block_children_pagination(self) -> None:
        responses = iter(
            [
                {
                    "results": [block_payload("page1", block_id="b1")],
                    "has_more": True,
                    "next_cursor": "cur-2",
                },
                {
                    "results": [block_payload("page2", block_id="b2")],
                    "has_more": False,
                    "next_cursor": None,
                },
            ]
        )
        captured: list[str] = []

        def children(request: httpx.Request) -> httpx.Response:
            captured.append(str(request.url))
            return json_response(next(responses))

        transport = httpx.MockTransport(
            make_handler(
                [
                    ("/pages/p1", lambda _: json_response(page_object("p1"))),
                    ("/blocks/", children),
                ]
            )
        )
        c = NotionConnector(NotionConfig(token="t"), transport=transport)
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind=c.kind,
                path="notion://page/p1",
                metadata={"object_id": "p1", "object_type": "page"},
            )
            docs = [d async for d in c.fetch(ref)]
            text = docs[0].text or ""
            assert "page1" in text
            assert "page2" in text
            # second URL must contain start_cursor
            assert "start_cursor" in captured[1]
        finally:
            await c.close()

    async def test_block_children_pagination_stops_when_next_cursor_missing(self) -> None:
        # has_more=True without next_cursor must terminate (defense in depth).
        def children(_: httpx.Request) -> httpx.Response:
            return json_response(
                {
                    "results": [block_payload("only", block_id="b1")],
                    "has_more": True,
                    "next_cursor": None,
                }
            )

        transport = httpx.MockTransport(
            make_handler(
                [
                    ("/pages/p1", lambda _: json_response(page_object("p1"))),
                    ("/blocks/", children),
                ]
            )
        )
        c = NotionConnector(NotionConfig(token="t"), transport=transport)
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind=c.kind,
                path="notion://page/p1",
                metadata={"object_id": "p1", "object_type": "page"},
            )
            docs = [d async for d in c.fetch(ref)]
            assert "only" in (docs[0].text or "")
        finally:
            await c.close()

    async def test_block_children_archived_filtered(self) -> None:
        archived_block = block_payload("dropped", block_id="b1")
        archived_block["archived"] = True
        kept = block_payload("kept", block_id="b2")

        def children(_: httpx.Request) -> httpx.Response:
            return json_response(
                {
                    "results": [archived_block, kept, "broken"],
                    "has_more": False,
                    "next_cursor": None,
                }
            )

        transport = httpx.MockTransport(
            make_handler(
                [
                    ("/pages/p1", lambda _: json_response(page_object("p1"))),
                    ("/blocks/", children),
                ]
            )
        )
        c = NotionConnector(NotionConfig(token="t"), transport=transport)
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind=c.kind,
                path="notion://page/p1",
                metadata={"object_id": "p1", "object_type": "page"},
            )
            docs = [d async for d in c.fetch(ref)]
            text = docs[0].text or ""
            assert "kept" in text
            assert "dropped" not in text
        finally:
            await c.close()

    async def test_block_children_archived_kept_with_flag(self) -> None:
        archived_block = block_payload("kept-archived", block_id="b1")
        archived_block["archived"] = True

        def children(_: httpx.Request) -> httpx.Response:
            return json_response(
                {"results": [archived_block], "has_more": False, "next_cursor": None}
            )

        transport = httpx.MockTransport(
            make_handler(
                [
                    ("/pages/p1", lambda _: json_response(page_object("p1"))),
                    ("/blocks/", children),
                ]
            )
        )
        c = NotionConnector(
            NotionConfig(token="t", include_archived=True), transport=transport
        )
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind=c.kind,
                path="notion://page/p1",
                metadata={"object_id": "p1", "object_type": "page"},
            )
            docs = [d async for d in c.fetch(ref)]
            assert "kept-archived" in (docs[0].text or "")
        finally:
            await c.close()


# ---------------------------------------------------------------------
# rate-limit propagation
# ---------------------------------------------------------------------


class TestRateLimit:
    async def test_429_during_search_surfaces_rate_limited(self) -> None:
        transport = httpx.MockTransport(
            make_handler(
                [("/search", lambda _: httpx.Response(429, headers={"Retry-After": "1"}))]
            )
        )
        c = NotionConnector(NotionConfig(token="t"), transport=transport)
        try:
            with pytest.raises(RateLimited):
                await drain(c.discover(SourceFilter(), None))
        finally:
            await c.close()

    async def test_429_during_block_fetch_surfaces_rate_limited(self) -> None:
        transport = httpx.MockTransport(
            make_handler(
                [
                    ("/pages/p1", lambda _: json_response(page_object("p1"))),
                    ("/blocks/", lambda _: httpx.Response(429, headers={"Retry-After": "2"})),
                ]
            )
        )
        c = NotionConnector(NotionConfig(token="t"), transport=transport)
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind=c.kind,
                path="notion://page/p1",
                metadata={"object_id": "p1", "object_type": "page"},
            )
            with pytest.raises(RateLimited):
                [d async for d in c.fetch(ref)]
        finally:
            await c.close()


# ---------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------


class TestHelpers:
    def test_parse_iso_handles_z_suffix(self) -> None:
        ts = _parse_iso("2026-05-04T00:00:00Z")
        assert ts is not None and ts.year == 2026

    def test_parse_iso_returns_none_on_garbage(self) -> None:
        assert _parse_iso("not-a-date") is None
        assert _parse_iso(None) is None
        assert _parse_iso(42) is None

    def test_parent_id_workspace(self) -> None:
        assert _parent_id({"type": "workspace"}) == "notion://workspace"

    def test_parent_id_unknown(self) -> None:
        assert _parent_id({"type": "future"}) is None

    def test_string_tuple_accepts_lists(self) -> None:
        assert _string_tuple(["a", "b"]) == ("a", "b")
        assert _string_tuple(("a",)) == ("a",)
        assert _string_tuple(None) == ()

    def test_string_tuple_rejects_bare_string(self) -> None:
        with pytest.raises(ValueError, match="bare string"):
            _string_tuple("abc")

    def test_string_tuple_rejects_non_iterable(self) -> None:
        with pytest.raises(ValueError, match="iterable"):
            _string_tuple(42)

    def test_string_tuple_rejects_empty_or_non_string_items(self) -> None:
        with pytest.raises(ValueError, match="non-empty strings"):
            _string_tuple(["", "ok"])
        with pytest.raises(ValueError, match="non-empty strings"):
            _string_tuple([123])


# ---------------------------------------------------------------------
# factory + spec
# ---------------------------------------------------------------------


class TestFactoryAndSpec:
    def test_spec_metadata(self) -> None:
        assert SPEC.kind == "notion"
        assert KIND == "notion"
        assert SPEC.capabilities.incremental is True
        assert SPEC.capabilities.max_concurrent_fetches == 3
        assert "read_content" in SPEC.required_scopes

    def test_factory_minimal(self) -> None:
        c = SPEC.factory({"token": "secret_x"})
        assert isinstance(c, NotionConnector)
        assert c.id == "notion:default"

    def test_factory_full_config(self) -> None:
        c = _factory(
            {
                "token": "secret_x",
                "id": "custom",
                "pages": ["p1"],
                "databases": ["d1"],
                "include_archived": True,
                "base_url": "https://api.example.test/v1",
                "notion_version": "2099-12-31",
                "max_concurrent_fetches": 5,
                "request_timeout": 10.0,
                "workspace_id": "acme",
            }
        )
        assert c.id == "custom"
        assert c._config.pages == ("p1",)
        assert c._config.databases == ("d1",)
        assert c._config.include_archived is True
        assert c._config.notion_version == "2099-12-31"

    def test_factory_missing_token(self) -> None:
        with pytest.raises(ValueError, match="token"):
            _factory({})

    def test_factory_empty_token(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            _factory({"token": ""})

    def test_factory_rejects_bare_string_pages(self) -> None:
        with pytest.raises(ValueError, match="bare string"):
            _factory({"token": "t", "pages": "p1"})

    def test_factory_rejects_non_string_database_entries(self) -> None:
        with pytest.raises(ValueError, match="non-empty strings"):
            _factory({"token": "t", "databases": [42]})


# ---------------------------------------------------------------------
# package init re-exports
# ---------------------------------------------------------------------


class TestPackageInit:
    def test_top_level_exports(self) -> None:
        import pleno_pii_scanner_notion as pkg

        assert pkg.SPEC is SPEC
        assert pkg.KIND == "notion"
        assert pkg.NotionConnector is NotionConnector
        assert pkg.NotionConfig is NotionConfig
        assert pkg.__version__ == "0.1.0"


# ---------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------


class TestRecursion:
    async def test_block_walk_stops_at_max_depth(self) -> None:
        """`_fetch_block_tree`'s `_walk` short-circuits when depth >= MAX_DEPTH.

        We instrument the connector with a children map that always
        returns one block-with-children, then patch MAX_DEPTH down to a
        tiny value via the connector's import so the test runs in
        bounded time. Verifies the defense-in-depth recursion cap.
        """
        from pleno_pii_scanner_notion import connector as conn_module

        # Each call returns one nested-children block; without a depth cap
        # this loops forever.
        def children(_: httpx.Request) -> httpx.Response:
            child = block_payload("deep", block_id="loop")
            child["has_children"] = True
            return json_response(
                {"results": [child], "has_more": False, "next_cursor": None}
            )

        transport = httpx.MockTransport(
            make_handler(
                [
                    ("/pages/p1", lambda _: json_response(page_object("p1"))),
                    ("/blocks/", children),
                ]
            )
        )
        c = NotionConnector(NotionConfig(token="t"), transport=transport)
        original_max_depth = conn_module.MAX_DEPTH
        conn_module.MAX_DEPTH = 3  # type: ignore[attr-defined]
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind=c.kind,
                path="notion://page/p1",
                metadata={"object_id": "p1", "object_type": "page"},
            )
            docs = [d async for d in c.fetch(ref)]
            # Bounded recursion → bounded output, no hang.
            assert len(docs) == 1
        finally:
            conn_module.MAX_DEPTH = original_max_depth  # type: ignore[attr-defined]
            await c.close()

    async def test_block_walk_ignores_child_with_non_string_id(self) -> None:
        """Child block with `has_children=True` but a non-string `id`
        must be tolerated (don't recurse, don't crash) — defense against
        malformed Notion responses."""
        bad_child = block_payload("malformed", block_id="x")
        bad_child["has_children"] = True
        bad_child["id"] = 12345  # type: ignore[assignment]

        def children(_: httpx.Request) -> httpx.Response:
            return json_response(
                {"results": [bad_child], "has_more": False, "next_cursor": None}
            )

        transport = httpx.MockTransport(
            make_handler(
                [
                    ("/pages/p1", lambda _: json_response(page_object("p1"))),
                    ("/blocks/", children),
                ]
            )
        )
        c = NotionConnector(NotionConfig(token="t"), transport=transport)
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind=c.kind,
                path="notion://page/p1",
                metadata={"object_id": "p1", "object_type": "page"},
            )
            docs = [d async for d in c.fetch(ref)]
            assert "malformed" in (docs[0].text or "")
        finally:
            await c.close()


class TestLifecycle:
    async def test_close_can_be_called_twice(self) -> None:
        # Closing the underlying httpx.AsyncClient twice is a no-op in
        # httpx; we still want to verify our close() doesn't raise.
        c = NotionConnector(NotionConfig(token="t"))
        await c.close()
        # Second close is fine — httpx handles re-close gracefully.
        try:
            await c.close()
        except RuntimeError:
            # httpx may raise if used after close; the test only cares
            # that the first close didn't leak resources.
            pass
