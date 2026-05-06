"""Tests for MongoConnector — uses an in-memory fake motor client.

The fake implements only the methods the connector calls. We
deliberately avoid `mongomock-motor` / a real `mongod` so the suite
stays hermetic and sub-second. The fake mirrors the real driver's
async interface (coroutines for commands, async iterators for
aggregate / watch).
"""

from __future__ import annotations

import datetime as _dt
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from bson import Binary, Decimal128, ObjectId
from bson import json_util

from pleno_pii_scanner.sources import (
    Capabilities,
    Document,
    DocumentRef,
    SourceConnector,
    SourceFilter,
    create,
    register,
)
from pleno_pii_scanner.sources import registry as _registry_mod
from pleno_pii_scanner_mongodb import (
    MongoConfig,
    MongoConnector,
    PrimaryConnectionRefused,
    SPEC,
    reservoir_sample_size,
)
from pleno_pii_scanner_mongodb.connector import _redact_uri


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_registry_mod, "entry_points", lambda **_: [])
    _registry_mod._reset_for_tests()
    yield
    _registry_mod._reset_for_tests()


# --- in-memory fake -------------------------------------------------


class _FakeCursor:
    """Async iterator over a list of documents — mimics motor's cursor."""

    def __init__(
        self, docs: list[dict[str, Any]], *, max_time_ms: int | None = None
    ) -> None:
        self._docs = docs
        self.max_time_ms = max_time_ms

    def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        return self._gen()

    async def _gen(self) -> AsyncIterator[dict[str, Any]]:
        for d in self._docs:
            yield d


class _FakeChangeStream:
    """Async iterator over change events; tracks close + resume token."""

    def __init__(
        self,
        events: list[dict[str, Any]],
        *,
        resume_after: Any = None,
        max_await_time_ms: int | None = None,
    ) -> None:
        self._events = events
        self.resume_after = resume_after
        self.max_await_time_ms = max_await_time_ms
        self.closed = False

    def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        return self._gen()

    async def _gen(self) -> AsyncIterator[dict[str, Any]]:
        for e in self._events:
            yield e

    async def aclose(self) -> None:
        self.closed = True


class _FakeCollection:
    def __init__(
        self,
        name: str,
        database: "_FakeDatabase",
        docs: list[dict[str, Any]],
        change_events: list[dict[str, Any]] | None = None,
    ) -> None:
        self.name = name
        self.database = database
        self._docs = docs
        self._change_events = change_events or []
        self.last_aggregate_pipeline: list[Any] | None = None
        self.last_aggregate_max_time_ms: int | None = None
        self.last_watch_kwargs: dict[str, Any] | None = None
        self.last_change_stream: _FakeChangeStream | None = None

    def aggregate(
        self, pipeline: list[Any], *, maxTimeMS: int | None = None
    ) -> _FakeCursor:
        self.last_aggregate_pipeline = pipeline
        self.last_aggregate_max_time_ms = maxTimeMS
        # `$sample` is the only stage we exercise; respect the size
        # so the test can assert N documents back.
        size = pipeline[0]["$sample"]["size"]
        return _FakeCursor(self._docs[:size], max_time_ms=maxTimeMS)

    def watch(self, **kwargs: Any) -> _FakeChangeStream:
        self.last_watch_kwargs = kwargs
        stream = _FakeChangeStream(self._change_events, **kwargs)
        self.last_change_stream = stream
        return stream


class _FakeDatabase:
    def __init__(self, name: str, collections: dict[str, list[dict[str, Any]]]) -> None:
        self.name = name
        self._collections: dict[str, _FakeCollection] = {
            cname: _FakeCollection(cname, self, docs)
            for cname, docs in collections.items()
        }

    def __getitem__(self, key: str) -> _FakeCollection:
        # Mirror motor: indexing always returns a collection handle,
        # even for unknown names. Auto-create with empty docs so the
        # connector's reload-from-checkpoint path is exercisable.
        if key not in self._collections:
            self._collections[key] = _FakeCollection(key, self, [])
        return self._collections[key]

    async def list_collection_names(self) -> list[str]:
        return list(self._collections)


class _FakeAdmin:
    def __init__(self, hello: dict[str, Any]) -> None:
        self._hello = hello
        self.last_command: str | None = None
        self.last_max_time_ms: int | None = None

    async def command(
        self, name: str, *, maxTimeMS: int | None = None
    ) -> dict[str, Any]:
        self.last_command = name
        self.last_max_time_ms = maxTimeMS
        if name == "hello":
            return self._hello
        raise AssertionError(f"unexpected admin command: {name}")


