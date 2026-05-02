"""Baseline registry for the GiNZA+Presidio honest-baseline measurement (U3).

Provides a uniform `Predictor` interface over 5 variants:

- 3 OSS+Presidio wrapped via `MultiLangSpacyNlpEngine` + `ALL_JA_RECOGNIZERS`:
  `ja_core_news_trf` (transformer), `ja_ginza` (ginza), `ja_core_news_md` (CNN).
- 2 custom variants loaded from `packages/training/output/`:
  `custom_cnn`, `custom_bert`.

Each variant is classified as `score_bearing` (Presidio `RecognizerResult.score`
or per-token softmax available → percentile sweep eligible) or score-less
(`Doc.ents` only → k=100 single-point only). See plan KTD
"Score-bearing vs score-less variants の分類" (P0-5).

Module-top imports are stdlib only — heavy presidio/spacy imports are deferred
into `_build_*` builders so importing `BASELINE_REGISTRY` is cheap and does not
require the `[bench]` extra at module-import time.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

# (start, end, label, score_or_none, rank)
Prediction = tuple[int, int, str, float | None, int]


class Predictor(Protocol):
    def predict(self, text: str) -> list[Prediction]: ...


# Presidio entity name → internal label. ORG passes through; DATE_TIME projects
# to DATE_OF_BIRTH (plan scope target labels: ORGANIZATION + DATE_OF_BIRTH).
PRESIDIO_LABEL_MAP: dict[str, str] = {
    "ORGANIZATION": "ORGANIZATION",
    "DATE_TIME": "DATE_OF_BIRTH",
}

# spaCy `Doc.ents` label → internal label, for custom variants whose NER head
# emits the internal label set directly (PERSON/ORGANIZATION/ADDRESS/
# DATE_OF_BIRTH/BANK_ACCOUNT). Only ORG/DOB are kept downstream.
CUSTOM_LABEL_MAP: dict[str, str] = {
    "ORGANIZATION": "ORGANIZATION",
    "ORG": "ORGANIZATION",
    "DATE_OF_BIRTH": "DATE_OF_BIRTH",
    "DATE": "DATE_OF_BIRTH",
}

TARGET_LABELS: frozenset[str] = frozenset({"ORGANIZATION", "DATE_OF_BIRTH"})


@dataclass(frozen=True)
class BaselineSpec:
    """Static metadata for one baseline variant.

    `score_bearing=True` ⇒ U4 percentile sweep (k ∈ {10,20,30,50,70,90,100}).
    `score_bearing=False` ⇒ k=100 single point only (per plan P0-5).
    `builder` is lazy: heavy model loading happens only when invoked.
    """

    name: str
    category: Literal["oss_presidio", "custom"]
    score_bearing: bool
    builder: Callable[[], Predictor]


# --- helper: pure result→Prediction conversion (testable without presidio) ----


def _results_to_predictions(
    results: list[Any],
    label_map: dict[str, str],
) -> list[Prediction]:
    """Convert duck-typed entity results to `Prediction` tuples.

    Each `result` must expose `.start`, `.end`, `.entity_type`, `.score` (or
    `None`). Filters to `TARGET_LABELS` after applying `label_map`. Rank is
    assigned by document order after a deterministic sort on
    `(start, end, entity_type)` so that two predicts on identical input emit
    identical rank order regardless of upstream container ordering.
    """
    if not results:
        return []
    sorted_results = sorted(
        results,
        key=lambda r: (int(r.start), int(r.end), str(r.entity_type)),
    )
    out: list[Prediction] = []
    rank = 0
    for r in sorted_results:
        mapped = label_map.get(str(r.entity_type))
        if mapped is None or mapped not in TARGET_LABELS:
            continue
        score = getattr(r, "score", None)
        score_f = float(score) if score is not None else None
        out.append((int(r.start), int(r.end), mapped, score_f, rank))
        rank += 1
    return out


# --- OSS+Presidio builders (lazy) --------------------------------------------


def _build_presidio_predictor(spacy_model_name: str) -> Predictor:
    """Load `spacy_model_name`, wrap in MultiLangSpacyNlpEngine + Presidio
    AnalyzerEngine with `ALL_JA_RECOGNIZERS` (mirrors `server/src/app.py`)."""
    import spacy
    from presidio_analyzer import AnalyzerEngine
    from presidio_analyzer.nlp_engine import SpacyNlpEngine

    from pleno_ner_training.recognizers_ja import ALL_JA_RECOGNIZERS

    class MultiLangSpacyNlpEngine(SpacyNlpEngine):
        def __init__(self, models: dict):
            super().__init__()
            self.nlp = models

    try:
        nlp = spacy.load(spacy_model_name)
    except OSError as e:
        raise RuntimeError(
            f"spaCy model {spacy_model_name!r} not found. "
            f"Install via `[bench]` extras or `python -m spacy download {spacy_model_name}`."
        ) from e

    engine = MultiLangSpacyNlpEngine({"ja": nlp})
    analyzer = AnalyzerEngine(nlp_engine=engine, supported_languages=["ja"])
    for recognizer in ALL_JA_RECOGNIZERS:
        analyzer.registry.add_recognizer(recognizer)

    class _PresidioPredictor:
        def predict(self, text: str) -> list[Prediction]:
            if not text:
                return []
            results = analyzer.analyze(
                text=text,
                language="ja",
                entities=["ORGANIZATION", "DATE_TIME"],
            )
            return _results_to_predictions(results, PRESIDIO_LABEL_MAP)

    return _PresidioPredictor()


def _build_ja_core_news_trf() -> Predictor:
    return _build_presidio_predictor("ja_core_news_trf")


def _build_ja_ginza() -> Predictor:
    return _build_presidio_predictor("ja_ginza")


def _build_ja_core_news_md() -> Predictor:
    return _build_presidio_predictor("ja_core_news_md")


# --- custom variant builders (lazy) ------------------------------------------


_TRAINING_OUTPUT = Path(__file__).resolve().parents[3] / "output"


def _resolve_custom_model(subdir: str, friendly: str, train_target: str) -> Path:
    """Locate `<output>/<subdir>/model-best`. Raise a clear error if absent."""
    candidate = _TRAINING_OUTPUT / subdir / "model-best"
    if not candidate.exists():
        raise FileNotFoundError(
            f"{friendly} requires a trained model under "
            f"packages/training/output/{subdir}/model-best; run "
            f"`make {train_target}` first (looked at {candidate})."
        )
    return candidate


def _build_custom_spacy_predictor(model_path: Path) -> Predictor:
    """Load a custom spaCy model and read `Doc.ents` directly. score=None
    because `Doc.ents` carries no per-span confidence."""
    import spacy

    nlp = spacy.load(str(model_path))

    class _CustomPredictor:
        def predict(self, text: str) -> list[Prediction]:
            if not text:
                return []
            doc = nlp(text)

            class _DuckResult:
                __slots__ = ("start", "end", "entity_type", "score")

                def __init__(self, start: int, end: int, label: str) -> None:
                    self.start = start
                    self.end = end
                    self.entity_type = label
                    self.score = None

            results = [
                _DuckResult(ent.start_char, ent.end_char, ent.label_)
                for ent in doc.ents
            ]
            return _results_to_predictions(results, CUSTOM_LABEL_MAP)

    return _CustomPredictor()


def _build_custom_cnn() -> Predictor:
    # Search for a CNN build under output/. Convention: `ja-vNN-cnn` or
    # any subdir whose `model-best/meta.json` declares a tok2vec architecture.
    # We default to the first match; if none, raise.
    candidates = sorted(_TRAINING_OUTPUT.glob("*-cnn/model-best")) + sorted(
        _TRAINING_OUTPUT.glob("ja-v*-cnn/model-best")
    )
    if not candidates:
        # Fall back to a conventional name to keep error message specific.
        raise FileNotFoundError(
            "custom_cnn requires a trained CNN model under "
            "packages/training/output/<latest-cnn-build>/model-best; "
            "run `make train-cnn` first."
        )
    return _build_custom_spacy_predictor(candidates[0])


def _build_custom_bert() -> Predictor:
    # Plan Context names `ja-v02-trf` as the BERT-style transformer build.
    model_path = _resolve_custom_model(
        subdir="ja-v02-trf", friendly="custom_bert", train_target="train-trf"
    )
    return _build_custom_spacy_predictor(model_path)


# --- registry ----------------------------------------------------------------

# Score-bearing classification (plan P0-5):
# - ja_core_news_trf: transformer → Presidio RecognizerResult.score is informative → True
# - ja_ginza:        transformer-backed via ginza → Presidio score available → True
# - ja_core_news_md: small CNN → Doc.ents-driven score is near-uniform → False (conservative)
# - custom_cnn:      Doc.ents has no .score → False
# - custom_bert:     ja-v02-trf model artifacts not yet present in repo and
#                    `Doc.ents` does not expose per-span confidence without an
#                    explicit beam_ner/spancat pipe. Marked False until score
#                    extraction lands; flip to True when the predictor actually
#                    returns non-None scores. Lying to U4 with score_bearing=True
#                    + score=None would corrupt the percentile sweep.
BASELINE_REGISTRY: dict[str, BaselineSpec] = {
    "ja_core_news_trf": BaselineSpec(
        name="ja_core_news_trf",
        category="oss_presidio",
        score_bearing=True,
        builder=_build_ja_core_news_trf,
    ),
    "ja_ginza": BaselineSpec(
        name="ja_ginza",
        category="oss_presidio",
        score_bearing=True,
        builder=_build_ja_ginza,
    ),
    "ja_core_news_md": BaselineSpec(
        name="ja_core_news_md",
        category="oss_presidio",
        score_bearing=False,
        builder=_build_ja_core_news_md,
    ),
    "custom_cnn": BaselineSpec(
        name="custom_cnn",
        category="custom",
        score_bearing=False,
        builder=_build_custom_cnn,
    ),
    "custom_bert": BaselineSpec(
        name="custom_bert",
        category="custom",
        score_bearing=False,
        builder=_build_custom_bert,
    ),
}
