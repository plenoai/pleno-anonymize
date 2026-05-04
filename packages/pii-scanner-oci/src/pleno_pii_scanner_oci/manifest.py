"""OCI manifest + image-index parsing.

Two manifest shapes coexist in the wild:

  * **Image Manifest** (single platform): top-level `config` blob +
    `layers` array. Used for single-arch images.
  * **Image Index** (multi-platform): top-level `manifests` array of
    descriptors, each with a `platform` selector. Used to ship one
    repo:tag covering linux/amd64, linux/arm64, etc.

The connector resolves an Image Index to a single Image Manifest by
matching the configured `default_platform`. Operators who need to scan
*every* platform can list each digest explicitly in the config.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


_INDEX_TYPES: frozenset[str] = frozenset(
    {
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    }
)

_MANIFEST_TYPES: frozenset[str] = frozenset(
    {
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    }
)


@dataclass(frozen=True, slots=True)
class Descriptor:
    """One entry in a manifest's `layers` or an Image Index's `manifests`."""

    media_type: str
    digest: str
    size: int
    platform: str | None = None  # `os/arch[/variant]`, or None for layers


@dataclass(frozen=True, slots=True)
class ImageManifest:
    """Resolved single-platform manifest."""

    media_type: str
    config: Descriptor
    layers: tuple[Descriptor, ...]


def is_index(media_type: str) -> bool:
    return media_type in _INDEX_TYPES


def is_manifest(media_type: str) -> bool:
    return media_type in _MANIFEST_TYPES


def parse_manifest(media_type: str, body: dict[str, Any]) -> ImageManifest:
    """Parse a single-platform manifest body."""
    if not is_manifest(media_type):
        raise ValueError(f"not an image manifest media-type: {media_type!r}")
    config_raw = body["config"]
    config = Descriptor(
        media_type=str(config_raw["mediaType"]),
        digest=str(config_raw["digest"]),
        size=int(config_raw["size"]),
    )
    layers = tuple(
        Descriptor(
            media_type=str(layer["mediaType"]),
            digest=str(layer["digest"]),
            size=int(layer["size"]),
        )
        for layer in body.get("layers", [])
    )
    return ImageManifest(
        media_type=media_type, config=config, layers=layers
    )


def select_platform(
    body: dict[str, Any], *, default_platform: str
) -> Descriptor:
    """Pick a single-platform descriptor from an Image Index body.

    `default_platform` is `os/arch[/variant]` (e.g. `linux/amd64`).
    Falls back to the first manifest in the list when the requested
    platform is absent — better to scan *something* than to fail
    silently when the operator points at a multi-arch index that
    happens to omit their default.
    """
    manifests = body.get("manifests", [])
    if not manifests:
        raise ValueError("image index has no manifests entry")
    for entry in manifests:
        plat = entry.get("platform", {})
        rendered = _render_platform(plat)
        if rendered == default_platform:
            return Descriptor(
                media_type=str(entry["mediaType"]),
                digest=str(entry["digest"]),
                size=int(entry["size"]),
                platform=rendered,
            )
    fallback = manifests[0]
    return Descriptor(
        media_type=str(fallback["mediaType"]),
        digest=str(fallback["digest"]),
        size=int(fallback["size"]),
        platform=_render_platform(fallback.get("platform", {})),
    )


def _render_platform(plat: dict[str, Any]) -> str:
    os_ = plat.get("os", "")
    arch = plat.get("architecture", "")
    variant = plat.get("variant")
    base = f"{os_}/{arch}"
    return f"{base}/{variant}" if variant else base


__all__ = [
    "Descriptor",
    "ImageManifest",
    "is_index",
    "is_manifest",
    "parse_manifest",
    "select_platform",
]