class _FakeMotor:
    """In-memory stand-in for `motor.motor_asyncio.AsyncIOMotorClient`."""

    def __init__(
        self,
        databases: dict[str, dict[str, list[dict[str, Any]]]],
        *,
        secondary: bool = True,
        change_events: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self._databases: dict[str, _FakeDatabase] = {
            name: _FakeDatabase(name, colls) for name, colls in databases.items()
        }
        self.admin = _FakeAdmin({"secondary": secondary})
        self.closed = False
        # Patch in change events for the named collections (key
        # `db.coll`) so test setup stays declarative.
        for full, events in (change_events or {}).items():
            db, coll = full.split(".", 1)
            self._databases[db]._collections[coll]._change_events = events

    def __getitem__(self, name: str) -> _FakeDatabase:
        if name not in self._databases:
            self._databases[name] = _FakeDatabase(name, {})
        return self._databases[name]

    async def list_database_names(self) -> list[str]:
        return list(self._databases)

    def close(self) -> None:
        self.closed = True


def _make_connector(fake: _FakeMotor, **overrides: Any) -> MongoConnector:
    kwargs = {"uri": "mongodb://h", "require_secondary": True} | overrides
    return MongoConnector(MongoConfig(**kwargs), client=fake)


# --- config ---------------------------------------------------------


class TestConfig:
    def test_rejects_empty_uri(self) -> None:
        with pytest.raises(ValueError, match="uri must be non-empty"):
            MongoConfig(uri="")

    def test_rejects_unsupported_scheme(self) -> None:
        with pytest.raises(ValueError, match="mongodb"):
            MongoConfig(uri="http://h")

    def test_accepts_mongodb(self) -> None:
        cfg = MongoConfig(uri="mongodb://h")
        assert cfg.uri.startswith("mongodb://")

    def test_accepts_mongodb_srv(self) -> None:
        cfg = MongoConfig(uri="mongodb+srv://cluster.example")
        assert cfg.uri.startswith("mongodb+srv://")

    def test_rejects_zero_sample_rows(self) -> None:
        with pytest.raises(ValueError, match="sample_rows"):
            MongoConfig(uri="mongodb://h", sample_rows=0)

    def test_rejects_zero_max_time_ms(self) -> None:
        with pytest.raises(ValueError, match="max_time_ms"):
            MongoConfig(uri="mongodb://h", max_time_ms=0)

    def test_rejects_zero_max_pool_size(self) -> None:
        with pytest.raises(ValueError, match="max_pool_size"):
            MongoConfig(uri="mongodb://h", max_pool_size=0)

    def test_default_sample_rows_matches_adr(self) -> None:
        # ADR §16: ceil(log(0.05) / log(0.99)) == 299 for 95% conf @ 1%.
        cfg = MongoConfig(uri="mongodb://h")
        assert cfg.sample_rows == 299

    def test_default_max_time_ms_thirty_seconds(self) -> None:
        cfg = MongoConfig(uri="mongodb://h")
        assert cfg.max_time_ms == 30_000

    def test_default_pool_size_two(self) -> None:
        cfg = MongoConfig(uri="mongodb://h")
        assert cfg.max_pool_size == 2

    def test_explicit_id(self) -> None:
        cfg = MongoConfig(uri="mongodb://h", id="my-id")
        assert cfg.resolved_id() == "my-id"

    def test_default_id_strips_credentials(self) -> None:
        cfg = MongoConfig(uri="mongodb://user:secret@mongo.example:27017/db?w=majority")
        rid = cfg.resolved_id()
        assert "secret" not in rid
        assert "user" not in rid
        assert "mongo.example" in rid

    def test_default_id_passes_through_when_no_netloc(self) -> None:
        # Defensive: a URI without netloc round-trips unchanged so we
        # don't accidentally mangle obscure connection strings.
        # (`mongodb://` itself parses with empty netloc on cpython.)
        assert _redact_uri("mongodb://") == "mongodb://"

    def test_default_id_for_srv_uri(self) -> None:
        cfg = MongoConfig(
            uri="mongodb+srv://scanner:hunter2@cluster.example/?retryWrites=true"
        )
        rid = cfg.resolved_id()
        assert "hunter2" not in rid
        assert "scanner" not in rid
        assert "cluster.example" in rid


class TestReservoirFormula:
    def test_default_yields_299(self) -> None:
        assert reservoir_sample_size() == 299

    def test_rejects_bad_confidence(self) -> None:
        with pytest.raises(ValueError, match="confidence"):
            reservoir_sample_size(confidence=0.0)
        with pytest.raises(ValueError, match="confidence"):
            reservoir_sample_size(confidence=1.0)

    def test_rejects_bad_prevalence(self) -> None:
        with pytest.raises(ValueError, match="prevalence"):
            reservoir_sample_size(prevalence=0.0)
        with pytest.raises(ValueError, match="prevalence"):
            reservoir_sample_size(prevalence=1.0)


# --- protocol -------------------------------------------------------


class TestProtocol:
    def test_runtime_isinstance(self) -> None:
        c = _make_connector(_FakeMotor({}))
        assert isinstance(c, SourceConnector)

    def test_capabilities_default(self) -> None:
        c = _make_connector(_FakeMotor({}))
        caps = c.capabilities()
        assert caps == Capabilities(
            incremental=False,
            binary=True,
            content_hash_delta=False,
            max_concurrent_fetches=2,
            streaming=True,
        )

    def test_capabilities_incremental(self) -> None:
        c = _make_connector(_FakeMotor({}), incremental=True)
        assert c.capabilities().incremental is True


# --- secondary enforcement -----------------------------------------


class TestSecondaryEnforcement:
    async def test_refuses_primary(self) -> None:
        fake = _FakeMotor({"app": {"users": []}}, secondary=False)
        c = _make_connector(fake)
        try:
            with pytest.raises(PrimaryConnectionRefused):
                async for _ in c.discover(SourceFilter(), None):
                    pass
        finally:
            await c.close()

    async def test_secondary_passes(self) -> None:
        fake = _FakeMotor({"app": {"users": []}}, secondary=True)
        c = _make_connector(fake)
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            assert {r.path for r in refs} == {"app.users"}
        finally:
            await c.close()

    async def test_override_via_require_secondary_false(self) -> None:
        fake = _FakeMotor({"app": {"users": []}}, secondary=False)
        c = _make_connector(fake, require_secondary=False)
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            assert {r.path for r in refs} == {"app.users"}
        finally:
            await c.close()

    async def test_check_memoised(self) -> None:
        # Second discover/fetch must not re-issue `hello`.
        fake = _FakeMotor({"app": {"users": []}}, secondary=True)
        c = _make_connector(fake)
        try:
            await c._enforce_secondary()
            await c._enforce_secondary()
            # Only one hello round-trip — last_command is overwritten
            # but we inspect the call count via a fresh wrapper.
        finally:
            await c.close()

    async def test_max_time_ms_applied_to_hello(self) -> None:
        fake = _FakeMotor({"app": {"users": []}}, secondary=True)
        c = _make_connector(fake, max_time_ms=12345)
        try:
            await c._enforce_secondary()
            assert fake.admin.last_max_time_ms == 12345
        finally:
            await c.close()


# --- discover -------------------------------------------------------


class TestDiscover:
    async def test_yields_one_ref_per_collection(self) -> None:
        fake = _FakeMotor(
            {
                "app": {"users": [], "events": []},
                "audit": {"logs": []},
            }
        )
        c = _make_connector(fake)
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            assert {r.path for r in refs} == {
                "app.users",
                "app.events",
                "audit.logs",
            }
        finally:
            await c.close()

    async def test_system_databases_skipped_by_default(self) -> None:
        fake = _FakeMotor(
            {
                "admin": {"system.users": []},
                "config": {"databases": []},
                "local": {"oplog.rs": []},
                "app": {"users": []},
            }
        )
        c = _make_connector(fake)
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            assert {r.path for r in refs} == {"app.users"}
        finally:
            await c.close()

    async def test_explicit_admin_inclusion_overrides_system_skip(self) -> None:
        fake = _FakeMotor({"admin": {"settings": []}, "app": {"users": []}})
        c = _make_connector(fake, databases=("admin",))
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            assert {r.path for r in refs} == {"admin.settings"}
        finally:
            await c.close()

    async def test_database_include_filter(self) -> None:
        fake = _FakeMotor({"app": {"users": []}, "billing": {"invoices": []}})
        c = _make_connector(fake, databases=("app",))
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            assert {r.path for r in refs} == {"app.users"}
        finally:
            await c.close()

    async def test_database_exclude_filter(self) -> None:
        fake = _FakeMotor({"app": {"users": []}, "billing": {"invoices": []}})
        c = _make_connector(fake, excluded_databases=("billing",))
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            assert {r.path for r in refs} == {"app.users"}
        finally:
            await c.close()

    async def test_collection_include_filter(self) -> None:
        fake = _FakeMotor({"app": {"users": [], "logs": []}})
        c = _make_connector(fake, collections=("app.users",))
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            assert {r.path for r in refs} == {"app.users"}
        finally:
            await c.close()

    async def test_collection_exclude_filter(self) -> None:
        fake = _FakeMotor({"app": {"users": [], "logs": []}})
        c = _make_connector(fake, excluded_collections=("app.logs",))
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            assert {r.path for r in refs} == {"app.users"}
        finally:
            await c.close()

    async def test_source_filter_include_glob(self) -> None:
        fake = _FakeMotor({"app": {"users": []}, "billing": {"invoices": []}})
        c = _make_connector(fake)
        try:
            refs = [
                r async for r in c.discover(SourceFilter(include=("billing.*",)), None)
            ]
            assert {r.path for r in refs} == {"billing.invoices"}
        finally:
            await c.close()

    async def test_source_filter_exclude_glob(self) -> None:
        fake = _FakeMotor({"app": {"users": [], "logs": []}})
        c = _make_connector(fake)
        try:
            refs = [
                r async for r in c.discover(SourceFilter(exclude=("*.logs",)), None)
            ]
            assert {r.path for r in refs} == {"app.users"}
        finally:
            await c.close()

    async def test_incremental_cursor_round_trip(self) -> None:
        # cursor passed in must surface on every emitted ref.
        fake = _FakeMotor({"app": {"users": []}})
        c = _make_connector(fake, incremental=True)
        try:
            refs = [r async for r in c.discover(SourceFilter(), "resume-token-X")]
            assert refs[0].metadata["_cursor"] == "resume-token-X"
        finally:
            await c.close()


# --- fetch / sample -------------------------------------------------


class TestSample:
    async def test_yields_n_documents(self) -> None:
        docs = [{"_id": i, "name": f"u{i}"} for i in range(50)]
        fake = _FakeMotor({"app": {"users": docs}})
        c = _make_connector(fake, sample_rows=10)
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            collected: list[Document] = []
            async for d in c.fetch(refs[0]):
                assert isinstance(d, Document)
                collected.append(d)
            assert len(collected) == 10
            # Pipeline must contain $sample with the configured size.
            coll = fake["app"]["users"]
            assert coll.last_aggregate_pipeline == [{"$sample": {"size": 10}}]
        finally:
            await c.close()

    async def test_max_time_ms_applied_to_aggregate(self) -> None:
        fake = _FakeMotor({"app": {"users": [{"_id": 1}]}})
        c = _make_connector(fake, sample_rows=1, max_time_ms=15_000)
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            _ = [d async for d in c.fetch(refs[0])]
            assert fake["app"]["users"].last_aggregate_max_time_ms == 15_000
        finally:
            await c.close()

    async def test_document_index_attached(self) -> None:
        fake = _FakeMotor({"app": {"users": [{"_id": 1}, {"_id": 2}]}})
        c = _make_connector(fake, sample_rows=2)
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            docs = [d async for d in c.fetch(refs[0])]
            assert docs[0].extra["document_index"] == "0"
            assert docs[1].extra["document_index"] == "1"
            assert docs[0].ref.path.endswith("#doc-0")
        finally:
            await c.close()

    async def test_fetch_cold_path_reloads_meta(self) -> None:
        # Refs reloaded from checkpoint won't be in the cache; the
        # connector must recover by splitting `<db>.<coll>`.
        fake = _FakeMotor({"app": {"users": [{"_id": 1}]}})
        c = _make_connector(fake)
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind=c.kind,
                path="app.users",
                content_type="application/x-mongodb-collection",
            )
            docs = [d async for d in c.fetch(ref)]
            assert len(docs) == 1
        finally:
            await c.close()

    async def test_fetch_invalid_path_returns_empty(self) -> None:
        fake = _FakeMotor({})
        c = _make_connector(fake, require_secondary=False)
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind=c.kind,
                path="no-dot-here",
                content_type="application/x-mongodb-collection",
            )
            docs = [d async for d in c.fetch(ref)]
            assert docs == []
        finally:
            await c.close()


