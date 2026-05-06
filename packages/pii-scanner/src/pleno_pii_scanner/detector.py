"""Bridge between the detector pipeline and `IncrementalRunner.DetectorFn`.

The IncrementalRunner caches arbitrary `bytes` per document — this
module supplies the canonical wire format and the `(ref, doc)` →
`(count, payload)` callable that the cache wraps.

What runs inside one `DetectorFn` invocation:

  1. `regex_pass.scan_text` — PCRE pack from `pleno-recognizers`.
  2. `ner_pass.scan_text`   — Presidio + spaCy / HF NER (skippable
     for fast-mode scans via `skip_ner=True`).
  3. `verify.verify`        — deterministic checksum + context
     proximity. Pure CPU, no network — safe to cache.

What does NOT run here (and intentionally so):

  * `secret_verifiers/*` liveness checks — those issue live API
    calls (AWS STS, GitHub /user, ...) whose result drifts the moment
    the upstream key is rotated. Caching a "live" verdict would
    silently report revoked keys as live forever; that layer must
    fire on every scan, after the cache replay.

The on-wire format is JSON:

  payload = utf-8(json.dumps([finding_dict, ...]))

with one dict per `models.Finding`. JSON keeps `cache ls`-style
diagnostics human-readable and avoids dragging msgpack into the
core dep set. Round-trip is exact (numbers stay floats, None stays
null) so a cache replay is byte-for-byte indistinguishable from a
fresh detector run.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterable, Sequence
from hashlib import sha256
from typing import Any

from pleno_recognizers.types import PiiRecognizer

from pleno_pii_scanner import ner_pass, regex_pass, verify
from pleno_pii_scanner.models import Finding
from pleno_pii_scanner.sources.base import (
    Document,
    DocumentChunk,
    DocumentRef,
)


# Re-exported as the canonical detector signature the IncrementalRunner
# consumes. Kept as a module-level alias instead of importing the
# Scheduler one so this module stays usable even when the runner is
# constructed lazily.
DetectorFn = Callable[
    [DocumentRef, Document | DocumentChunk],
    Awaitable[tuple[int, bytes]],
]


# Wire-format version — bumped if `_finding_to_dict` changes shape.
# Surfaces in `schema_version()` callers as an extra component so a
# format flip auto-invalidates every cached entry without operator
# action.
DETECTOR_WIRE_VERSION = "1"


# Detector logic version — bumped manually when a code change in the
# regex / NER / verify pipeline alters output for the same inputs
# without affecting the wire format. Reviewers gate this in PR review;
# forgetting to bump it just means the operator has to wipe the cache,
# never that incorrect cached findings get served. (The verify rules
# and ner_pass thresholds are the most likely places to need a bump.)
DETECTOR_LOGIC_VERSION = "1"


def recognizer_pack_fingerprint(
    recognizers: Iterable[PiiRecognizer],
) -> str:
    """Stable 16-hex-char fingerprint of a recognizer pack.

    Captures every field that influences detector output: entity name,
    language, the full pattern list (name + regex + score), and the
    context-keyword tuple. Recognizers are sorted by `(entity, language)`
    so iteration order in `ALL_JA_RECOGNIZERS` does not affect the
    hash. Adding a new recognizer, tweaking a regex, or rotating a
    context keyword all flip the fingerprint and auto-invalidate the
    cache on next scan.
    """
    sorted_recognizers = sorted(recognizers, key=lambda r: (r.entity, r.language))
    h = sha256()
    for r in sorted_recognizers:
        h.update(r.entity.encode())
        h.update(b"\0")
        h.update(r.language.encode())
        h.update(b"\0")
        for p in r.patterns:
            h.update(p.name.encode())
            h.update(b"=")
            h.update(p.regex.encode())
            h.update(b"@")
            h.update(f"{p.score:.6f}".encode())
            h.update(b";")
        h.update(b"\0")
        for kw in r.context:
            h.update(kw.encode())
            h.update(b",")
        h.update(b"\0")
    return h.hexdigest()[:16]


def schema_components(
    recognizers: Iterable[PiiRecognizer],
    *,
    language: str,
    entities: tuple[str, ...] | None,
    skip_ner: bool,
    skip_verify: bool = False,
    extra: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Canonical component tuple for `state.schema_version()`.

    Bundles every input that affects what `make_detector(...)`
    produces:

      * `DETECTOR_WIRE_VERSION` — JSON shape on the cache wire.
      * `DETECTOR_LOGIC_VERSION` — manual code-change marker.
      * `recognizer_pack_fingerprint(...)` — auto-detects regex pack
        edits, recognizer additions, score / context-keyword flips.
      * `language`, `entities`, `skip_ner`, `skip_verify` — operator
        flags that change which findings are emitted.
      * `extra` — caller-supplied (NER model checksum, custom
        recognizer revisions, deployment-specific knobs).

    Pass the result to `state.schema_version(*components)` to derive
    the cache key.
    """
    entity_str = ",".join(entities) if entities else "*"
    return (
        f"detector-wire/{DETECTOR_WIRE_VERSION}",
        f"detector-logic/{DETECTOR_LOGIC_VERSION}",
        f"recognizers/{recognizer_pack_fingerprint(recognizers)}",
        f"lang/{language}",
        f"entities/{entity_str}",
        f"skip_ner/{int(skip_ner)}",
        f"skip_verify/{int(skip_verify)}",
        *extra,
    )


