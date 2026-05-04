"""HuggingFace token-classification scan path (#48 / #98).

Opt-in alternative to the default Presidio + spaCy `ja_ner_ja` pipeline.
Loads a transformers token-classification checkpoint and applies a per-label
confidence floor before emitting findings — production rollout of the ORG
precision floor (#98 set ORG=0.99) without forcing every consumer to rewrite
their analyzer.

Selected when env var `PLENO_PII_SCANNER_BACKEND=hf` is set, or when
`scan_files_hf` is called directly.

Per-label thresholds are read from `PLENO_PII_SCANNER_THRESHOLDS`, a
comma-separated list `LABEL=VALUE`. Defaults match the v0.13.0 model card:
`ORGANIZATION=0.99` (others 0.0). Overrideable for non-DLP use cases.

Model source resolution:
1. Env `PLENO_PII_SCANNER_HF_MODEL` (path or HF repo id; pins one model
   regardless of language).
2. Env `PLENO_PII_SCANNER_HF_LANG` (`ja` | `en` | `auto`) selects the
   default model per language. `auto` loads both ja + en and runs both
   scans, merging findings (for codebases that mix the two — common in
   bilingual repos with English code + Japanese comments).
3. Default lang `ja` → `0xhikae/ja-ner-onnx@v0.13.0`.
   Lang `en` → `0xhikae/en-ner-onnx@v0.1.0`.
The HF Hub fetch is cached by huggingface_hub; first run downloads, subsequent
runs are local-only.
"""

from __future__ import annotations

import bisect
import os
from pathlib import Path
from typing import Iterable

from pleno_pii_scanner.models import Finding


# Per-language defaults. Adding a new language is a one-line addition here
# plus a published 0xhikae/<lang>-ner-onnx repo with the same BIO label set.
_LANG_DEFAULTS: dict[str, tuple[str, str]] = {
    "ja": ("0xhikae/ja-ner-onnx", "v0.13.0"),
    "en": ("0xhikae/en-ner-onnx", "v0.1.0"),
}
_DEFAULT_LANG = "ja"
_DEFAULT_LABEL_THRESHOLDS: dict[str, float] = {"ORGANIZATION": 0.99}
_NER_CHUNK_CHAR_LIMIT = 12_000  # mirror ner_pass.py for parity with the spaCy path

# Per-(model, revision) cache so `auto` mode keeps both pipelines warm
# across files within one scan.
_pipeline_cache: dict[tuple[str, str | None], tuple[object, object, dict[int, str]]] = {}


def _parse_thresholds(env_value: str | None) -> dict[str, float]:
    if not env_value:
        return dict(_DEFAULT_LABEL_THRESHOLDS)
    out: dict[str, float] = {}
    for kv in env_value.split(","):
        kv = kv.strip()
        if not kv:
            continue
        if "=" not in kv:
            raise ValueError(
                f"PLENO_PII_SCANNER_THRESHOLDS expects LABEL=VALUE pairs; got {kv!r}"
            )
        k, v = kv.split("=", 1)
        out[k.strip()] = float(v)
    return out


def _resolve_model_sources() -> list[tuple[str, str | None]]:
    """Return a list of (model, revision) pairs to load.

    Single-model unless `PLENO_PII_SCANNER_HF_LANG=auto`. Explicit
    `PLENO_PII_SCANNER_HF_MODEL` always wins and pins a single source
    (auto + explicit-model is silently treated as single-model).
    """
    explicit = os.environ.get("PLENO_PII_SCANNER_HF_MODEL")
    if explicit:
        if Path(explicit).exists():
            return [(explicit, None)]
        return [(explicit, os.environ.get("PLENO_PII_SCANNER_HF_REVISION"))]

    lang = os.environ.get("PLENO_PII_SCANNER_HF_LANG", _DEFAULT_LANG).lower()
    if lang == "auto":
        return [(model, rev) for model, rev in _LANG_DEFAULTS.values()]
    if lang not in _LANG_DEFAULTS:
        raise RuntimeError(
            f"PLENO_PII_SCANNER_HF_LANG={lang!r} not supported. "
            f"Known: {sorted(_LANG_DEFAULTS) + ['auto']}"
        )
    model, revision = _LANG_DEFAULTS[lang]
    explicit_revision = os.environ.get("PLENO_PII_SCANNER_HF_REVISION")
    return [(model, explicit_revision or revision)]