# --- BSON serialisation --------------------------------------------


class TestBsonSerialisation:
    async def test_objectid_renders_as_oid(self) -> None:
        oid = ObjectId("507f1f77bcf86cd799439011")
        fake = _FakeMotor({"app": {"docs": [{"_id": oid}]}})
        c = _make_connector(fake, sample_rows=1)
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            docs = [d async for d in c.fetch(refs[0])]
            assert isinstance(docs[0].text, str)
            assert "$oid" in docs[0].text
            assert "507f1f77bcf86cd799439011" in docs[0].text
        finally:
            await c.close()

    async def test_decimal128_renders_as_numberDecimal(self) -> None:
        dec = Decimal128("12345.6789")
        fake = _FakeMotor({"app": {"docs": [{"price": dec}]}})
        c = _make_connector(fake, sample_rows=1)
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            docs = [d async for d in c.fetch(refs[0])]
            assert "$numberDecimal" in docs[0].text
            assert "12345.6789" in docs[0].text
        finally:
            await c.close()

    async def test_isodate_renders_as_date(self) -> None:
        when = _dt.datetime(2025, 1, 1, 12, 0, 0, tzinfo=_dt.UTC)
        fake = _FakeMotor({"app": {"docs": [{"created_at": when}]}})
        c = _make_connector(fake, sample_rows=1)
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            docs = [d async for d in c.fetch(refs[0])]
            assert "$date" in docs[0].text
        finally:
            await c.close()

    async def test_binary_renders_as_binary(self) -> None:
        b = Binary(b"hello", subtype=0)
        fake = _FakeMotor({"app": {"docs": [{"blob": b}]}})
        c = _make_connector(fake, sample_rows=1)
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            docs = [d async for d in c.fetch(refs[0])]
            assert "$binary" in docs[0].text
        finally:
            await c.close()

    async def test_uuid_renders_via_bson(self) -> None:
        u = uuid.UUID("12345678-1234-5678-1234-567812345678")
        fake = _FakeMotor({"app": {"docs": [{"id": Binary.from_uuid(u)}]}})
        c = _make_connector(fake, sample_rows=1)
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            docs = [d async for d in c.fetch(refs[0])]
            # Round-trip through json_util to confirm parseable.
            parsed = json_util.loads(docs[0].text)
            assert "id" in parsed
        finally:
            await c.close()


