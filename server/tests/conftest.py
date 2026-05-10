"""共通フィクスチャ."""

import pytest
import spacy
from presidio_analyzer import AnalyzerEngine, RecognizerRegistry
from presidio_analyzer.nlp_engine import SpacyNlpEngine

from pleno_anonymize.recognizers.presidio_adapter import all_ja_presidio


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
    for recognizer in all_ja_presidio():
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
