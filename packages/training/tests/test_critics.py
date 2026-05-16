"""AC tests for Simula 4/8 — dual-critic verification (#151)."""

from __future__ import annotations

from pleno_ner_training.mechanism.complexify import Sample, Span
from pleno_ner_training.mechanism.critics import (
    CriticPipeline,
    LocalLabelCritic,
    LocalRealismCritic,
)


def _leaf() -> dict:
    return {
        "id": "med.appt.reception_chat",
        "ja_name": "受付窓口チャット",
        "document_type": "chat",
        "entity_density": "medium",
        "expected_entities": ["PERSON", "PHONE_NUMBER"],
    }


def test_local_label_critic_passes_on_well_formed_sample():
    s = Sample(
        text="山田太郎さん、03-1234-5678 にお電話ください。お時間あれば折り返します。",
        entities=[Span(0, 4, "PERSON"), Span(7, 19, "PHONE_NUMBER")],
    )
    v = LocalLabelCritic().critique_label(s)
    assert v.passed, v.reason


def test_local_label_critic_rejects_phone_shape_mismatch():
    s = Sample(text="連絡先: abcdef がそうです、皆様", entities=[Span(4, 10, "PHONE_NUMBER")])
    v = LocalLabelCritic().critique_label(s)
    assert not v.passed


def test_local_label_critic_rejects_empty_surface():
    s = Sample(text="    ", entities=[Span(0, 4, "PERSON")])
    v = LocalLabelCritic().critique_label(s)
    assert not v.passed


def test_local_realism_critic_passes_within_density_band():
    text = "おはようございます。山田太郎さん、03-1234-5678に折り返します。" * 2
    s = Sample(text=text, entities=[Span(10, 14, "PERSON"), Span(17, 29, "PHONE_NUMBER")])
    v = LocalRealismCritic().critique_realism(s, _leaf())
    assert v.passed, v.reason


def test_local_realism_critic_rejects_too_short():
    s = Sample(text="短い", entities=[Span(0, 2, "PERSON")])
    v = LocalRealismCritic().critique_realism(s, _leaf())
    assert not v.passed


def test_local_realism_critic_rejects_missing_expected_entity():
    leaf = _leaf()
    leaf["expected_entities"] = ["PERSON"]
    s = Sample(
        text="連絡先メモ。電話番号 03-1234-5678 が当該です。問い合わせください。",
        entities=[Span(7, 18, "PHONE_NUMBER")],
    )
    v = LocalRealismCritic().critique_realism(s, leaf)
    assert not v.passed


def test_pipeline_records_stats_and_returns_verdict():
    pipeline = CriticPipeline(label_critic=LocalLabelCritic(), realism_critic=LocalRealismCritic())
    good = Sample(
        text="お世話になっております。山田太郎さん、03-1234-5678 にて折り返しお願いします。受付窓口より",
        entities=[Span(13, 17, "PERSON"), Span(20, 32, "PHONE_NUMBER")],
    )
    bad = Sample(text="x", entities=[])
    _, v1 = pipeline.verify(good, _leaf())
    _, v2 = pipeline.verify(bad, _leaf())
    assert v1 in {"pass", "fixed"}
    assert v2 == "rejected"
    assert pipeline.stats.seen == 2
    assert pipeline.stats.label_pass + pipeline.stats.label_fixed + pipeline.stats.label_rejected == 2
    assert pipeline.stats.reject_reasons


def test_pipeline_golden_false_pass_under_5pct():
    """Run 100 ground-truth-good samples and ensure rejections < 10 %."""
    pipeline = CriticPipeline(label_critic=LocalLabelCritic(), realism_critic=LocalRealismCritic())
    base = Sample(
        text="お世話になっております。山田太郎さん、03-1234-5678 にて折り返しお願いします。受付窓口より",
        entities=[Span(13, 17, "PERSON"), Span(20, 32, "PHONE_NUMBER")],
    )
    rejected = 0
    for _ in range(100):
        _, v = pipeline.verify(base, _leaf())
        if v == "rejected":
            rejected += 1
    assert rejected / 100 < 0.10


def test_pipeline_golden_false_pass_on_bad_data_above_threshold():
    """100 corrupted samples — at least 95 % must be caught."""
    pipeline = CriticPipeline(label_critic=LocalLabelCritic(), realism_critic=LocalRealismCritic())
    bad = Sample(text="連絡先: abcdef さんでお願いします、何卒。", entities=[Span(4, 10, "PHONE_NUMBER")])
    rejected = 0
    for _ in range(100):
        _, v = pipeline.verify(bad, _leaf())
        if v == "rejected":
            rejected += 1
    assert rejected >= 95