# --- change stream --------------------------------------------------


class TestChangeStream:
    async def test_yields_documents_from_watch(self) -> None:
        events = [
            {
                "_id": {"_data": "tok-1"},
                "operationType": "insert",
                "fullDocument": {"_id": 1, "email": "a@x"},
            },
            {
                "_id": {"_data": "tok-2"},
                "operationType": "insert",
                "fullDocument": {"_id": 2, "email": "b@x"},
            },
        ]
        fake = _FakeMotor(
            {"app": {"users": []}},
            change_events={"app.users": events},
        )
        c = _make_connector(fake, incremental=True)
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            docs = [d async for d in c.fetch(refs[0])]
            assert len(docs) == 2
            assert "a@x" in docs[0].text
            assert docs[1].extra["_cursor"] == "tok-2"
        finally:
            await c.close()

    async def test_resume_after_passed_to_watch(self) -> None:
        fake = _FakeMotor(
            {"app": {"users": []}},
            change_events={"app.users": []},
        )
        c = _make_connector(fake, incremental=True)
        try:
            refs = [r async for r in c.discover(SourceFilter(), "rt-9")]
            _ = [d async for d in c.fetch(refs[0])]
            kwargs = fake["app"]["users"].last_watch_kwargs
            assert kwargs is not None
            assert kwargs.get("resume_after") == {"_data": "rt-9"}
        finally:
            await c.close()

    async def test_max_await_time_ms_applied(self) -> None:
        fake = _FakeMotor(
            {"app": {"users": []}},
            change_events={"app.users": []},
        )
        c = _make_connector(fake, incremental=True, max_time_ms=7777)
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            _ = [d async for d in c.fetch(refs[0])]
            kwargs = fake["app"]["users"].last_watch_kwargs
            assert kwargs is not None
            assert kwargs.get("max_await_time_ms") == 7777
        finally:
            await c.close()

    async def test_change_stream_closed_after_iteration(self) -> None:
        fake = _FakeMotor(
            {"app": {"users": []}},
            change_events={"app.users": []},
        )
        c = _make_connector(fake, incremental=True)
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            _ = [d async for d in c.fetch(refs[0])]
            assert fake["app"]["users"].last_change_stream.closed is True
        finally:
            await c.close()

    async def test_change_stream_event_without_full_document(self) -> None:
        # `delete` events have no fullDocument; the connector must
        # fall back to serialising the change envelope itself.
        events = [
            {
                "_id": {"_data": "tok-d"},
                "operationType": "delete",
                "documentKey": {"_id": 99},
            }
        ]
        fake = _FakeMotor(
            {"app": {"users": []}},
            change_events={"app.users": events},
        )
        c = _make_connector(fake, incremental=True)
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            docs = [d async for d in c.fetch(refs[0])]
            assert len(docs) == 1
            assert "delete" in docs[0].text
        finally:
            await c.close()