def _load_one_pipeline(model_src: str, revision: str | None):
    """Lazy + cached load of a single HF token-classification model.

    Uses optimum's ONNX Runtime backend (ORTModelForTokenClassification) so
    we can load the published quantized artifact directly — the HF Hub repos
    `0xhikae/{ja,en}-ner-onnx` ship ONNX (`model_quantized.onnx`), not
    safetensors. ONNX is also the canonical inference artifact: ~3× smaller
    INT8 model, ~2× faster CPU inference vs torch.

    Imports are lazy so the default spaCy path doesn't pay the optimum cost.
    """
    cache_key = (model_src, revision)
    if cache_key in _pipeline_cache:
        return _pipeline_cache[cache_key]

    try:
        from optimum.onnxruntime import ORTModelForTokenClassification
        from transformers import AutoTokenizer
    except ImportError as e:
        raise RuntimeError(
            "PLENO_PII_SCANNER_BACKEND=hf requires the [hf] extra. "
            "Install with: uvx --with 'pleno-pii-scanner[hf]' pleno-pii-scanner ..."
        ) from e

    kwargs = {"revision": revision} if revision else {}
    tokenizer = AutoTokenizer.from_pretrained(model_src, **kwargs)
    if not getattr(tokenizer, "is_fast", False):
        raise RuntimeError(
            f"HF model {model_src} must ship a fast tokenizer "
            "(return_offsets_mapping required)."
        )
    # Prefer the INT8-quantized model (smaller, faster); fall back to FP32.
    try:
        model = ORTModelForTokenClassification.from_pretrained(
            model_src, file_name="model_quantized.onnx", **kwargs
        )
    except Exception:
        model = ORTModelForTokenClassification.from_pretrained(model_src, **kwargs)
    id2label = {int(k): v for k, v in model.config.id2label.items()}
    _pipeline_cache[cache_key] = (tokenizer, model, id2label)
    return _pipeline_cache[cache_key]


def _load_pipelines() -> list[tuple[object, object, dict[int, str]]]:
    """Load all configured pipelines (1 for single-lang, 2 for `auto`)."""
    return [_load_one_pipeline(m, r) for m, r in _resolve_model_sources()]


def _decode_spans(
    offsets: list[tuple[int, int]],
    pred_label_ids: list[int],
    token_scores: list[float],
    id2label: dict[int, str],
) -> list[tuple[int, int, str, float]]:
    """BIO decode → (start, end, label, min_token_score) spans.

    Mirrors `predict_hf_with_scores.decode_bio_spans_with_scores` so the spans
    seen by production match the spans evaluated in the v0.12.0 sweep.
    """
    spans: list[tuple[int, int, str, float]] = []
    cur_label: str | None = None
    cur_start: int | None = None
    cur_end: int | None = None
    cur_min_score = 1.0

    def _flush() -> None:
        nonlocal cur_label, cur_start, cur_end, cur_min_score
        if cur_label is not None and cur_start is not None and cur_end is not None:
            spans.append((cur_start, cur_end, cur_label, cur_min_score))
        cur_label = None
        cur_start = None
        cur_end = None
        cur_min_score = 1.0

    for (tok_start, tok_end), label_id, score in zip(offsets, pred_label_ids, token_scores):
        if tok_start == 0 and tok_end == 0:
            _flush()
            continue
        label = id2label[int(label_id)]
        if label == "O":
            _flush()
            continue
        prefix, _, ent = label.partition("-")
        if not ent:
            _flush()
            continue
        if prefix == "B" or cur_label != ent:
            _flush()
            cur_label = ent
            cur_start = int(tok_start)
            cur_end = int(tok_end)
            cur_min_score = float(score)
        else:
            cur_end = int(tok_end)
            cur_min_score = min(cur_min_score, float(score))
    _flush()
    return spans


def _line_offsets(text: str) -> list[int]:
    offsets = [0]
    pos = 0
    while True:
        idx = text.find("\n", pos)
        if idx == -1:
            break
        offsets.append(idx + 1)
        pos = idx + 1
    return offsets


def _line_col(line_starts: list[int], offset: int) -> tuple[int, int]:
    line_idx = bisect.bisect_right(line_starts, offset) - 1
    return line_idx + 1, offset - line_starts[line_idx] + 1


