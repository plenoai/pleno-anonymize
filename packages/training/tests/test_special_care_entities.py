"""SPECIAL_CARE (APPI Art. 2(3)) entity definitions and PII filter tests."""

from __future__ import annotations

import pytest

from pleno_ner_training.entity_types import (
    NER_LABELS,
    SPECIAL_CARE_ENTITIES,
    SPECIAL_CARE_LABELS,
    ALL_NER_ENTITIES,
)
from pleno_ner_training.convert_to_hf_dataset import (
    BIO_LABELS,
    ENTITY_LABELS,
    PII_HINT_RE,
)


EXPECTED_SPECIAL_CARE = {
    "RACE",
    "CREED",
    "SOCIAL_STATUS",
    "MEDICAL_HISTORY",
    "HEALTH_CHECKUP",
    "DISABILITY",
    "CRIMINAL_RECORD",
    "CRIME_VICTIM",
}


class TestEntityDefinitions:
    def test_all_subtypes_present(self):
        assert set(SPECIAL_CARE_LABELS) == EXPECTED_SPECIAL_CARE

    def test_special_care_in_ner_labels(self):
        for label in EXPECTED_SPECIAL_CARE:
            assert label in NER_LABELS, f"{label} missing from NER_LABELS"

    def test_bio_labels_include_special_care(self):
        for label in EXPECTED_SPECIAL_CARE:
            assert f"B-{label}" in BIO_LABELS, f"B-{label} missing from BIO_LABELS"
            assert f"I-{label}" in BIO_LABELS, f"I-{label} missing from BIO_LABELS"

    def test_entity_labels_sorted(self):
        assert ENTITY_LABELS == sorted(ENTITY_LABELS)

    def test_special_care_entities_have_examples(self):
        for entity in SPECIAL_CARE_ENTITIES:
            assert len(entity.examples) >= 2, (
                f"{entity.label}: need at least 2 examples"
            )

    def test_no_duplicate_labels(self):
        all_labels = [e.label for e in ALL_NER_ENTITIES]
        assert len(all_labels) == len(set(all_labels))


# PII_HINT_RE positive cases for SPECIAL_CARE keywords
SC_PII_POSITIVES: list[tuple[str, str]] = [
    ("medical/byoureki", "病歴: 高血圧症、2型糖尿病"),
    ("medical/kiohreki", "既往歴として喘息がある"),
    ("medical/shindan", "診断名: うつ病"),
    ("medical/chiryou", "治療歴を確認する"),
    ("disability/techo", "身体障害者手帳を所持"),
    ("disability/chiteki", "知的障害B判定と認定"),
    ("disability/seishin", "精神障害者保健福祉手帳"),
    ("disability/kaigo", "要介護3と認定された"),
    ("checkup/kenshin", "健康診断の結果は異常なし"),
    ("checkup/dock", "人間ドックの結果を報告"),
    ("checkup/seimitsu", "要精密検査と判定された"),
    ("checkup/saiken", "要再検査の通知が届いた"),
    ("criminal/zenka", "前科があることが判明"),
    ("criminal/taiho", "逮捕歴を照会する"),
    ("criminal/kiso", "起訴された事実がある"),
    ("criminal/yuuzai", "有罪判決を受けた"),
    ("criminal/choeki", "懲役2年の実刑を受けた"),
    ("criminal/shikkou", "執行猶予中の身である"),
    ("criminal/shounen", "少年院に送致された"),
    ("victim/higai", "被害届を提出した"),
    ("victim/higai2", "暴行の被害に遭った"),
    ("victim/dv", "DV被害を受けていた"),
    ("victim/stalker", "ストーカー被害を訴えた"),
    ("victim/seihanzai", "性犯罪被害の相談を受けた"),
    ("race/zainichi", "在日コリアンとして育った"),
    ("race/minzoku", "アイヌ民族の伝統を守る"),
    ("race/jinshu", "人種差別を受けた経験"),
    ("race/buraku", "部落出身であることを告白"),
    ("creed/shinkou", "キリスト教の信仰を持つ"),
    ("creed/shuukyou", "宗教は仏教である"),
    ("creed/shinja", "信者として活動している"),
]


@pytest.mark.parametrize(
    "case_id,text", SC_PII_POSITIVES, ids=[c[0] for c in SC_PII_POSITIVES]
)
def test_pii_hint_re_matches_special_care(case_id: str, text: str) -> None:
    assert PII_HINT_RE.search(text), (
        f"[{case_id}] expected PII match in: {text!r}"
    )


# Hard negatives: SPECIAL_CARE vocabulary that is NOT person-specific
SC_PII_NEGATIVES: list[tuple[str, str]] = [
    ("neutral/weather", "明日の天気は晴れのち曇りです。"),
    ("neutral/tech", "データベースのインデックスを再構築する。"),
    ("neutral/cooking", "カレーの隠し味にヨーグルトを加える。"),
]


@pytest.mark.parametrize(
    "case_id,text", SC_PII_NEGATIVES, ids=[c[0] for c in SC_PII_NEGATIVES]
)
def test_pii_hint_re_no_false_positive(case_id: str, text: str) -> None:
    assert not PII_HINT_RE.search(text), (
        f"[{case_id}] unexpected PII match in: {text!r}"
    )
