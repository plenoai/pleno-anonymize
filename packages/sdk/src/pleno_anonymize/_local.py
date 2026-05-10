"""Local engine — Presidio + spaCy + pleno-recognizers, no network at scan time."""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Iterable

from ._engine import Finding, RedactResult
from ._models import Language, ensure, is_installed, model_for

logger = logging.getLogger("pleno_anonymize")


class LocalEngine:
    """Run Presidio analyzer/anonymizer in-process.

    For each requested language we try to load the matching NER wheel
    (``ja_ner_ja`` / ``en_ner_en``). When the wheel is unavailable and
    ``auto_download`` is False, we fall back to a tokenizer-only blank
    spaCy pipeline — pattern recognizers (regex + checksum) still run, but
    free-text NER classes (``PERSON``, ``ADDRESS``, ``ORGANIZATION`` …)
    will not surface.
    """

    def __init__(
        self,
        *,
        languages: tuple[str, ...] = ("ja",),
        auto_download: bool = True,
    ) -> None:
        if not languages:
            raise ValueError("at least one language must be requested")
        self._languages = tuple(languages)
        self._auto_download = auto_download
        self._analyzer = None
        self._anonymizer = None

    # public API -----------------------------------------------------------

    def analyze(
        self,
        text: str,
        *,
        language: str = "ja",
        entities: Iterable[str] | None = None,
    ) -> list[Finding]:
        if not text:
            return []
        analyzer = self._get_analyzer()
        ent_list = list(entities) if entities is not None else None
        results = analyzer.analyze(text=text, language=language, entities=ent_list)
        return [
            Finding(
                entity_type=r.entity_type,
                start=r.start,
                end=r.end,
                score=float(r.score),
                text=text[r.start : r.end],
            )
            for r in results
        ]

    def redact(
        self,
        text: str,
        *,
        language: str = "ja",
        entities: Iterable[str] | None = None,
        operators: dict[str, dict[str, object]] | None = None,
    ) -> RedactResult:
        from presidio_anonymizer.entities import OperatorConfig

        analyzer = self._get_analyzer()
        ent_list = list(entities) if entities is not None else None
        results = analyzer.analyze(text=text, language=language, entities=ent_list)
        configs: dict[str, OperatorConfig] = {}
        for r in results:
            et = r.entity_type
            if et in configs:
                continue
            user_cfg = operators.get(et) if operators else None
            if user_cfg:
                op_name = str(user_cfg.get("type", "replace"))
                params = {k: v for k, v in user_cfg.items() if k != "type"}
                configs[et] = OperatorConfig(op_name, params)
            else:
                configs[et] = OperatorConfig("replace", {"new_value": f"<{et}>"})
        result = self._get_anonymizer().anonymize(
            text=text, analyzer_results=results, operators=configs
        )
        return RedactResult(text=result.text)

    # warm path ------------------------------------------------------------

    def warmup(self) -> None:
        """Eagerly initialize the analyzer (downloads models if missing)."""
        self._get_analyzer()

    # internal -------------------------------------------------------------

    def _get_analyzer(self):
        if self._analyzer is None:
            self._analyzer = self._build_analyzer()
        return self._analyzer

    def _get_anonymizer(self):
        if self._anonymizer is None:
            from presidio_anonymizer import AnonymizerEngine

            self._anonymizer = AnonymizerEngine()
        return self._anonymizer

    def _build_analyzer(self):
        import spacy
        from presidio_analyzer import AnalyzerEngine
        from presidio_analyzer.nlp_engine import SpacyNlpEngine
        from pleno_recognizers.presidio_adapter import all_ja_presidio

        class _MultiLangSpacyNlpEngine(SpacyNlpEngine):
            def __init__(self, models: dict[str, "spacy.Language"]):
                super().__init__()
                self.nlp = models

        models: dict[str, "spacy.Language"] = {}
        for raw in self._languages:
            if raw not in {"ja", "en"}:
                raise ValueError(f"unsupported language: {raw!r}")
            lang: Language = raw  # type: ignore[assignment]
            model_name = model_for(lang)
            if not is_installed(model_name):
                resolved = ensure(lang, auto_download=self._auto_download)
                if resolved is None:
                    logger.warning(
                        "NER model %s not installed; %s detection falls back to "
                        "blank tokenizer + pattern recognizers only",
                        model_name,
                        raw,
                    )
                    models[raw] = spacy.blank(raw)
                    continue
            models[raw] = spacy.load(model_name)

        engine = _MultiLangSpacyNlpEngine(models)
        analyzer = AnalyzerEngine(
            nlp_engine=engine,
            supported_languages=list(models.keys()),
        )
        for recognizer in all_ja_presidio():
            analyzer.registry.add_recognizer(recognizer)
        if "en" in models:
            analyzer.registry.load_predefined_recognizers(languages=["en"])
        return analyzer


@lru_cache(maxsize=1)
def default_engine() -> LocalEngine:
    """Process-wide default LocalEngine for ergonomic use."""
    return LocalEngine()
