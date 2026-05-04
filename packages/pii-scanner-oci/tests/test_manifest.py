"""Tests for OCI manifest + image-index parsing."""

from __future__ import annotations

import pytest

from pleno_pii_scanner_oci.manifest import (
    is_index,
    is_manifest,
    parse_manifest,
    select_platform,
)


_OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
_OCI_INDEX = "application/vnd.oci.image.index.v1+json"
_DOCKER_MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"
_DOCKER_LIST = "application/vnd.docker.distribution.manifest.list.v2+json"


class TestMediaTypeChecks:
    @pytest.mark.parametrize("mt", [_OCI_INDEX, _DOCKER_LIST])
    def test_is_index(self, mt: str) -> None:
        assert is_index(mt)

    @pytest.mark.parametrize("mt", [_OCI_MANIFEST, _DOCKER_MANIFEST])
    def test_is_manifest(self, mt: str) -> None:
        assert is_manifest(mt)

    def test_is_manifest_negative(self) -> None:
        assert not is_manifest(_OCI_INDEX)
        assert not is_index(_OCI_MANIFEST)


class TestParseManifest:
    def test_full_manifest(self) -> None:
        body = {
            "config": {
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "digest": "sha256:" + "c" * 64,
                "size": 4096,
            },
            "layers": [
                {
                    "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                    "digest": "sha256:" + "1" * 64,
                    "size": 100_000,
                },
                {
                    "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                    "digest": "sha256:" + "2" * 64,
                    "size": 200_000,
                },
            ],
        }
        manifest = parse_manifest(_OCI_MANIFEST, body)
        assert manifest.config.size == 4096
        assert len(manifest.layers) == 2
        assert manifest.layers[0].size == 100_000

    def test_no_layers_array(self) -> None:
        body = {
            "config": {
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "digest": "sha256:" + "c" * 64,
                "size": 100,
            }
        }
        manifest = parse_manifest(_OCI_MANIFEST, body)
        assert manifest.layers == ()

    def test_rejects_index_media_type(self) -> None:
        with pytest.raises(ValueError, match="not an image manifest"):
            parse_manifest(_OCI_INDEX, {})


class TestSelectPlatform:
    def _index(self, *manifests):
        return {"manifests": list(manifests)}

    def _entry(self, os: str, arch: str, digest: str, variant: str | None = None):
        plat = {"os": os, "architecture": arch}
        if variant:
            plat["variant"] = variant
        return {
            "mediaType": _OCI_MANIFEST,
            "digest": digest,
            "size": 1234,
            "platform": plat,
        }

    def test_picks_matching_platform(self) -> None:
        body = self._index(
            self._entry("linux", "amd64", "sha256:" + "a" * 64),
            self._entry("linux", "arm64", "sha256:" + "b" * 64),
        )
        d = select_platform(body, default_platform="linux/arm64")
        assert d.digest == "sha256:" + "b" * 64
        assert d.platform == "linux/arm64"

    def test_with_variant(self) -> None:
        body = self._index(
            self._entry("linux", "arm", "sha256:" + "a" * 64, variant="v7"),
            self._entry("linux", "arm", "sha256:" + "b" * 64, variant="v6"),
        )
        d = select_platform(body, default_platform="linux/arm/v6")
        assert d.digest == "sha256:" + "b" * 64

    def test_falls_back_to_first(self) -> None:
        body = self._index(
            self._entry("linux", "amd64", "sha256:" + "a" * 64),
        )
        d = select_platform(body, default_platform="linux/arm64")
        # Requested platform absent → fall back to the first manifest.
        assert d.digest == "sha256:" + "a" * 64

    def test_empty_index_rejected(self) -> None:
        with pytest.raises(ValueError, match="no manifests"):
            select_platform({"manifests": []}, default_platform="linux/amd64")

    def test_missing_platform_field(self) -> None:
        body = {
            "manifests": [
                {"mediaType": _OCI_MANIFEST, "digest": "sha256:" + "a" * 64, "size": 1}
            ]
        }
        d = select_platform(body, default_platform="linux/amd64")
        # Renders to empty `os/arch` and fallback to first.
        assert d.platform == "/"
