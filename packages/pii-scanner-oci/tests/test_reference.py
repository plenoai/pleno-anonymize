"""Tests for OCI image reference parsing."""

from __future__ import annotations

import pytest

from pleno_pii_scanner_oci.reference import ImageReference, parse_reference


class TestParseReference:
    def test_short_form_docker_hub(self) -> None:
        ref = parse_reference("alpine")
        assert ref.registry == "registry-1.docker.io"
        assert ref.repository == "library/alpine"
        assert ref.reference == "latest"

    def test_short_form_with_tag(self) -> None:
        ref = parse_reference("alpine:3.20")
        assert ref.repository == "library/alpine"
        assert ref.reference == "3.20"

    def test_user_repo_docker_hub(self) -> None:
        ref = parse_reference("acme/widgets")
        assert ref.registry == "registry-1.docker.io"
        assert ref.repository == "acme/widgets"
        assert ref.reference == "latest"

    def test_full_registry(self) -> None:
        ref = parse_reference("ghcr.io/acme/api:v1.2.3")
        assert ref.registry == "ghcr.io"
        assert ref.repository == "acme/api"
        assert ref.reference == "v1.2.3"

    def test_localhost_with_port(self) -> None:
        ref = parse_reference("localhost:5000/foo:bar")
        assert ref.registry == "localhost:5000"
        assert ref.repository == "foo"
        assert ref.reference == "bar"

    def test_digest_pin(self) -> None:
        digest = "sha256:" + "a" * 64
        ref = parse_reference(f"alpine@{digest}")
        assert ref.is_digest
        assert ref.reference == digest

    def test_digest_beats_tag(self) -> None:
        digest = "sha256:" + "b" * 64
        ref = parse_reference(f"alpine:3.20@{digest}")
        assert ref.is_digest
        assert ref.reference == digest

    def test_digest_validation(self) -> None:
        with pytest.raises(ValueError, match="invalid digest"):
            parse_reference("alpine@sha256:short")

    def test_empty_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            parse_reference("")

    def test_canonical_with_tag(self) -> None:
        ref = parse_reference("ghcr.io/acme/api:v1")
        assert ref.canonical() == "ghcr.io/acme/api:v1"

    def test_canonical_with_digest(self) -> None:
        digest = "sha256:" + "c" * 64
        ref = parse_reference(f"ghcr.io/acme/api@{digest}")
        assert ref.canonical() == f"ghcr.io/acme/api@{digest}"

    def test_manifest_url(self) -> None:
        ref = parse_reference("ghcr.io/acme/api:v1")
        assert (
            ref.manifest_url()
            == "https://ghcr.io/v2/acme/api/manifests/v1"
        )

    def test_blob_url(self) -> None:
        ref = parse_reference("ghcr.io/acme/api:v1")
        digest = "sha256:" + "d" * 64
        assert (
            ref.blob_url(digest)
            == f"https://ghcr.io/v2/acme/api/blobs/{digest}"
        )

    def test_blob_url_invalid_digest(self) -> None:
        ref = parse_reference("ghcr.io/acme/api:v1")
        with pytest.raises(ValueError, match="not a valid digest"):
            ref.blob_url("plaintext-not-a-digest")

    def test_missing_repository_after_registry(self) -> None:
        # `ghcr.io/` without anything after should fail.
        with pytest.raises(ValueError, match="missing repository"):
            parse_reference("ghcr.io/")