def _chunk_text(text: str) -> Iterable[tuple[int, str]]:
    """Mirror ner_pass._chunk_text so HF and spaCy paths see identical chunks."""
    if len(text) <= _NER_CHUNK_CHAR_LIMIT:
        yield 0, text
        return
    pos = 0
    n = len(text)
    while pos < n:
        end = min(pos + _NER_CHUNK_CHAR_LIMIT, n)
        if end < n:
            newline = text.rfind("\n", pos, end)
            if newline > pos:
                end = newline + 1
        yield pos, text[pos:end]
        pos = end


def _softmax_max_np(logits):
    """Stable per-row softmax → (max prob, argmax) without a torch dep."""
    import numpy as np

    a = np.asarray(logits, dtype=np.float64)
    a -= a.max(axis=-1, keepdims=True)
    np.exp(a, out=a)
    a /= a.sum(axis=-1, keepdims=True)
    label_ids = a.argmax(axis=-1)
    scores = a.max(axis=-1)
    return scores.tolist(), label_ids.tolist()


def scan_text_hf(
    text: str,
    file: str,
    *,
    entities: tuple[str, ...] | None = None,
    label_thresholds: dict[str, float] | None = None,
    max_length: int = 512,
) -> list[Finding]:
    """HF NER pass with per-label confidence floor.

    With `PLENO_PII_SCANNER_HF_LANG=auto`, runs both the ja and en models
    and unions their findings; per-(start, end, label) duplicates are
    de-duplicated by keeping the higher-confidence prediction.
    """
    if not text:
        return []

    pipelines = _load_pipelines()
    thresholds = (
        label_thresholds
        if label_thresholds is not None
        else _parse_thresholds(os.environ.get("PLENO_PII_SCANNER_THRESHOLDS"))
    )
    entity_filter = set(entities) if entities else None
    line_starts = _line_offsets(text)

    # Map (abs_start, abs_end, label) → best Finding (highest score wins) so
    # auto mode doesn't double-count entities both models agree on.
    best: dict[tuple[int, int, str], Finding] = {}

    for tokenizer, model, id2label in pipelines:
        for chunk_start, chunk in _chunk_text(text):
            enc = tokenizer(
                chunk,
                max_length=max_length,
                truncation=True,
                padding=False,
                return_offsets_mapping=True,
                return_tensors="np",
            )
            offsets = [tuple(o) for o in enc.pop("offset_mapping")[0].tolist()]
            outputs = model(**enc)
            logits = outputs.logits[0]
            scores, label_ids = _softmax_max_np(logits)
            spans = _decode_spans(
                offsets=offsets,
                pred_label_ids=label_ids,
                token_scores=scores,
                id2label=id2label,
            )
            for span_start, span_end, label, score in spans:
                if entity_filter is not None and label not in entity_filter:
                    continue
                if score < thresholds.get(label, 0.0):
                    continue
                abs_start = span_start + chunk_start
                abs_end = span_end + chunk_start
                key = (abs_start, abs_end, label)
                prior = best.get(key)
                if prior is not None and prior.score >= score:
                    continue
                line, col = _line_col(line_starts, abs_start)
                line_end_idx = bisect.bisect_right(line_starts, abs_start)
                line_end = (
                    line_starts[line_end_idx]
                    if line_end_idx < len(line_starts)
                    else len(text)
                )
                snippet = text[line_starts[line - 1] : line_end].rstrip("\n")
                if len(snippet) > 240:
                    rel = abs_start - line_starts[line - 1]
                    snippet = snippet[max(0, rel - 80) : rel + 160]
                best[key] = Finding(
                    entity=label,
                    file=file,
                    line=line,
                    col=col,
                    score=float(score),
                    snippet=snippet,
                    matched=text[abs_start:abs_end],
                    pattern_name="hf-ner",
                )

    return list(best.values())


def scan_files_hf(
    files: list[tuple[Path, Path]],
    file_text: dict[str, str],
    *,
    entities: tuple[str, ...] | None = None,
    label_thresholds: dict[str, float] | None = None,
) -> list[Finding]:
    """Sequential per-file HF scan; the model loads once and is reused."""
    if not files:
        return []
    findings: list[Finding] = []
    for rel, _ in files:
        rel_str = rel.as_posix()
        text = file_text.get(rel_str, "")
        findings.extend(
            scan_text_hf(
                text,
                rel_str,
                entities=entities,
                label_thresholds=label_thresholds,
            )
        )
    return findings
