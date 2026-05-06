"""Default scan path: Presidio + spaCy NER (ja_ner_ja) + regex recognizers.

Mirrors the server's analyzer initialization so a local scan produces the
same set of entities as POST /api/analyze. This is the **default** scan
path — the regex-only fast path lives in regex_pass.py and is reserved
for git-history per-line scanning where NER overhead per short line is
not worth it.

The analyzer is heavy to construct (loads spaCy + Presidio). We init
lazily and cache process-globally.
"""

from __future__ import annotations

import bisect
from pathlib import Path

from pleno_pii_scanner.models import Finding


_analyzer = None

# PyPI は wheel メタデータ内の直接 URL 依存を許可しないため、ja_ner_ja は
# 実行時に取得する。`uvx pleno-pii-scanner` で動かすユースケースを成立させるための
# 苦肉の策で、初回 NER 実行時のみネットワークアクセスが発生する。
_JA_NER_JA_WHEEL = (
    "https://huggingface.co/0xhikae/ja-ner-ja/resolve/main/"
    "ja_ner_ja-0.2.0-py3-none-any.whl"
)


def _load_ja_ner_ja(spacy_module):
    try:
        return spacy_module.load("ja_ner_ja")
    except OSError:
        pass

    import subprocess
    import sys

    print(
        "[pleno-pii-scanner] ja_ner_ja model not found; installing from "
        f"{_JA_NER_JA_WHEEL}",
        flush=True,
    )
    # uvx-managed venvs ship without pip; bootstrap it before installing.
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "--version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        subprocess.check_call([sys.executable, "-m", "ensurepip", "--upgrade"])
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "--quiet", _JA_NER_JA_WHEEL]
    )
    return spacy_module.load("ja_ner_ja")


def _init_analyzer():
    global _analyzer
    if _analyzer is not None:
        return _analyzer

    import spacy
    from presidio_analyzer import AnalyzerEngine
    from presidio_analyzer.nlp_engine import SpacyNlpEngine

    from pleno_recognizers.presidio_adapter import all_ja_presidio

    class _MultiLangSpacyNlpEngine(SpacyNlpEngine):
        def __init__(self, models: dict):
            super().__init__()
            self.nlp = models

    nlp_ja = _load_ja_ner_ja(spacy)
    models = {"ja": nlp_ja}

    # Optional: en model if installed in the venv (used when --language en).
    try:
        models["en"] = spacy.load("en_ner_en")
    except OSError:
        try:
            models["en"] = spacy.load("en_core_web_sm")
        except OSError:
            pass

    engine = _MultiLangSpacyNlpEngine(models)
    analyzer = AnalyzerEngine(
        nlp_engine=engine,
        supported_languages=list(models.keys()),
    )
    for r in all_ja_presidio():
        analyzer.registry.add_recognizer(r)
    if "en" in models:
        analyzer.registry.load_predefined_recognizers(languages=["en"])

    _analyzer = analyzer
    return _analyzer


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


# Sudachi (used by spaCy's ja tokenizer) hard-caps tokenize input at 49,149 bytes.
# Chunk on character boundaries with margin for multi-byte characters; prefer
# splitting on newlines so detections stay aligned with source lines.
_NER_CHUNK_CHAR_LIMIT = 12_000


# Latin-character ratio threshold above which a Japanese-language chunk also
# gets analyzed by the English NER model (issue #102). Chosen empirically:
# pure-Japanese prose sits well below 5% Latin (URLs + technical terms);
# nodejs-ja weekly notes and pep8-ja translation credits both clear 30%+.
_LATIN_PASS_THRESHOLD = 0.10
# Latin entities whose English NER detections supplement ja_ner_ja's misses.
# We deliberately scope this to PERSON — ORG/LOC bring more noise than recall.
_EN_SECONDARY_ENTITIES = frozenset({"PERSON"})


def _latin_ratio(chunk: str) -> float:
    if not chunk:
        return 0.0
    latin = sum(1 for c in chunk if "A" <= c <= "Z" or "a" <= c <= "z")
    return latin / len(chunk)


