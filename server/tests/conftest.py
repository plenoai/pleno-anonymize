"""共通フィクスチャ."""

import pytest
import spacy
from presidio_analyzer import AnalyzerEngine, RecognizerRegistry
from presidio_analyzer.nlp_engine import SpacyNlpEngine

from src.recognizers_ja import ALL_JA_RECOGNIZERS


class _TestNlpEngine(SpacyNlpEngine):
    """en_core_web_smを直接ロードしてja言語として使用するNLPエンジン."""

    def __init__(self):
        super().__init__()
        self.nlp = {"ja": spacy.load("en_core_web_sm")}


@pytest.fixture(scope="session")
def analyzer() -> AnalyzerEngine:
    """パターン認識器付きAnalyzerEngine."""
    nlp_engine = _TestNlpEngine()

    registry = RecognizerRegistry(supported_languages=["ja"])
    for recognizer in ALL_JA_RECOGNIZERS:
        registry.add_recognizer(recognizer)

    return AnalyzerEngine(
        registry=registry,
        nlp_engine=nlp_engine,
        supported_languages=["ja"],
    )