def encode_findings(findings: Sequence[Finding]) -> bytes:
    """Serialize findings to the cache wire format."""
    return json.dumps(
        [_finding_to_dict(f) for f in findings],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def decode_findings(payload: bytes) -> list[Finding]:
    """Reconstruct findings from a cache-stored payload.

    Empty/`b""` payloads (the `(0, b"")` case for documents the
    detector skipped) deserialize to an empty list. Garbage bytes
    raise `ValueError` so a corrupted cache surfaces loudly rather
    than silently dropping findings.
    """
    if not payload:
        return []
    try:
        rows = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        # WHY: `json.loads(bytes)` first sniffs the encoding from the
        # BOM / leading null bytes (RFC 8259 §3); a binary blob that
        # happens to start with NUL surfaces as `UnicodeDecodeError`,
        # not `JSONDecodeError`. Both signal "not valid JSON" and
        # should look identical to the caller.
        raise ValueError(f"detector payload is not valid JSON: {exc}") from None
    if not isinstance(rows, list):
        raise ValueError(
            f"detector payload must encode a JSON array; got {type(rows).__name__}"
        )
    return [_dict_to_finding(d) for d in rows]


def _finding_to_dict(f: Finding) -> dict[str, Any]:
    # Keys mirror `Finding`'s field order so a JSON dump diffs cleanly
    # against the dataclass repr in tests / debug output.
    return {
        "entity": f.entity,
        "file": f.file,
        "line": f.line,
        "col": f.col,
        "score": f.score,
        "snippet": f.snippet,
        "matched": f.matched,
        "pattern_name": f.pattern_name,
        "verification": f.verification,
        "commit": f.commit,
        "author": f.author,
        "date": f.date,
    }


def _dict_to_finding(d: dict[str, Any]) -> Finding:
    return Finding(
        entity=str(d["entity"]),
        file=str(d["file"]),
        line=int(d["line"]),
        col=int(d["col"]),
        score=float(d["score"]),
        snippet=str(d["snippet"]),
        matched=str(d["matched"]),
        pattern_name=str(d["pattern_name"]),
        verification=d.get("verification", "unverified"),
        commit=d.get("commit"),
        author=d.get("author"),
        date=d.get("date"),
    )


def make_detector(
    recognizers: Sequence[PiiRecognizer],
    *,
    language: str = "ja",
    entities: tuple[str, ...] | None = None,
    skip_ner: bool = False,
    skip_verify: bool = False,
) -> DetectorFn:
    """Build a `DetectorFn` that runs the standard regex + NER + verify pipeline.

    `recognizers` is the regex pack — typically
    `pleno_recognizers.ja.ALL_JA_RECOGNIZERS` filtered by entities
    flag. `language` and `entities` are forwarded to `ner_pass.scan_text`.
    `skip_ner` short-circuits the NER pass (regex-only, ~50× faster
    on text-heavy documents). `skip_verify` skips the deterministic
    verifier (use only for benchmarking — production scans should
    leave it on so checksum / context boosts survive cache replay).

    The returned coroutine is reentrant; the caller may run many
    concurrently against the same detector instance. The compiled
    regex pack and Presidio analyzer are both module-level singletons
    in their respective passes, so concurrent invocations share work
    rather than re-initializing.
    """
    patterns = regex_pass.compile_patterns(recognizers)
    recognizers_tuple = tuple(recognizers)

    async def detector(
        ref: DocumentRef, doc: Document | DocumentChunk
    ) -> tuple[int, bytes]:
        text = _doc_text(doc)
        if text is None or not text:
            return (0, b"")
        regex_findings = regex_pass.scan_text(text, ref.path, patterns)
        ner_findings: list[Finding] = []
        if not skip_ner:
            ner_findings = ner_pass.scan_text(
                text, ref.path, language=language, entities=entities
            )
        all_findings: list[Finding] = list(regex_findings) + list(ner_findings)
        if not skip_verify and all_findings:
            all_findings = verify.verify(
                all_findings,
                recognizers_tuple,
                file_text_for={ref.path: text},
            )
        return (len(all_findings), encode_findings(all_findings))

    return detector


def _doc_text(doc: Document | DocumentChunk) -> str | None:
    """Return UTF-8 text from a Document/DocumentChunk, or None.

    Both types satisfy the (text XOR binary) invariant from
    `sources.base`; we prefer text and fall back to a forgiving
    UTF-8 decode of the binary payload so connectors that emit
    `binary=` still get their content scanned. Encoding sniffing
    is the ContentExtractor's job, not the detector's — for binary
    that does not decode meaningfully, we return None so the runner
    counts a miss with no findings.
    """
    if doc.text is not None:
        return doc.text
    if doc.binary is not None:
        try:
            return doc.binary.decode("utf-8", errors="replace")
        except Exception:
            return None
    return None


__all__ = [
    "DETECTOR_LOGIC_VERSION",
    "DETECTOR_WIRE_VERSION",
    "DetectorFn",
    "decode_findings",
    "encode_findings",
    "make_detector",
    "recognizer_pack_fingerprint",
    "schema_components",
]