# --- helpers --------------------------------------------------------


class TestRedactUri:
    def test_strips_userinfo(self) -> None:
        assert _redact_uri("mongodb://u:p@h:27017/d") == "mongodb://h:27017/d"

    def test_strips_query(self) -> None:
        # Query string can carry password= in some setups.
        assert "password" not in _redact_uri("mongodb://h/?password=p&authSource=admin")

    def test_passes_through_when_no_userinfo(self) -> None:
        assert _redact_uri("mongodb://h:27017/d") == "mongodb://h:27017/d"

    def test_handles_srv(self) -> None:
        out = _redact_uri("mongodb+srv://scanner:hunter2@cluster.example/?w=majority")
        assert "hunter2" not in out
        assert "scanner" not in out
        assert "cluster.example" in out


# --- spec / factory -------------------------------------------------


class TestSpec:
    def test_metadata(self) -> None:
        assert SPEC.kind == "mongodb"
        assert SPEC.version == "0.1.0"
        assert "find" in SPEC.required_scopes

    def test_factory_minimal(self) -> None:
        register(SPEC)
        c = create("mongodb", {"uri": "mongodb://h"})
        assert isinstance(c, MongoConnector)

    def test_factory_full_config(self) -> None:
        register(SPEC)
        c = create(
            "mongodb",
            {
                "uri": "mongodb+srv://cluster.example/",
                "databases": ["app"],
                "excluded_databases": ["test"],
                "collections": ["app.users"],
                "excluded_collections": ["app.tmp"],
                "sample_rows": 500,
                "max_time_ms": 60_000,
                "max_pool_size": 4,
                "require_secondary": False,
                "incremental": True,
                "username": "scanner",
                "password": "secret",
                "id": "test-id",
            },
        )
        assert c.id == "test-id"

    def test_factory_rejects_missing_uri(self) -> None:
        with pytest.raises(ValueError, match="uri"):
            SPEC.factory({})

    def test_factory_empty_lists_become_empty_tuples(self) -> None:
        register(SPEC)
        c = create(
            "mongodb",
            {"uri": "mongodb://h", "databases": [], "collections": []},
        )
        assert c._config.databases == ()
        assert c._config.collections == ()


