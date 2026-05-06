"""Schema-version helper — derives a stable cache-busting key from the
current detector pipeline.

Every cached scan result is tagged with a `schema_version`. When the
detector pipeline changes (regex pack release, NER model bump, custom
recognizer added), the schema_version flips and prior cached entries
fall through as misses without needing the operator to manually wipe
the cache file.

The value is intentionally derived from package metadata + caller-
supplied components rather than a hardcoded string. Forgetting to bump
a hardcoded constant when the pipeline changes silently serves stale
findings — the highest-cost failure mode for a security tool.
"""

from __future__ import annotations

from collections.abc import Iterable
from hashlib import sha256
from importlib import metadata


# `_TRACKED_DISTRIBUTIONS` lists every wheel whose version, when bumped,
# may change detector output. Add to this list when a new detector
# component lands; cache entries from older versions then auto-invalidate
# on the next run. Version-not-found is treated as the literal string
# `none` so an editable workspace install (no metadata) still yields a
# stable, reproducible schema_version.
_TRACKED_DISTRIBUTIONS: tuple[str, ...] = (
    "pleno-pii-scanner",
    "pleno-recognizers",
)


def _resolved_versions(distributions: Iterable[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for dist in distributions:
        try:
            version = metadata.version(dist)
        except metadata.PackageNotFoundError:
            version = "none"
        out.append((dist, version))
    return out


def schema_version(*extra_components: str) -> str:
    """Compute the current cache schema version.

    `extra_components` lets callers pin custom recognizer pack revisions,
    NER model checksums, or per-deployment configuration that affects
    detector output. Components are combined deterministically (NUL-
    separated) and SHA-256-hashed so the on-disk cache stores compact
    fixed-length keys regardless of how many inputs the deployment has.
    """
    h = sha256()
    for dist, version in _resolved_versions(_TRACKED_DISTRIBUTIONS):
        h.update(dist.encode())
        h.update(b"=")
        h.update(version.encode())
        h.update(b"\0")
    for component in extra_components:
        h.update(b"x=")
        h.update(component.encode())
        h.update(b"\0")
    return h.hexdigest()[:32]


__all__ = ["schema_version"]
