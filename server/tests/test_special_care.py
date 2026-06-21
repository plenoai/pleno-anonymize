"""SPECIAL_CARE (APPI Art. 2(3)) integration tests.

These tests verify that the NER model detects 要配慮個人情報 in context-dependent
free text. They require a trained model that includes the SPECIAL_CARE labels;
skip until the model is available.
"""

import pytest
from presidio_analyzer import AnalyzerEngine


def _has_special_care_model() -> bool:
    """Check if the NER model supports SPECIAL_CARE labels."""
    try:
        import spacy

        nlp = spacy.load("pleno_anonymize_ja")
        ner = nlp.get_pipe("ner")
        return "MEDICAL_HISTORY" in ner.labels
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _has_special_care_model(),
    reason="NER model with SPECIAL_CARE labels not installed",
)


def _analyze_all(analyzer: AnalyzerEngine, text: str) -> dict[str, list[str]]:
    results = analyzer.analyze(text=text, language="ja")
    out: dict[str, list[str]] = {}
    for r in results:
        out.setdefault(r.entity_type, []).append(text[r.start : r.end])
    return out


class TestMedicalHistory:
    """MEDICAL_HISTORY: disease, treatment, surgery history tied to a person."""

    def test_diagnosis(self, analyzer: AnalyzerEngine):
        text = "患者 山田太郎はうつ病と診断され、2023年より通院中である。"
        entities = _analyze_all(analyzer, text)
        assert "MEDICAL_HISTORY" in entities

    def test_surgery_history(self, analyzer: AnalyzerEngine):
        text = "既往歴: 胃がんの手術歴あり（2020年3月）、高血圧症で投薬中。"
        entities = _analyze_all(analyzer, text)
        assert "MEDICAL_HISTORY" in entities


class TestHealthCheckup:
    """HEALTH_CHECKUP: health checkup / test results tied to a person."""

    def test_blood_test(self, analyzer: AnalyzerEngine):
        text = "佐藤花子様の健康診断結果: HbA1c 7.2%、血圧 152/96mmHg。要精密検査。"
        entities = _analyze_all(analyzer, text)
        assert "HEALTH_CHECKUP" in entities

    def test_general_checkup(self, analyzer: AnalyzerEngine):
        text = "定期健康診断の結果、田中一郎は心電図にて洞性徐脈が認められた。総合判定D。"
        entities = _analyze_all(analyzer, text)
        assert "HEALTH_CHECKUP" in entities


class TestDisability:
    """DISABILITY: physical/mental disability tied to a person."""

    def test_disability_certificate(self, analyzer: AnalyzerEngine):
        text = "鈴木次郎は身体障害者手帳1級（右下肢機能全廃）を所持している。"
        entities = _analyze_all(analyzer, text)
        assert "DISABILITY" in entities

    def test_mental_disability(self, analyzer: AnalyzerEngine):
        text = "申請者の高橋美咲は精神障害者保健福祉手帳2級と認定されている。"
        entities = _analyze_all(analyzer, text)
        assert "DISABILITY" in entities


class TestCriminalRecord:
    """CRIMINAL_RECORD: criminal record tied to a person."""

    def test_conviction(self, analyzer: AnalyzerEngine):
        text = "被告人 渡辺健は窃盗罪で懲役1年6月の判決を受けた。"
        entities = _analyze_all(analyzer, text)
        assert "CRIMINAL_RECORD" in entities

    def test_prior_record(self, analyzer: AnalyzerEngine):
        text = "前科: 傷害罪（2019年、罰金30万円）。現在は執行猶予中。"
        entities = _analyze_all(analyzer, text)
        assert "CRIMINAL_RECORD" in entities


class TestCrimeVictim:
    """CRIME_VICTIM: crime victimization facts tied to a person."""

    def test_assault_victim(self, analyzer: AnalyzerEngine):
        text = "被害者の伊藤明美は暴行事件の被害に遭い、全治3週間の怪我を負った。"
        entities = _analyze_all(analyzer, text)
        assert "CRIME_VICTIM" in entities

    def test_dv_victim(self, analyzer: AnalyzerEngine):
        text = "相談者の中村由美はDV被害を5年間受け続けたと訴えた。"
        entities = _analyze_all(analyzer, text)
        assert "CRIME_VICTIM" in entities


class TestRaceCreedSocialStatus:
    """RACE, CREED, SOCIAL_STATUS: attributes tied to a person."""

    def test_race(self, analyzer: AnalyzerEngine):
        text = "申請者の朴鉄男は在日韓国人3世である。"
        entities = _analyze_all(analyzer, text)
        assert "RACE" in entities

    def test_creed(self, analyzer: AnalyzerEngine):
        text = "従業員の木村太郎はキリスト教プロテスタントを信仰している。"
        entities = _analyze_all(analyzer, text)
        assert "CREED" in entities

    def test_social_status(self, analyzer: AnalyzerEngine):
        text = "相談者は被差別部落出身であることを理由に差別を受けたと申告した。"
        entities = _analyze_all(analyzer, text)
        assert "SOCIAL_STATUS" in entities


class TestHardNegatives:
    """Vocabulary overlap that should NOT be tagged as SPECIAL_CARE."""

    @pytest.mark.parametrize(
        "text",
        [
            "糖尿病は生活習慣病の一つであり、日本人の約1000万人が罹患している。",
            "窃盗罪の構成要件は刑法235条に規定されている。",
            "イスラム教の五行とは信仰告白・礼拝・喜捨・断食・巡礼である。",
            "障害者総合支援法に基づくサービス体系について解説する。",
            "令和5年の犯罪白書によると、刑法犯の認知件数は前年比2.3%増加した。",
        ],
        ids=[
            "medical-general",
            "criminal-law",
            "religion-general",
            "disability-law",
            "crime-stats",
        ],
    )
    def test_general_discussion_not_tagged(self, analyzer: AnalyzerEngine, text: str):
        results = analyzer.analyze(text=text, language="ja")
        special_care_types = {
            "RACE", "CREED", "SOCIAL_STATUS", "MEDICAL_HISTORY",
            "HEALTH_CHECKUP", "DISABILITY", "CRIMINAL_RECORD", "CRIME_VICTIM",
        }
        sc_results = [r for r in results if r.entity_type in special_care_types and r.score >= 0.5]
        assert not sc_results, (
            f"False positive: {[(r.entity_type, text[r.start:r.end], r.score) for r in sc_results]}"
        )