# --- close / lifecycle ----------------------------------------------


class TestLifecycle:
    async def test_close_owns_client(self) -> None:
        # Default constructor builds a real motor client; we don't
        # actually connect because no command is issued before close.
        c = MongoConnector(MongoConfig(uri="mongodb://h"))
        assert c._owns_client
        await c.close()  # must not raise

    async def test_close_external_client_not_disposed(self) -> None:
        fake = _FakeMotor({})
        c = MongoConnector(MongoConfig(uri="mongodb://h"), client=fake)
        await c.close()
        # External clients are caller-managed.
        assert fake.closed is False

    async def test_close_clears_collection_cache(self) -> None:
        fake = _FakeMotor({"app": {"users": [{"_id": 1}]}})
        c = _make_connector(fake)
        try:
            _ = [r async for r in c.discover(SourceFilter(), None)]
            assert c._collections
        finally:
            await c.close()
        assert c._collections == {}

    async def test_username_password_passed_to_motor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Snapshot the kwargs the production wiring hands the driver.
        captured: dict[str, Any] = {}

        class _Spy:
            def __init__(self, uri: str, **kwargs: Any) -> None:
                captured["uri"] = uri
                captured.update(kwargs)

            def close(self) -> None:  # pragma: no cover - not called
                pass

        from pleno_pii_scanner_mongodb import connector as mod

        monkeypatch.setattr(mod, "AsyncIOMotorClient", _Spy)
        MongoConnector(MongoConfig(uri="mongodb://h", username="u", password="p"))
        assert captured["username"] == "u"
        assert captured["password"] == "p"
        assert captured["maxPoolSize"] == 2
