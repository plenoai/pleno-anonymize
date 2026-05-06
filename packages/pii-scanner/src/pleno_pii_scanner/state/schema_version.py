"""Schema-version helper — caller-driven cache-bust hash.

Every cached scan result is tagged with a `schema_version`. When any
component the caller passes here flips, prior cached entries fall
through as misses without operator intervention.

This is intentionally **not** derived from package versions. A patch
release that fixes a typo or rewrites a comment must not invalidate
every cached entry: the cost of re-scanning a 10**3-repo org because
0.3.0→0.3.1 shipped is much higher than the upside, and the wrong-
output failure mode that auto-version-tracking was meant to defend
against is already covered by the explicit components callers pass
(detector wire version, recognizer pack fingerprint, NER model id, ...).

Callers therefore own the contract: pass every input that influences
detector output. The `pleno_pii_scanner.detector` module exposes the
canonical helper for the built-in pipeline; custom pipelines compose
their own.
"""

from __future__ import annotations

from hashlib import sha256


def schema_version(*components: str) -> str:
    """Deterministic 32-hex-char hash of `components`.

    Components are combined NUL-separated and SHA-256-hashed so the
    on-disk cache stores compact fixed-length keys regardless of how
    many inputs the deployment has. Caller order matters — `(a, b)`
    and `(b, a)` are intentionally distinct schemas, since reordering
    the inputs typically reflects a real semantic change.

    Empty input is permitted and yields a stable, reproducible hash;
    callers that pass no components are explicitly opting out of all
    cache invalidation, which is rarely what they want.
    """
    h = sha256()
    for component in components:
        h.update(component.encode())
        h.update(b"\0")
    return h.hexdigest()[:32]


__all__ = ["schema_version"]
