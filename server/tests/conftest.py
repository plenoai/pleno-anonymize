"""共通フィクスチャ."""

import sys
from pathlib import Path

# `recognizers_ja.py` は #74 で `server/src/` に移動した。
# server は Python の正規 package ではなく workspace member なので、
# `from server.src.recognizers_ja import ...` は uv sync 環境でも解決できない。
# `src/` を sys.path に積んで bare import で解決する。
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import pytest
import spacy
from presidio_analyzer import AnalyzerEngine, RecognizerRegistry
from presidio_analyzer.nlp_engine import SpacyNlpEngine

from recognizers_ja import ALL_JA_RECOGNIZERS  # type: ignore[import-not-found]


class _TestNlpEngine(SpacyNlpEngine):
    """テスト用NLPエンジン（ja/en両言語対応）."""

    def __init__(self):
        super().__init__()
        nlp_en = spacy.load("en_core_web_sm")
        self.nlp = {"ja": nlp_en, "en": nlp_en}


@pytest.fixture(scope="session")
def analyzer() -> AnalyzerEngine:
    """パターン認識器付きAnalyzerEngine（ja/en対応）."""
    nlp_engine = _TestNlpEngine()

    registry = RecognizerRegistry(supported_languages=["ja", "en"])
    for recognizer in ALL_JA_RECOGNIZERS:
        registry.add_recognizer(recognizer)

    # Presidio requires at least one recognizer per supported language.
    # Load default English recognizers from Presidio's built-in registry.
    default_registry = RecognizerRegistry(supported_languages=["en"])
    default_registry.load_predefined_recognizers(languages=["en"])
    for recognizer in default_registry.recognizers:
        if "en" in recognizer.supported_language:
            registry.add_recognizer(recognizer)

    return AnalyzerEngine(
        registry=registry,
        nlp_engine=nlp_engine,
        supported_languages=["ja", "en"],
    )
