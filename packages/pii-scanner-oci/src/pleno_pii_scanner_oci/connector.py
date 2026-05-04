"""Daemon-less OCI registry SourceConnector (ADR-0007 §15).

Pipeline per image reference:

  1. GET manifest → if Image Index, resolve to single-platform manifest
     via `default_platform`
  2. GET config blob → emit as the **first** DocumentRef. Per ADR §15:
     `Env`, `Cmd`, `Entrypoint`, and `history` are where 40-60% of
     registry findings live, so we feed them to the detector before any
     layer cost is paid.
  3. For each layer:
       - skip if digest already in the per-run dedup cache (base layers
         are shared across hundreds of images)
       - GET blob → stream-decompress → tarfile member iterator
       - emit each regular-file member as a DocumentRef + Document

Layer dedup is per-connector-instance (one scan run). A long-lived
registry process re-creating the connector per scan correctly drops
the cache between runs — operators who want cross-run dedup can keep
the connector alive between scans.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from pleno_pii_scanner.sources.base import (
    Capabilities,
    Cursor,
    Document,
    DocumentChunk,
    DocumentRef,
    SourceConnector,
    SourceFilter,
)
from pleno_pii_scanner.sources.registry import ConnectorSpec
from pleno_pii_scanner_oci.auth import (
    AnonymousAuth,
    BasicAuth,
    StaticAuth,
    parse_challenge,
)
from pleno_pii_scanner_oci.layers import iter_layer_members
from pleno_pii_scanner_oci.manifest import (
    ImageManifest,
    is_index,
    is_manifest,
    parse_manifest,
    select_platform,
)
from pleno_pii_scanner_oci.reference import ImageReference, parse_reference


_ACCEPT_MANIFEST = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)


# Test seam — production wiring is `httpx.AsyncClient`. Tests inject
# `httpx.MockTransport` doubles via the constructor instead of going
# over the network.
HttpClientFactory = "callable returning httpx.AsyncClient"


@dataclass(frozen=True, slots=True)
class OciConfig:
    """Construction config for `OciConnector`.

    `references` is the list of images to scan, each in the
    `<registry>/<repo>[:tag][@digest]` form. `default_platform` selects
    one variant from each multi-arch image; operators who want every
    arch list each digest separately. `max_member_bytes` caps the body
    size of any single tarball entry (legitimately huge files are rare
    in container images; stripping the long tail saves regex cost).

    `bearer_token` (StaticAuth) is for ECR / pre-issued tokens.
    `username`/`password` use the realm-token flow (Docker Hub PAT,
    GHCR PAT, etc.). When both are absent, anonymous tokens are
    requested — works for public images on every public registry.
    """

    references: tuple[str, ...]
    default_platform: str = "linux/amd64"
    max_member_bytes: int | None = 50 * 1024 * 1024
    bearer_token: str | None = None
    username: str | None = None
    password: str | None = None
    id: str | None = None

    def __post_init__(self) -> None:
        if not self.references:
            raise ValueError("references must be non-empty")
        if (self.username is None) != (self.password is None):
            raise ValueError(
                "username and password must be set together (or both omitted)"
            )

    def resolved_id(self) -> str:
        if self.id is not None:
            return self.id
        # Reference set may be huge; hash deterministically.
        import hashlib

        h = hashlib.sha256()
        for ref in sorted(self.references):
            h.update(ref.encode("utf-8"))
            h.update(b"\0")
        return f"oci:{h.hexdigest()[:16]}"


class OciConnector:
    """Daemon-less OCI registry scanner."""

    kind = "oci"

    def __init__(
        self,
        config: OciConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self.id = config.resolved_id()
        # When a client is injected (tests), we don't own its lifecycle.
        # When we create our own, close() must close it.
        if client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
            self._owns_client = True
        else:
            self._client = client
            self._owns_client = False
        # Per-realm token cache so we don't re-negotiate on every blob
        # fetch. Bounded by the number of distinct (registry, scope)
        # pairs in the config — negligible in practice.
        self._tokens: dict[tuple[str, str], str] = {}
        # Per-run layer-digest dedup cache. Hit count grows linearly
        # with images sharing base layers; the speedup compounds.
        self._scanned_layers: set[str] = set()
        # Cache of resolved (ref → ImageManifest) so fetch() does not
        # re-resolve when the discover pass already did the work.
        self._manifests: dict[str, _ResolvedManifest] = {}

    def capabilities(self) -> Capabilities:
        return Capabilities(
            incremental=False,
            binary=True,
            content_hash_delta=True,
            max_concurrent_fetches=4,
            streaming=True,
        )

    async def discover(
        self,
        filter: SourceFilter,
        cursor: Cursor | None,
    ) -> AsyncIterator[DocumentRef]:
        del cursor  # incremental=False
        for raw_ref in self._config.references:
            try:
                ref = parse_reference(raw_ref)
            except ValueError as exc:
                # Skip malformed refs — surface as a single ref-less
                # warning DocumentRef so operators see the error in the
                # findings dashboard rather than a silent miss.
                yield DocumentRef(
                    source_id=self.id,
                    source_kind=self.kind,
                    path=f"<invalid:{raw_ref}>",
                    content_type="text/plain",
                    metadata={"error": str(exc)},
                )
                continue
            resolved = await self._resolve_manifest(ref)
            self._manifests[ref.canonical()] = resolved

            # Always-first: the config blob (Env / Cmd / Entrypoint /
            # history). Per ADR §15 this is the highest-priority finding
            # source.
            yield DocumentRef(
                source_id=self.id,
                source_kind=self.kind,
                path=f"{ref.canonical()}::config",
                native_url=f"https://{ref.registry}/v2/{ref.repository}/manifests/{ref.reference}",
                content_type="application/json",
                size=resolved.manifest.config.size,
                etag=resolved.manifest.config.digest,
                metadata={
                    "image": ref.canonical(),
                    "platform": resolved.platform or "",
                    "kind": "config",
                },
            )

            for layer in resolved.manifest.layers:
                if layer.digest in self._scanned_layers:
                    continue
                # Mark at yield-time so a second image sharing this base
                # layer (or a re-discover) skips the duplicate ref.
                self._scanned_layers.add(layer.digest)
                yield DocumentRef(
                    source_id=self.id,
                    source_kind=self.kind,
                    path=f"{ref.canonical()}::layer:{layer.digest}",
                    content_type=layer.media_type,
                    size=layer.size,
                    etag=layer.digest,
                    metadata={
                        "image": ref.canonical(),
                        "platform": resolved.platform or "",
                        "kind": "layer",
                        "digest": layer.digest,
                    },
                )

    async def fetch(
        self,
        ref: DocumentRef,
    ) -> AsyncIterator[Document | DocumentChunk]:
        kind_meta = ref.metadata.get("kind")
        image_canonical = ref.metadata.get("image")
        if kind_meta is None or image_canonical is None:
            return
        resolved = self._manifests.get(image_canonical)
        if resolved is None:
            return
        if kind_meta == "config":
            blob = await self._fetch_blob(
                resolved.parsed_ref, resolved.manifest.config.digest
            )
            text = blob.decode("utf-8", errors="replace")
            yield Document(
                ref=ref,
                text=text,
                fetched_at=datetime.now(UTC),
                content_hash=resolved.manifest.config.digest,
                extra={
                    "image": image_canonical,
                    "kind": "config",
                },
            )
            return
        if kind_meta == "layer":
            digest = ref.metadata["digest"]
            self._scanned_layers.add(digest)
            blob = await self._fetch_blob(resolved.parsed_ref, digest)
            for member in iter_layer_members(
                ref.content_type,
                blob,
                max_member_bytes=self._config.max_member_bytes,
            ):
                yield Document(
                    ref=DocumentRef(
                        source_id=ref.source_id,
                        source_kind=ref.source_kind,
                        path=f"{ref.path}#{member.path}",
                        content_type="application/octet-stream",
                        size=member.size,
                        etag=ref.etag,
                        metadata=dict(ref.metadata) | {"member": member.path},
                    ),
                    binary=member.body,
                    fetched_at=datetime.now(UTC),
                    content_hash=digest,
                )

    async def close(self) -> None:
        self._tokens.clear()
        self._scanned_layers.clear()
        self._manifests.clear()
        if self._owns_client:
            await self._client.aclose()

    # --- internals -------------------------------------------------

    async def _resolve_manifest(
        self, ref: ImageReference
    ) -> "_ResolvedManifest":
        manifest_url = ref.manifest_url()
        body, media_type = await self._get_json(
            manifest_url, scope=f"repository:{ref.repository}:pull", ref=ref
        )
        if is_index(media_type):
            descriptor = select_platform(
                body, default_platform=self._config.default_platform
            )
            inner_url = ref.blob_url(descriptor.digest).replace(
                "/blobs/", "/manifests/"
            )
            inner_body, inner_media = await self._get_json(
                inner_url,
                scope=f"repository:{ref.repository}:pull",
                ref=ref,
            )
            return _ResolvedManifest(
                parsed_ref=ref,
                platform=descriptor.platform,
                manifest=parse_manifest(inner_media, inner_body),
            )
        if is_manifest(media_type):
            return _ResolvedManifest(
                parsed_ref=ref,
                platform=None,
                manifest=parse_manifest(media_type, body),
            )
        raise ValueError(
            f"unexpected manifest media-type: {media_type!r} for {ref.canonical()}"
        )

    async def _get_json(
        self, url: str, *, scope: str, ref: ImageReference
    ) -> tuple[dict[str, Any], str]:
        headers = await self._auth_headers(ref.registry, scope, ref=ref)
        headers["Accept"] = _ACCEPT_MANIFEST
        resp = await self._client.get(url, headers=headers)
        if resp.status_code == 401:
            await self._negotiate_token(resp, ref.registry, scope)
            headers = await self._auth_headers(ref.registry, scope, ref=ref)
            headers["Accept"] = _ACCEPT_MANIFEST
            resp = await self._client.get(url, headers=headers)
        resp.raise_for_status()
        media_type = resp.headers.get("Content-Type", "").split(";", 1)[0].strip()
        return resp.json(), media_type

    async def _fetch_blob(
        self, ref: ImageReference, digest: str
    ) -> bytes:
        url = ref.blob_url(digest)
        scope = f"repository:{ref.repository}:pull"
        headers = await self._auth_headers(ref.registry, scope, ref=ref)
        resp = await self._client.get(url, headers=headers, follow_redirects=True)
        if resp.status_code == 401:
            await self._negotiate_token(resp, ref.registry, scope)
            headers = await self._auth_headers(ref.registry, scope, ref=ref)
            resp = await self._client.get(
                url, headers=headers, follow_redirects=True
            )
        resp.raise_for_status()
        return resp.content

    async def _auth_headers(
        self, registry: str, scope: str, *, ref: ImageReference
    ) -> dict[str, str]:
        if self._config.bearer_token is not None:
            return StaticAuth(self._config.bearer_token).headers(scope)
        cached = self._tokens.get((registry, scope))
        if cached is not None:
            return {"Authorization": f"Bearer {cached}"}
        return {}

    async def _negotiate_token(
        self, response: httpx.Response, registry: str, scope: str
    ) -> None:
        """Exchange a 401 for a fresh bearer token via the realm flow."""
        challenge_header = response.headers.get("WWW-Authenticate", "")
        params = parse_challenge(challenge_header)
        realm = params.get("realm")
        service = params.get("service", "")
        # Some registries (Docker Hub on protected repos) include a
        # narrower scope in the challenge; honour it because requesting
        # broader scope returns 403. The cache key stays the original
        # `scope` so subsequent `_auth_headers` lookups hit.
        token_scope = params.get("scope", scope)
        if not realm:
            raise ValueError(
                "challenge from registry has no realm parameter"
            )
        auth: BasicAuth | AnonymousAuth
        if self._config.username is not None and self._config.password is not None:
            auth = BasicAuth(self._config.username, self._config.password)
        else:
            auth = AnonymousAuth()
        token = await auth.fetch_token(self._client, realm, token_scope, service)
        self._tokens[(registry, scope)] = token


@dataclass(frozen=True, slots=True)
class _ResolvedManifest:
    parsed_ref: ImageReference
    platform: str | None
    manifest: ImageManifest


def _factory(config: Mapping[str, Any]) -> SourceConnector:
    refs_raw = config.get("references", ())
    if not refs_raw:
        raise ValueError("oci connector config requires 'references'")
    return OciConnector(
        OciConfig(
            references=tuple(refs_raw),
            default_platform=str(config.get("default_platform", "linux/amd64")),
            max_member_bytes=(
                int(config["max_member_bytes"])
                if config.get("max_member_bytes") is not None
                else None
            ),
            bearer_token=(
                str(config["bearer_token"])
                if config.get("bearer_token") is not None
                else None
            ),
            username=(
                str(config["username"])
                if config.get("username") is not None
                else None
            ),
            password=(
                str(config["password"])
                if config.get("password") is not None
                else None
            ),
            id=str(config["id"]) if config.get("id") is not None else None,
        )
    )


SPEC = ConnectorSpec(
    kind="oci",
    version="0.1.0",
    factory=_factory,
    capabilities=Capabilities(
        incremental=False,
        binary=True,
        content_hash_delta=True,
        max_concurrent_fetches=4,
        streaming=True,
    ),
    required_scopes=("registry:pull",),
    description=(
        "Daemon-less OCI registry scanner. Pulls manifests + layers via "
        "HTTPS only (no /var/run/docker.sock). Image-Index multi-arch "
        "resolution; per-run layer-digest dedup; gzip + zstd streaming. "
        "Config blob (Env/Cmd/Entrypoint/history) is the first document "
        "for each image — that's where 40-60% of registry secrets live."
    ),
)


__all__ = ["SPEC", "OciConfig", "OciConnector"]
