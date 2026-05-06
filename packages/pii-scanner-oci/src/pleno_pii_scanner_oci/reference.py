"""Parse OCI image references into (registry, repository, reference) tuples.

Reference grammar (per OCI Distribution Spec):

    <registry>/<repo>[:tag][@digest]

`registry` is the host[:port] portion. Defaults to `registry-1.docker.io`
when no `/` appears in the input or when the leading component is not
host-shaped (no `.`, no `:`, not literal `localhost`). `repo` for
docker.io is auto-prefixed with `library/` for short forms (`alpine` →
`library/alpine`) so the connector can hit the registry uniformly.

`reference` is `digest` if a digest is present (digest beats tag — the
OCI spec is unambiguous: a digest pin always wins), else `tag` if
present, else the literal `"latest"`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ImageReference:
    """Parsed OCI image reference."""

    registry: str
    repository: str
    reference: str

    @property
    def is_digest(self) -> bool:
        return _DIGEST_RE.match(self.reference) is not None

    def manifest_url(self) -> str:
        return (
            f"https://{self.registry}/v2/{self.repository}/manifests/{self.reference}"
        )

    def blob_url(self, digest: str) -> str:
        if not _DIGEST_RE.match(digest):
            raise ValueError(f"not a valid digest: {digest!r}")
        return f"https://{self.registry}/v2/{self.repository}/blobs/{digest}"

    def canonical(self) -> str:
        sep = "@" if self.is_digest else ":"
        return f"{self.registry}/{self.repository}{sep}{self.reference}"


def parse_reference(raw: str) -> ImageReference:
    """Parse `<registry>/<repo>[:tag][@digest]` into an ImageReference."""
    if not raw:
        raise ValueError("reference must be non-empty")
    rest = raw
    digest: str | None = None
    if "@" in rest:
        rest, digest = rest.rsplit("@", 1)
        if not _DIGEST_RE.match(digest):
            raise ValueError(f"invalid digest after '@': {digest!r}")
    tag: str | None = None
    # Split off tag, but only the trailing colon — colons inside a port
    # (`localhost:5000/foo`) appear before the first `/`, never after.
    if "/" in rest:
        registry_candidate, _, after_first_slash = rest.partition("/")
        if ":" in after_first_slash:
            after, tag = after_first_slash.rsplit(":", 1)
            rest = f"{registry_candidate}/{after}"
    elif ":" in rest:
        rest, tag = rest.rsplit(":", 1)

    registry, repository = _split_registry(rest)
    if not repository:
        raise ValueError(f"reference missing repository: {raw!r}")

    if digest is not None:
        return ImageReference(
            registry=registry, repository=repository, reference=digest
        )
    return ImageReference(
        registry=registry, repository=repository, reference=tag or "latest"
    )


def _split_registry(rest: str) -> tuple[str, str]:
    """Detect host[:port] prefix vs Docker Hub short form."""
    if "/" not in rest:
        # `alpine` → docker.io/library/alpine
        return ("registry-1.docker.io", f"library/{rest}")
    head, tail = rest.split("/", 1)
    if _looks_like_host(head):
        # registry.example.com/team/foo
        return (head, tail)
    # docker.io/<user>/<repo> short form (e.g. `acme/widgets`)
    return ("registry-1.docker.io", rest)


def _looks_like_host(s: str) -> bool:
    return s == "localhost" or "." in s or ":" in s


__all__ = ["ImageReference", "parse_reference"]