def _spans_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start < b_end and b_start < a_end


def _chunk_text(text: str):
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


def scan_text(
    text: str,
    file: str,
    *,
    language: str = "ja",
    entities: tuple[str, ...] | None = None,
) -> list[Finding]:
    if not text:
        return []
    analyzer = _init_analyzer()
    line_starts = _line_offsets(text)
    findings: list[Finding] = []
    entity_filter = list(entities) if entities else None
    # English NER is only invoked when (a) we're scanning Japanese text and
    # (b) en_core_web_sm (or en_ner_en) was loadable at init time. The flag
    # is computed once per scan_text call to avoid repeated dict lookups.
    en_secondary_enabled = (
        language == "ja"
        and "en" in getattr(analyzer, "supported_languages", [])
        and (
            entity_filter is None
            or any(e in _EN_SECONDARY_ENTITIES for e in entity_filter)
        )
    )
    en_entity_filter = (
        [e for e in entity_filter if e in _EN_SECONDARY_ENTITIES]
        if entity_filter is not None
        else list(_EN_SECONDARY_ENTITIES)
    )
    for chunk_start, chunk in _chunk_text(text):
        chunk_results = analyzer.analyze(
            text=chunk,
            language=language,
            entities=entity_filter,
        )

        # Issue #102 — Latin-script names slip past ja_ner_ja in
        # Japanese-mixed text. Run the English NER on chunks whose Latin
        # ratio crosses the threshold and merge non-overlapping PERSON
        # detections. Scoped to PERSON to keep precision losses bounded.
        if (
            en_secondary_enabled
            and en_entity_filter
            and _latin_ratio(chunk) >= _LATIN_PASS_THRESHOLD
        ):
            try:
                en_results = analyzer.analyze(
                    text=chunk,
                    language="en",
                    entities=en_entity_filter,
                )
            except Exception:
                # Defensive: if the English pipeline ever errors, the ja
                # detections are still valid — never let Path 1 break the scan.
                en_results = []
            existing_spans = [
                (int(r.start), int(r.end))
                for r in chunk_results
                if str(r.entity_type) in _EN_SECONDARY_ENTITIES
            ]
            for r in en_results:
                rs, re_ = int(r.start), int(r.end)
                if any(_spans_overlap(rs, re_, s, e) for s, e in existing_spans):
                    continue
                chunk_results.append(r)

        for r in chunk_results:
            start = int(r.start) + chunk_start
            end = int(r.end) + chunk_start
            line, col = _line_col(line_starts, start)
            line_end_idx = bisect.bisect_right(line_starts, start)
            line_end = (
                line_starts[line_end_idx]
                if line_end_idx < len(line_starts)
                else len(text)
            )
            snippet = text[line_starts[line - 1] : line_end].rstrip("\n")
            if len(snippet) > 240:
                rel = start - line_starts[line - 1]
                snippet = snippet[max(0, rel - 80) : rel + 160]
            findings.append(
                Finding(
                    entity=str(r.entity_type),
                    file=file,
                    line=line,
                    col=col,
                    score=float(r.score),
                    snippet=snippet,
                    matched=text[start:end],
                    pattern_name="presidio",
                )
            )
    return findings


def scan_files(
    files: list[tuple[Path, Path]],
    file_text: dict[str, str],
    *,
    language: str = "ja",
    entities: tuple[str, ...] | None = None,
) -> list[Finding]:
    """Sequential per-file analyze. Single process so the loaded model is reused.

    Throughput is bounded by spaCy NER (~10–100k chars/sec). For very large
    repos prefer the cloud offload (`--base-url`) which can run on a beefier
    machine, or restrict scope with --include / --exclude.
    """
    if not files:
        return []
    findings: list[Finding] = []
    for rel, _ in files:
        rel_str = rel.as_posix()
        text = file_text.get(rel_str, "")
        findings.extend(scan_text(text, rel_str, language=language, entities=entities))
    return findings
