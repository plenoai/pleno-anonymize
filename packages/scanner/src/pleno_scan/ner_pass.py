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
from typing import Iterable

from pleno_scan.models import Finding


_analyzer = None


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

    nlp_ja = spacy.load("ja_ner_ja")
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
    results = analyzer.analyze(
        text=text,
        language=language,
        entities=list(entities) if entities else None,
    )
    line_starts = _line_offsets(text)
    findings: list[Finding] = []
    for r in results:
        start = int(r.start)
        end = int(r.end)
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
        findings.extend(
            scan_text(text, rel_str, language=language, entities=entities)
        )
    return findings
