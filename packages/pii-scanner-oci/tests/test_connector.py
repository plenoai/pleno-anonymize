"""Tests for OciConnector — uses httpx.MockTransport doubles."""

from __future__ import annotations

import gzip
import io
import json
import tarfile
from collections.abc import Callable

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
from pleno_pii_scanner.sources import registry as _registry_mod
from pleno_pii_scanner_oci import OciConfig, OciConnector, SPEC


_OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
_OCI_INDEX = "application/vnd.oci.image.index.v1+json"
_OCI_LAYER_GZIP = "application/vnd.oci.image.layer.v1.tar+gzip"
_OCI_CONFIG = "application/vnd.oci.image.config.v1+json"


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_registry_mod, "entry_points", lambda **_: [])
    _registry_mod._reset_for_tests()
    yield
    _registry_mod._reset_for_tests()


def _gz_tar(*entries: tuple[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, body in entries:
            info = tarfile.TarInfo(name=name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return gzip.compress(buf.getvalue())


def _config_blob() -> bytes:
    body = {
        "config": {
            "Env": ["AWS_SECRET=hunter2"],
            "Cmd": ["sh"],
            "Entrypoint": ["/usr/local/bin/app"],
        },
        "history": [{"created_by": "/bin/sh -c apt-get install"}],
    }
    return json.dumps(body).encode()


def _build_handler(routes: dict[str, Callable[[httpx.Request], httpx.Response]]):
    """Build a MockTransport handler from a `path → responder` map.

    Path matching is suffix-based so test cases stay readable
    (`"/v2/lib/x/manifests/v1"` rather than full URLs).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        for suffix, responder in routes.items():
            if request.url.path.endswith(suffix) or str(request.url).endswith(suffix):
                return responder(request)
        return httpx.Response(404, content=b"unmatched: " + str(request.url).encode())

    return handler


# --- config ---------------------------------------------------------


class TestConfig:
    def test_rejects_empty_references(self) -> None:
        with pytest.raises(ValueError, match="references"):
            OciConfig(references=())

    def test_user_without_password_rejected(self) -> None:
        with pytest.raises(ValueError, match="username and password"):
            OciConfig(references=("alpine",), username="u")

    def test_password_without_user_rejected(self) -> None:
        with pytest.raises(ValueError, match="username and password"):
            OciConfig(references=("alpine",), password="p")

    def test_explicit_id(self) -> None:
        cfg = OciConfig(references=("alpine",), id="custom")
        assert cfg.resolved_id() == "custom"

    def test_default_id_is_stable_hash(self) -> None:
        a = OciConfig(references=("alpine", "ghcr.io/foo/bar:1"))
        b = OciConfig(references=("ghcr.io/foo/bar:1", "alpine"))
        # Order-independent — sorted before hashing.
        assert a.resolved_id() == b.resolved_id()


# --- protocol -------------------------------------------------------


class TestProtocol:
    def test_runtime_isinstance(self) -> None:
        c = OciConnector(OciConfig(references=("alpine",)))
        assert isinstance(c, SourceConnector)

    def test_capabilities(self) -> None:
        c = OciConnector(OciConfig(references=("alpine",)))
        assert c.capabilities() == Capabilities(
            incremental=False,
            binary=True,
            content_hash_delta=True,
            max_concurrent_fetches=4,
            streaming=True,
        )


# --- single image (no index) ----------------------------------------


class TestSingleImage:
    async def test_full_pipeline(self) -> None:
        config_bytes = _config_blob()
        layer_bytes = _gz_tar(("etc/secret.env", b"DB_PASSWORD=hunter2\n"))
        config_digest = "sha256:" + "c" * 64
        layer_digest = "sha256:" + "1" * 64

        manifest_body = {
            "config": {
                "mediaType": _OCI_CONFIG,
                "digest": config_digest,
                "size": len(config_bytes),
            },
            "layers": [
                {
                    "mediaType": _OCI_LAYER_GZIP,
                    "digest": layer_digest,
                    "size": len(layer_bytes),
                }
            ],
        }

        def manifest_responder(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=manifest_body,
                headers={"Content-Type": _OCI_MANIFEST},
            )

        def config_blob_responder(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=config_bytes)

        def layer_blob_responder(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=layer_bytes)

        routes = {
            "/manifests/v1": manifest_responder,
            f"/blobs/{config_digest}": config_blob_responder,
            f"/blobs/{layer_digest}": layer_blob_responder,
        }
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_build_handler(routes))
        ) as client:
            c = OciConnector(
                OciConfig(references=("ghcr.io/acme/api:v1",)),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                kinds = [r.metadata.get("kind") for r in refs]
                # Config first, then layer
                assert kinds == ["config", "layer"]

                # Fetch config
                config_ref = refs[0]
                docs = []
                async for d in c.fetch(config_ref):
                    assert isinstance(d, Document)
                    docs.append(d)
                assert len(docs) == 1
                assert "AWS_SECRET=hunter2" in docs[0].text

                # Fetch layer (yields per-member Documents)
                layer_ref = refs[1]
                docs = []
                async for d in c.fetch(layer_ref):
                    assert isinstance(d, Document)
                    docs.append(d)
                assert len(docs) == 1
                assert b"DB_PASSWORD=hunter2" in docs[0].binary
            finally:
                await c.close()


# --- multi-arch image-index ----------------------------------------


class TestImageIndex:
    async def test_index_resolves_to_default_platform(self) -> None:
        config_digest = "sha256:" + "a" * 64
        amd_manifest_digest = "sha256:" + "1" * 64

        index_body = {
            "manifests": [
                {
                    "mediaType": _OCI_MANIFEST,
                    "digest": amd_manifest_digest,
                    "size": 100,
                    "platform": {"os": "linux", "architecture": "amd64"},
                },
                {
                    "mediaType": _OCI_MANIFEST,
                    "digest": "sha256:" + "2" * 64,
                    "size": 100,
                    "platform": {"os": "linux", "architecture": "arm64"},
                },
            ]
        }
        amd_manifest = {
            "config": {
                "mediaType": _OCI_CONFIG,
                "digest": config_digest,
                "size": 10,
            },
            "layers": [],
        }

        def index_responder(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=index_body,
                headers={"Content-Type": _OCI_INDEX},
            )

        def amd_manifest_responder(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=amd_manifest,
                headers={"Content-Type": _OCI_MANIFEST},
            )

        routes = {
            "/manifests/v1": index_responder,
            f"/manifests/{amd_manifest_digest}": amd_manifest_responder,
        }
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_build_handler(routes))
        ) as client:
            c = OciConnector(
                OciConfig(
                    references=("ghcr.io/acme/api:v1",),
                    default_platform="linux/amd64",
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                # config-only image (no layers) → 1 ref
                assert len(refs) == 1
                assert refs[0].metadata["platform"] == "linux/amd64"
            finally:
                await c.close()


# --- layer dedup ----------------------------------------------------


class TestLayerDedup:
    async def test_shared_layer_emitted_once(self) -> None:
        config_bytes = _config_blob()
        layer_bytes = _gz_tar(("a.txt", b"x\n"))
        config_digest = "sha256:" + "c" * 64
        layer_digest = "sha256:" + "1" * 64

        # Two images, identical layer digest (shared base layer).
        manifest_body = {
            "config": {
                "mediaType": _OCI_CONFIG,
                "digest": config_digest,
                "size": len(config_bytes),
            },
            "layers": [
                {
                    "mediaType": _OCI_LAYER_GZIP,
                    "digest": layer_digest,
                    "size": len(layer_bytes),
                }
            ],
        }

        def manifest_responder(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=manifest_body,
                headers={"Content-Type": _OCI_MANIFEST},
            )

        def config_blob_responder(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=config_bytes)

        def layer_blob_responder(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=layer_bytes)

        routes = {
            "/manifests/v1": manifest_responder,
            "/manifests/v2": manifest_responder,
            f"/blobs/{config_digest}": config_blob_responder,
            f"/blobs/{layer_digest}": layer_blob_responder,
        }
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_build_handler(routes))
        ) as client:
            c = OciConnector(
                OciConfig(
                    references=(
                        "ghcr.io/acme/api:v1",
                        "ghcr.io/acme/api:v2",
                    )
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                # 2 configs + 1 dedup'd layer (the layer for the second
                # image is only emitted after fetch() of the first marks
                # the digest as scanned)
                # Fetch the first layer to populate the dedup cache.
                layer_refs = [r for r in refs if r.metadata.get("kind") == "layer"]
                assert len(layer_refs) == 1  # second image's layer was deduped

                async for _ in c.fetch(layer_refs[0]):
                    pass
                # Now run discover again — second image's layer still skipped.
                refs2 = [r async for r in c.discover(SourceFilter(), None)]
                layer_refs2 = [r for r in refs2 if r.metadata.get("kind") == "layer"]
                assert len(layer_refs2) == 0
            finally:
                await c.close()


# --- malformed reference --------------------------------------------


class TestMalformedReference:
    async def test_invalid_ref_yields_error_doc(self) -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _r: httpx.Response(404))
        ) as client:
            c = OciConnector(
                OciConfig(references=("alpine@sha256:bogus",)),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                assert len(refs) == 1
                assert refs[0].path.startswith("<invalid:")
                assert "error" in refs[0].metadata
            finally:
                await c.close()


# --- fetch with stale ref ------------------------------------------


class TestStaleFetch:
    async def test_unknown_image_returns_empty(self) -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _r: httpx.Response(404))
        ) as client:
            c = OciConnector(OciConfig(references=("alpine",)), client=client)
            try:
                ref = DocumentRef(
                    source_id=c.id,
                    source_kind=c.kind,
                    path="ghost::config",
                    metadata={"image": "ghost", "kind": "config"},
                )
                async for _ in c.fetch(ref):
                    pytest.fail("must yield nothing for unknown image")
            finally:
                await c.close()

    async def test_ref_without_metadata_returns_empty(self) -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _r: httpx.Response(404))
        ) as client:
            c = OciConnector(OciConfig(references=("alpine",)), client=client)
            try:
                ref = DocumentRef(source_id=c.id, source_kind=c.kind, path="x")
                async for _ in c.fetch(ref):
                    pytest.fail("must yield nothing without metadata")
            finally:
                await c.close()


# --- token negotiation flow ----------------------------------------


class TestTokenNegotiation:
    async def test_401_then_200_with_anon_token(self) -> None:
        config_bytes = _config_blob()
        config_digest = "sha256:" + "c" * 64
        manifest_body = {
            "config": {
                "mediaType": _OCI_CONFIG,
                "digest": config_digest,
                "size": len(config_bytes),
            },
            "layers": [],
        }

        call_count = {"manifest": 0, "blob": 0, "token": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/token" in url:
                call_count["token"] += 1
                return httpx.Response(200, json={"token": "anon-token"})
            if "/manifests/v1" in url:
                call_count["manifest"] += 1
                if call_count["manifest"] == 1:
                    return httpx.Response(
                        401,
                        headers={
                            "WWW-Authenticate": (
                                'Bearer realm="https://auth.example/token",'
                                'service="r.example",'
                                'scope="repository:lib/alpine:pull"'
                            )
                        },
                    )
                # After token negotiation, expect Authorization header.
                assert request.headers.get("Authorization", "").startswith("Bearer ")
                return httpx.Response(
                    200,
                    json=manifest_body,
                    headers={"Content-Type": _OCI_MANIFEST},
                )
            if config_digest in url:
                call_count["blob"] += 1
                return httpx.Response(200, content=config_bytes)
            return httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = OciConnector(OciConfig(references=("alpine:v1",)), client=client)
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                assert len(refs) == 1
                assert call_count["token"] == 1
                # Manifest hit twice: once for 401, once with token.
                assert call_count["manifest"] == 2
            finally:
                await c.close()

    async def test_basic_auth_token_path(self) -> None:
        config_bytes = _config_blob()
        config_digest = "sha256:" + "c" * 64
        manifest_body = {
            "config": {
                "mediaType": _OCI_CONFIG,
                "digest": config_digest,
                "size": len(config_bytes),
            },
            "layers": [],
        }

        seen_basic = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal seen_basic
            url = str(request.url)
            if "/token" in url:
                if request.headers.get("Authorization", "").startswith("Basic "):
                    seen_basic = True
                return httpx.Response(200, json={"token": "user-token"})
            if "/manifests/v1" in url:
                if request.headers.get("Authorization", "").startswith("Bearer "):
                    return httpx.Response(
                        200,
                        json=manifest_body,
                        headers={"Content-Type": _OCI_MANIFEST},
                    )
                return httpx.Response(
                    401,
                    headers={
                        "WWW-Authenticate": (
                            'Bearer realm="https://auth.example/token",'
                            'service="r.example",scope="repository:lib/alpine:pull"'
                        )
                    },
                )
            return httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = OciConnector(
                OciConfig(
                    references=("alpine:v1",),
                    username="u",
                    password="p",
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                assert refs
                assert seen_basic
            finally:
                await c.close()

    async def test_static_bearer_token_skips_realm(self) -> None:
        config_bytes = _config_blob()
        config_digest = "sha256:" + "c" * 64
        manifest_body = {
            "config": {
                "mediaType": _OCI_CONFIG,
                "digest": config_digest,
                "size": len(config_bytes),
            },
            "layers": [],
        }

        seen_static = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal seen_static
            url = str(request.url)
            if "/token" in url:
                pytest.fail("static bearer must skip realm exchange")
            if request.headers.get("Authorization") == "Bearer static-tok":
                seen_static = True
            if "/manifests/v1" in url:
                return httpx.Response(
                    200,
                    json=manifest_body,
                    headers={"Content-Type": _OCI_MANIFEST},
                )
            return httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = OciConnector(
                OciConfig(
                    references=("alpine:v1",),
                    bearer_token="static-tok",
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                assert refs
                assert seen_static
            finally:
                await c.close()


# --- spec / factory ------------------------------------------------


class TestSpec:
    def test_metadata(self) -> None:
        assert SPEC.kind == "oci"
        assert SPEC.version == "0.1.0"

    def test_factory_minimal(self) -> None:
        register(SPEC)
        c = create("oci", {"references": ["alpine"]})
        assert isinstance(c, OciConnector)

    def test_factory_full(self) -> None:
        register(SPEC)
        c = create(
            "oci",
            {
                "references": ["ghcr.io/acme/api:v1"],
                "default_platform": "linux/arm64",
                "max_member_bytes": 1024,
                "bearer_token": "tok",
                "id": "x",
            },
        )
        assert c.id == "x"

    def test_factory_user_pass(self) -> None:
        register(SPEC)
        c = create(
            "oci",
            {
                "references": ["alpine"],
                "username": "u",
                "password": "p",
            },
        )
        assert isinstance(c, OciConnector)

    def test_factory_max_member_none(self) -> None:
        register(SPEC)
        c = create(
            "oci",
            {"references": ["alpine"], "max_member_bytes": None},
        )
        assert c._config.max_member_bytes is None

    def test_factory_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="references"):
            SPEC.factory({})


# --- error paths ----------------------------------------------------


class TestErrorPaths:
    async def test_unknown_manifest_media_type_rejected(self) -> None:
        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"unrelated": True},
                headers={"Content-Type": "application/x-bogus"},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = OciConnector(OciConfig(references=("alpine",)), client=client)
            try:
                with pytest.raises(ValueError, match="unexpected manifest media-type"):
                    [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()

    async def test_blob_401_then_retry(self) -> None:
        config_bytes = _config_blob()
        config_digest = "sha256:" + "c" * 64
        manifest_body = {
            "config": {
                "mediaType": _OCI_CONFIG,
                "digest": config_digest,
                "size": len(config_bytes),
            },
            "layers": [],
        }

        blob_call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/token" in url:
                return httpx.Response(200, json={"token": "blob-token"})
            if "/manifests/" in url:
                return httpx.Response(
                    200,
                    json=manifest_body,
                    headers={"Content-Type": _OCI_MANIFEST},
                )
            if "/blobs/" in url:
                blob_call_count["n"] += 1
                if blob_call_count["n"] == 1:
                    return httpx.Response(
                        401,
                        headers={
                            "WWW-Authenticate": (
                                'Bearer realm="https://auth.example/token",'
                                'service="r.example",scope="repository:lib/alpine:pull"'
                            )
                        },
                    )
                return httpx.Response(200, content=config_bytes)
            return httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = OciConnector(OciConfig(references=("alpine:v1",)), client=client)
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                async for d in c.fetch(refs[0]):
                    assert isinstance(d, Document)
                # Blob hit twice (401 then 200 with token).
                assert blob_call_count["n"] == 2
            finally:
                await c.close()

    async def test_challenge_without_realm_rejected(self) -> None:
        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(
                401,
                headers={"WWW-Authenticate": 'Bearer service="r.example"'},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = OciConnector(OciConfig(references=("alpine",)), client=client)
            try:
                with pytest.raises(ValueError, match="no realm parameter"):
                    [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()


# --- close ----------------------------------------------------------


class TestClose:
    async def test_close_owns_client(self) -> None:
        c = OciConnector(OciConfig(references=("alpine",)))
        assert c._owns_client
        await c.close()
        # Idempotent
        await c.close()

    async def test_close_external_client_not_closed(self) -> None:
        client = httpx.AsyncClient()
        c = OciConnector(OciConfig(references=("alpine",)), client=client)
        await c.close()
        # External client survives
        assert not client.is_closed
        await client.aclose()
