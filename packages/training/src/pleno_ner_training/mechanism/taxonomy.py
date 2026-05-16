"""Hierarchical taxonomy of Japanese PII scenarios.

Acts as the sampling scaffold for Simula-style Global Diversification.
The seed taxonomy is built deterministically so it can be reviewed,
diff'd, and regenerated. An optional LLM enrichment pass widens the
scaffold further but never replaces a seed scenario.

Schema (per leaf):
    id                slug, unique across the taxonomy
    ja_name           Japanese display name
    domain / sub_domain  hierarchical position
    registers         subset of {formal, polite, casual, terse}
    document_type     one of REGISTERED_DOCUMENT_TYPES
    entity_density    one of {sparse, medium, dense}
    expected_entities subset of canonical pleno entity labels
                      (see pleno_ner_training.entity_types)
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

import yaml

from pleno_ner_training.entity_types import (
    NER_LABELS,
    PATTERN_LABELS,
)

REGISTERS = ("formal", "polite", "casual", "terse")
DOCUMENT_TYPES = (
    "chat",
    "email",
    "form",
    "transcript",
    "ocr_residue",
    "code_comment",
    "doc_export",
    "post",
    "note",
    "voice_memo",
)
ENTITY_DENSITIES = ("sparse", "medium", "dense")

CANONICAL_LABELS = tuple(NER_LABELS + PATTERN_LABELS)


@dataclass(frozen=True)
class Scenario:
    id: str
    ja_name: str
    registers: tuple[str, ...]
    document_type: str
    entity_density: str
    expected_entities: tuple[str, ...]

    def validate(self) -> None:
        if self.document_type not in DOCUMENT_TYPES:
            raise ValueError(f"{self.id}: bad document_type {self.document_type!r}")
        if self.entity_density not in ENTITY_DENSITIES:
            raise ValueError(f"{self.id}: bad entity_density {self.entity_density!r}")
        if not self.registers:
            raise ValueError(f"{self.id}: registers must be non-empty")
        for r in self.registers:
            if r not in REGISTERS:
                raise ValueError(f"{self.id}: bad register {r!r}")
        for e in self.expected_entities:
            if e not in CANONICAL_LABELS:
                raise ValueError(f"{self.id}: unknown entity label {e!r}")


@dataclass(frozen=True)
class SubDomain:
    id: str
    ja_name: str
    scenarios: tuple[Scenario, ...]


@dataclass(frozen=True)
class Domain:
    id: str
    ja_name: str
    sub_domains: tuple[SubDomain, ...]


@dataclass(frozen=True)
class Taxonomy:
    version: str
    language: str
    domains: tuple[Domain, ...] = field(default_factory=tuple)

    def leaves(self) -> Iterable[Scenario]:
        for d in self.domains:
            for sd in d.sub_domains:
                yield from sd.scenarios

    def stats(self) -> dict[str, int]:
        scenarios = list(self.leaves())
        return {
            "domains": len(self.domains),
            "sub_domains": sum(len(d.sub_domains) for d in self.domains),
            "scenarios": len(scenarios),
            "entity_coverage": len({e for s in scenarios for e in s.expected_entities}),
        }


# --------------------------------------------------------------------------
# Seed builder
# --------------------------------------------------------------------------

# Bundles of expected entities, named so domain definitions stay readable.
_E_NAMED = ("PERSON",)
_E_CONTACT = ("PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER")
_E_CONTACT_ADDR = ("PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "ADDRESS", "POSTAL_CODE")
_E_ORG_CONTACT = ("PERSON", "ORGANIZATION", "EMAIL_ADDRESS", "PHONE_NUMBER")
_E_BANK = ("PERSON", "BANK_ACCOUNT", "ORGANIZATION")
_E_CARD = ("PERSON", "CREDIT_CARD", "ADDRESS")
_E_GOV_ID = ("PERSON", "MY_NUMBER", "DATE_OF_BIRTH", "ADDRESS")
_E_INSURANCE = ("PERSON", "HEALTH_INSURANCE", "DATE_OF_BIRTH", "ADDRESS")
_E_MEDICAL = ("PERSON", "DATE_OF_BIRTH", "HEALTH_INSURANCE", "ADDRESS", "PHONE_NUMBER")
_E_TRAVEL = ("PERSON", "PASSPORT", "DATE_OF_BIRTH", "EMAIL_ADDRESS", "PHONE_NUMBER")
_E_DRIVER = ("PERSON", "DRIVER_LICENSE", "ADDRESS", "DATE_OF_BIRTH")
_E_RESIDENCE = ("PERSON", "RESIDENCE_CARD", "DATE_OF_BIRTH", "ADDRESS")
_E_CORPORATE = ("ORGANIZATION", "MY_NUMBER_CORPORATE", "ADDRESS", "PHONE_NUMBER")
_E_URL_ONLY = ("URL", "EMAIL_ADDRESS")
_E_IP = ("IP_ADDRESS", "URL", "EMAIL_ADDRESS")


def _scen(
    sid: str,
    ja: str,
    registers: tuple[str, ...],
    doc_type: str,
    density: str,
    entities: tuple[str, ...],
) -> Scenario:
    return Scenario(
        id=sid,
        ja_name=ja,
        registers=registers,
        document_type=doc_type,
        entity_density=density,
        expected_entities=entities,
    )


def _sub(sid: str, ja: str, scenarios: tuple[Scenario, ...]) -> SubDomain:
    return SubDomain(id=sid, ja_name=ja, scenarios=scenarios)


def _domain(
    did: str, ja: str, sub_domains: tuple[SubDomain, ...]
) -> Domain:
    return Domain(id=did, ja_name=ja, sub_domains=sub_domains)


def build_seed_taxonomy() -> Taxonomy:
    """Return the deterministic seed taxonomy (≥ 30 domains, ≥ 200 scenarios)."""

    domains: list[Domain] = []

    # 1. Medical / 医療
    domains.append(_domain("medical", "医療", (
        _sub("clinical_record", "診療記録", (
            _scen("med.clinical.kanja_chart", "外来カルテ", ("formal",), "doc_export", "dense", _E_MEDICAL),
            _scen("med.clinical.handover_note", "申し送りメモ", ("polite",), "note", "medium", _E_MEDICAL),
            _scen("med.clinical.discharge_summary", "退院サマリー", ("formal",), "doc_export", "dense", _E_MEDICAL),
        )),
        _sub("pharmacy", "調剤", (
            _scen("med.pharmacy.prescription", "処方箋", ("formal",), "form", "medium", _E_MEDICAL),
            _scen("med.pharmacy.medication_history", "お薬手帳", ("polite",), "form", "medium", _E_MEDICAL),
        )),
        _sub("appointments", "予約", (
            _scen("med.appt.reception_chat", "受付窓口チャット", ("polite",), "chat", "sparse", _E_CONTACT),
            _scen("med.appt.reminder_email", "予約リマインドメール", ("polite",), "email", "sparse", _E_CONTACT_ADDR),
        )),
    )))

    # 2. Financial / 金融
    domains.append(_domain("financial", "金融", (
        _sub("retail_banking", "リテール銀行", (
            _scen("fin.bank.transfer_chat", "振込相談チャット", ("polite",), "chat", "medium", _E_BANK),
            _scen("fin.bank.statement", "明細書", ("formal",), "doc_export", "dense", _E_BANK),
            _scen("fin.bank.fraud_alert_email", "不正利用アラートメール", ("formal",), "email", "medium", _E_BANK + ("EMAIL_ADDRESS",)),
        )),
        _sub("cards", "クレジットカード", (
            _scen("fin.card.charge_dispute", "請求異議申立", ("formal",), "email", "medium", _E_CARD),
            _scen("fin.card.welcome_kit", "発行通知書", ("formal",), "doc_export", "dense", _E_CARD + ("PHONE_NUMBER",)),
        )),
        _sub("brokerage", "証券", (
            _scen("fin.brokerage.kyc", "口座開設 KYC", ("formal",), "form", "dense", _E_GOV_ID + ("ORGANIZATION",)),
            _scen("fin.brokerage.trade_confirm", "約定通知", ("formal",), "email", "sparse", ("PERSON", "ORGANIZATION")),
        )),
    )))

    # 3. Government / 行政
    domains.append(_domain("government", "行政", (
        _sub("residence", "住民票・戸籍", (
            _scen("gov.residence.juuminhyou_request", "住民票交付申請", ("formal",), "form", "dense", _E_GOV_ID),
            _scen("gov.residence.change_of_address", "転入届", ("formal",), "form", "dense", _E_GOV_ID),
        )),
        _sub("tax", "税務", (
            _scen("gov.tax.kakutei_shinkoku", "確定申告書", ("formal",), "form", "dense", _E_GOV_ID + ("BANK_ACCOUNT",)),
            _scen("gov.tax.gensenchoshu", "源泉徴収票", ("formal",), "doc_export", "dense", _E_GOV_ID),
        )),
        _sub("social", "社会保障", (
            _scen("gov.social.nenkin_notice", "年金通知", ("formal",), "doc_export", "medium", _E_GOV_ID + ("PHONE_NUMBER",)),
            _scen("gov.social.welfare_intake_call", "生活相談電話メモ", ("polite",), "transcript", "medium", _E_CONTACT_ADDR + ("DATE_OF_BIRTH",)),
        )),
    )))

    # 4. E-commerce / EC
    domains.append(_domain("ecommerce", "EC", (
        _sub("orders", "注文", (
            _scen("ec.orders.order_confirm_email", "注文確認メール", ("polite",), "email", "medium", _E_CONTACT_ADDR + ("CREDIT_CARD",)),
            _scen("ec.orders.delivery_status_chat", "配送状況チャット", ("polite",), "chat", "sparse", _E_CONTACT_ADDR),
        )),
        _sub("returns", "返品交換", (
            _scen("ec.returns.return_request", "返品申請", ("polite",), "form", "medium", _E_CONTACT_ADDR + ("CREDIT_CARD",)),
            _scen("ec.returns.refund_dispute", "返金紛争", ("formal",), "email", "medium", _E_CARD + ("EMAIL_ADDRESS",)),
        )),
        _sub("reviews", "レビュー", (
            _scen("ec.reviews.product_review_post", "商品レビュー投稿", ("casual", "polite"), "post", "sparse", _E_NAMED),
        )),
    )))

    # 5. Customer support / カスタマーサポート
    domains.append(_domain("customer_support", "カスタマーサポート", (
        _sub("inbound", "受電", (
            _scen("cs.inbound.call_transcript", "受電トランスクリプト", ("polite",), "transcript", "dense", _E_CONTACT_ADDR + ("DATE_OF_BIRTH",)),
            _scen("cs.inbound.callback_note", "折り返しメモ", ("terse",), "note", "medium", _E_CONTACT),
        )),
        _sub("ticketing", "チケット", (
            _scen("cs.ticket.zendesk_thread", "Zendesk スレッド", ("polite",), "email", "medium", _E_CONTACT_ADDR),
            _scen("cs.ticket.internal_handoff", "内部引き継ぎ", ("terse",), "chat", "medium", _E_CONTACT),
        )),
    )))

    # 6. HR / 人事
    domains.append(_domain("hr", "人事", (
        _sub("hiring", "採用", (
            _scen("hr.hire.offer_letter", "オファーレター", ("formal",), "doc_export", "dense", _E_CONTACT_ADDR + ("DATE_OF_BIRTH", "BANK_ACCOUNT")),
            _scen("hr.hire.recruiter_email", "リクルーターメール", ("polite",), "email", "medium", _E_ORG_CONTACT),
        )),
        _sub("onboarding", "オンボーディング", (
            _scen("hr.onboard.form_submission", "入社手続きフォーム", ("formal",), "form", "dense", _E_GOV_ID + ("BANK_ACCOUNT", "EMAIL_ADDRESS")),
            _scen("hr.onboard.welcome_email", "歓迎メール", ("polite",), "email", "sparse", _E_CONTACT),
        )),
        _sub("payroll", "給与", (
            _scen("hr.payroll.payslip", "給与明細", ("formal",), "doc_export", "dense", _E_BANK + ("MY_NUMBER",)),
            _scen("hr.payroll.bonus_announce", "賞与通知", ("formal",), "email", "sparse", _E_NAMED + ("BANK_ACCOUNT",)),
        )),
    )))

    # 7. Education / 教育
    domains.append(_domain("education", "教育", (
        _sub("admissions", "入学", (
            _scen("edu.admissions.application", "入学願書", ("formal",), "form", "dense", _E_GOV_ID + ("PHONE_NUMBER", "EMAIL_ADDRESS")),
        )),
        _sub("academic_records", "成績", (
            _scen("edu.records.transcript", "成績証明書", ("formal",), "doc_export", "dense", _E_NAMED + ("DATE_OF_BIRTH", "ADDRESS")),
            _scen("edu.records.parent_email", "保護者メール", ("polite",), "email", "medium", _E_CONTACT_ADDR),
        )),
        _sub("counseling", "進路相談", (
            _scen("edu.counseling.meeting_note", "面談メモ", ("polite",), "note", "medium", _E_CONTACT),
        )),
    )))

    # 8. Social / SNS
    domains.append(_domain("social_sns", "SNS", (
        _sub("microblog", "マイクロブログ", (
            _scen("sns.microblog.life_post", "日常投稿", ("casual",), "post", "sparse", _E_NAMED),
            _scen("sns.microblog.event_announcement", "イベント告知", ("casual", "polite"), "post", "medium", _E_NAMED + ("ADDRESS", "URL")),
        )),
        _sub("dm", "DM", (
            _scen("sns.dm.friend_chat", "友人 DM", ("casual",), "chat", "sparse", _E_NAMED + ("PHONE_NUMBER",)),
            _scen("sns.dm.harassment_report", "ハラスメント報告", ("polite",), "chat", "medium", _E_CONTACT_ADDR),
        )),
        _sub("profile", "プロフィール", (
            _scen("sns.profile.bio", "プロフ紹介", ("casual",), "post", "sparse", _E_NAMED + ("URL", "EMAIL_ADDRESS")),
        )),
    )))

    # 9. Legal / 法律
    domains.append(_domain("legal", "法律", (
        _sub("contracts", "契約書", (
            _scen("legal.contract.nda", "NDA", ("formal",), "doc_export", "dense", _E_NAMED + ("ORGANIZATION", "ADDRESS", "MY_NUMBER_CORPORATE")),
            _scen("legal.contract.employment", "雇用契約", ("formal",), "doc_export", "dense", _E_NAMED + ("ADDRESS", "DATE_OF_BIRTH", "BANK_ACCOUNT")),
        )),
        _sub("litigation", "訴訟", (
            _scen("legal.lit.complaint", "訴状", ("formal",), "doc_export", "dense", _E_NAMED + ("ADDRESS", "DATE_OF_BIRTH")),
            _scen("legal.lit.lawyer_memo", "弁護士メモ", ("terse",), "note", "medium", _E_NAMED + ("ADDRESS", "PHONE_NUMBER")),
        )),
    )))

    # 10. Real estate / 不動産
    domains.append(_domain("real_estate", "不動産", (
        _sub("listings", "物件案内", (
            _scen("re.list.inquiry_email", "問い合わせメール", ("polite",), "email", "medium", _E_CONTACT_ADDR),
        )),
        _sub("contracts", "賃貸契約", (
            _scen("re.contract.lease_agreement", "賃貸借契約書", ("formal",), "doc_export", "dense", _E_NAMED + ("ADDRESS", "DATE_OF_BIRTH", "BANK_ACCOUNT")),
            _scen("re.contract.guarantor_form", "保証人連絡先", ("formal",), "form", "dense", _E_CONTACT_ADDR + ("DATE_OF_BIRTH",)),
        )),
    )))

    # 11. Transportation / 運輸・物流
    domains.append(_domain("transportation", "運輸・物流", (
        _sub("shipping", "配送", (
            _scen("trans.ship.waybill", "送り状", ("formal",), "form", "dense", _E_CONTACT_ADDR),
            _scen("trans.ship.delivery_chat", "配送員チャット", ("casual",), "chat", "medium", _E_CONTACT_ADDR),
        )),
        _sub("ride_share", "配車", (
            _scen("trans.rideshare.pickup_chat", "乗車前チャット", ("casual",), "chat", "sparse", _E_NAMED + ("PHONE_NUMBER",)),
            _scen("trans.rideshare.receipt", "配車レシート", ("formal",), "email", "sparse", _E_NAMED + ("ADDRESS",)),
        )),
    )))

    # 12. Telecom / 通信
    domains.append(_domain("telecom", "通信", (
        _sub("plans", "料金プラン", (
            _scen("telecom.plans.signup_form", "新規契約フォーム", ("formal",), "form", "dense", _E_GOV_ID + ("BANK_ACCOUNT", "EMAIL_ADDRESS", "PHONE_NUMBER")),
            _scen("telecom.plans.churn_call", "解約コール", ("polite",), "transcript", "medium", _E_CONTACT_ADDR),
        )),
        _sub("billing", "請求", (
            _scen("telecom.bill.monthly_invoice", "月次請求", ("formal",), "doc_export", "medium", _E_NAMED + ("ADDRESS", "BANK_ACCOUNT")),
        )),
    )))

    # 13. Family chat / 家族チャット
    domains.append(_domain("family_chat", "家族チャット", (
        _sub("logistics", "予定共有", (
            _scen("fam.logistics.school_pickup", "学校お迎え調整", ("casual",), "chat", "sparse", _E_NAMED + ("ADDRESS",)),
            _scen("fam.logistics.shopping_list", "買い物リスト", ("casual", "terse"), "chat", "sparse", _E_NAMED),
        )),
        _sub("emergency", "緊急連絡", (
            _scen("fam.emergency.hospital_chat", "病院連絡", ("casual",), "chat", "medium", _E_CONTACT_ADDR + ("HEALTH_INSURANCE",)),
        )),
    )))

    # 14. Insurance / 保険
    domains.append(_domain("insurance", "保険", (
        _sub("claims", "請求", (
            _scen("ins.claims.auto_claim", "自動車保険請求", ("formal",), "form", "dense", _E_NAMED + ("ADDRESS", "DATE_OF_BIRTH", "DRIVER_LICENSE")),
            _scen("ins.claims.medical_claim", "医療費請求", ("formal",), "form", "dense", _E_INSURANCE),
        )),
        _sub("underwriting", "引受", (
            _scen("ins.uw.health_questionnaire", "告知書", ("formal",), "form", "dense", _E_INSURANCE),
        )),
    )))

    # 15. Internal docs / 社内文書
    domains.append(_domain("internal_docs", "社内文書", (
        _sub("memos", "稟議・社内通達", (
            _scen("int.memo.ringi", "稟議書", ("formal",), "doc_export", "medium", _E_NAMED + ("ORGANIZATION", "BANK_ACCOUNT")),
            _scen("int.memo.allhands_announce", "全社通達", ("formal",), "email", "sparse", _E_NAMED + ("ORGANIZATION",)),
        )),
        _sub("expense", "経費精算", (
            _scen("int.expense.report", "経費精算書", ("formal",), "form", "medium", _E_NAMED + ("BANK_ACCOUNT", "ADDRESS")),
        )),
    )))

    # 16. Job / resume / 求人・履歴書
    domains.append(_domain("job_resume", "求人・履歴書", (
        _sub("resume", "履歴書", (
            _scen("job.resume.rirekisho", "履歴書", ("formal",), "form", "dense", _E_CONTACT_ADDR + ("DATE_OF_BIRTH",)),
            _scen("job.resume.shokumukeirekisho", "職務経歴書", ("formal",), "doc_export", "dense", _E_NAMED + ("ORGANIZATION", "ADDRESS")),
        )),
        _sub("postings", "求人", (
            _scen("job.posting.scout_dm", "スカウト DM", ("polite",), "email", "medium", _E_ORG_CONTACT),
        )),
    )))

    # 17. Research appendix / 研究論文付録
    domains.append(_domain("research_appendix", "研究論文付録", (
        _sub("acknowledgements", "謝辞・参加者", (
            _scen("res.ack.acknowledge", "謝辞", ("formal",), "doc_export", "sparse", _E_NAMED + ("ORGANIZATION", "EMAIL_ADDRESS")),
            _scen("res.ack.participant_demographics", "被験者属性", ("formal",), "form", "dense", _E_NAMED + ("DATE_OF_BIRTH", "ADDRESS")),
        )),
    )))

    # 18. Official gazette / 官報
    domains.append(_domain("official_gazette", "官報", (
        _sub("notices", "公告", (
            _scen("gaz.notice.merger", "合併公告", ("formal",), "doc_export", "medium", _E_NAMED + ("ORGANIZATION", "ADDRESS", "MY_NUMBER_CORPORATE")),
            _scen("gaz.notice.bankruptcy", "破産公告", ("formal",), "doc_export", "medium", _E_NAMED + ("ADDRESS", "ORGANIZATION")),
        )),
    )))

    # 19. Meeting minutes / 議事録
    domains.append(_domain("meeting_minutes", "議事録", (
        _sub("board", "取締役会", (
            _scen("min.board.full_minutes", "取締役会議事録", ("formal",), "doc_export", "medium", _E_NAMED + ("ORGANIZATION",)),
        )),
        _sub("operational", "オペレーション会議", (
            _scen("min.ops.standup_note", "スタンドアップメモ", ("terse",), "note", "sparse", _E_NAMED + ("URL",)),
            _scen("min.ops.qa_transcript", "Q&A 書き起こし", ("polite",), "transcript", "medium", _E_NAMED + ("EMAIL_ADDRESS",)),
        )),
    )))

    # 20. News report / 報道
    domains.append(_domain("news_report", "報道", (
        _sub("local", "地方ニュース", (
            _scen("news.local.incident", "事件報道", ("formal",), "doc_export", "medium", _E_NAMED + ("ADDRESS", "DATE_OF_BIRTH")),
            _scen("news.local.profile_feature", "人物特集", ("formal",), "doc_export", "medium", _E_NAMED + ("ORGANIZATION", "ADDRESS")),
        )),
    )))

    # 21. EC reviews / レビュー
    domains.append(_domain("ec_reviews", "ECレビュー", (
        _sub("text_only", "テキストレビュー", (
            _scen("ecrev.text.delivery_complaint", "配送クレーム", ("casual",), "post", "medium", _E_CONTACT_ADDR),
        )),
    )))

    # 22. Helpdesk log / ヘルプデスクログ
    domains.append(_domain("helpdesk_log", "ヘルプデスクログ", (
        _sub("it_helpdesk", "IT 問合せ", (
            _scen("help.it.password_reset", "パスワード再設定", ("polite",), "email", "medium", _E_CONTACT + ("IP_ADDRESS",)),
            _scen("help.it.vpn_issue", "VPN 障害", ("terse",), "chat", "medium", _E_CONTACT + ("IP_ADDRESS", "URL")),
        )),
    )))

    # 23. Ringi / 稟議系 (separate from internal docs for fine-grain coverage)
    domains.append(_domain("ringi", "稟議", (
        _sub("approvals", "承認フロー", (
            _scen("ringi.approval.vendor_contract", "ベンダー契約稟議", ("formal",), "doc_export", "medium", _E_CORPORATE + ("PERSON",)),
            _scen("ringi.approval.budget_request", "予算稟議", ("formal",), "doc_export", "medium", _E_NAMED + ("ORGANIZATION",)),
        )),
    )))

    # 24. Invoice / 請求・納品
    domains.append(_domain("invoice", "請求・納品", (
        _sub("b2b", "B2B 請求", (
            _scen("inv.b2b.invoice", "請求書", ("formal",), "doc_export", "dense", _E_NAMED + ("ORGANIZATION", "ADDRESS", "BANK_ACCOUNT", "MY_NUMBER_CORPORATE")),
            _scen("inv.b2b.delivery_note", "納品書", ("formal",), "doc_export", "medium", _E_NAMED + ("ORGANIZATION", "ADDRESS")),
        )),
    )))

    # 25. Delivery log / 配送ログ
    domains.append(_domain("delivery_log", "配送ログ", (
        _sub("courier", "宅配", (
            _scen("del.courier.driver_note", "ドライバーメモ", ("terse",), "note", "medium", _E_NAMED + ("ADDRESS", "PHONE_NUMBER")),
            _scen("del.courier.attempt_log", "再配達ログ", ("terse",), "ocr_residue", "medium", _E_NAMED + ("ADDRESS", "PHONE_NUMBER")),
        )),
    )))

    # 26. School records / 学校
    domains.append(_domain("school_record", "学校記録", (
        _sub("seitokaikiroku", "生徒記録", (
            _scen("sch.seitokai.attendance_register", "出席簿", ("formal",), "form", "dense", _E_NAMED + ("DATE_OF_BIRTH",)),
            _scen("sch.seitokai.teacher_chat", "保護者と教員のチャット", ("polite",), "chat", "medium", _E_CONTACT),
        )),
    )))

    # 27. Utility bill / 公共料金
    domains.append(_domain("utility_bill", "公共料金", (
        _sub("bills", "請求書", (
            _scen("util.bill.electricity", "電気料金通知", ("formal",), "doc_export", "medium", _E_NAMED + ("ADDRESS", "BANK_ACCOUNT")),
            _scen("util.bill.water_late_notice", "水道料金督促", ("formal",), "doc_export", "medium", _E_NAMED + ("ADDRESS",)),
        )),
    )))

    # 28. Travel booking / 旅行予約
    domains.append(_domain("travel_booking", "旅行予約", (
        _sub("airline", "航空券", (
            _scen("trav.air.eticket", "電子航空券", ("formal",), "doc_export", "dense", _E_TRAVEL),
            _scen("trav.air.boarding_email", "搭乗案内メール", ("polite",), "email", "medium", _E_TRAVEL),
        )),
        _sub("hotel", "宿泊", (
            _scen("trav.hotel.reservation_confirm", "宿泊予約確認", ("polite",), "email", "medium", _E_CONTACT_ADDR + ("CREDIT_CARD",)),
            _scen("trav.hotel.checkin_form", "チェックインフォーム", ("formal",), "form", "dense", _E_TRAVEL + ("ADDRESS",)),
        )),
    )))

    # 29. Dating chat / マッチング
    domains.append(_domain("dating_chat", "マッチング", (
        _sub("intro", "初対面", (
            _scen("date.intro.app_chat", "アプリ内チャット", ("casual",), "chat", "sparse", _E_NAMED),
            _scen("date.intro.contact_exchange", "連絡先交換", ("casual",), "chat", "sparse", _E_CONTACT),
        )),
    )))

    # 30. Food delivery / フードデリバリー
    domains.append(_domain("food_delivery", "フードデリバリー", (
        _sub("orders", "注文", (
            _scen("food.order.delivery_note", "配達指示メモ", ("casual", "terse"), "note", "sparse", _E_NAMED + ("ADDRESS", "PHONE_NUMBER")),
            _scen("food.order.complaint_chat", "クレームチャット", ("polite",), "chat", "medium", _E_CONTACT_ADDR),
        )),
    )))

    # 31. Forum Q&A
    domains.append(_domain("forum_qa", "Q&A フォーラム", (
        _sub("tech_qa", "技術 Q&A", (
            _scen("forum.tech.stackoverflow_post", "Stack 系ポスト", ("casual",), "post", "sparse", _E_IP),
            _scen("forum.tech.gist_paste", "Gist 貼り付け", ("terse",), "code_comment", "medium", _E_IP + ("EMAIL_ADDRESS",)),
        )),
        _sub("life_qa", "生活 Q&A", (
            _scen("forum.life.relationship_thread", "恋愛相談スレ", ("casual",), "post", "sparse", _E_NAMED),
        )),
    )))

    # 32. Dev logs / 開発ログ・Slack
    domains.append(_domain("dev_logs", "開発ログ", (
        _sub("chat_ops", "ChatOps", (
            _scen("dev.chat.slack_incident", "Slack インシデント", ("terse",), "chat", "medium", _E_IP + ("EMAIL_ADDRESS",)),
            _scen("dev.chat.pr_review", "PR レビュー", ("terse",), "chat", "sparse", _E_NAMED + ("URL",)),
        )),
        _sub("logs", "ログ抜粋", (
            _scen("dev.logs.access_log", "アクセスログ", ("terse",), "ocr_residue", "dense", _E_IP),
            _scen("dev.logs.error_trace", "スタックトレース", ("terse",), "code_comment", "medium", _E_IP + ("EMAIL_ADDRESS",)),
        )),
    )))

    # 33. Corporate PR / 広報
    domains.append(_domain("corporate_pr", "広報", (
        _sub("press_release", "プレスリリース", (
            _scen("pr.press.product_launch", "新製品", ("formal",), "doc_export", "medium", _E_NAMED + ("ORGANIZATION", "URL")),
            _scen("pr.press.exec_appointment", "役員人事", ("formal",), "doc_export", "medium", _E_NAMED + ("ORGANIZATION",)),
        )),
    )))

    # 34. Foreign residents / 在留関連
    domains.append(_domain("residence_card", "在留", (
        _sub("immigration", "在留手続き", (
            _scen("resi.imm.zairyu_renewal", "在留期間更新申請", ("formal",), "form", "dense", _E_RESIDENCE + ("PHONE_NUMBER",)),
            _scen("resi.imm.work_permit", "資格外活動申請", ("formal",), "form", "dense", _E_RESIDENCE + ("ORGANIZATION",)),
        )),
    )))

    # 35. Mixed orthography corner cases
    domains.append(_domain("orthography_edge", "表記揺れ・OCR残渣", (
        _sub("ocr", "OCR 残渣", (
            _scen("ortho.ocr.shashin_yuusou_fuhyou", "写真送付付票 OCR", ("terse",), "ocr_residue", "medium", _E_CONTACT_ADDR),
            _scen("ortho.ocr.tegaki_memo", "手書きメモ OCR", ("casual",), "ocr_residue", "medium", _E_NAMED + ("PHONE_NUMBER",)),
        )),
        _sub("voice", "音声書き起こし", (
            _scen("ortho.voice.driver_dispatch", "ドライバー無線", ("terse",), "voice_memo", "medium", _E_NAMED + ("ADDRESS",)),
        )),
    )))

    # Local-diversification axis: for every existing leaf, emit register
    # and density variants so the scaffold yields ≥ 200 distinct sampling
    # nodes without inventing weak synonyms. Variants intentionally keep
    # expected_entities fixed — the variation is in surface style only.
    domains = tuple(_expand_variants(d) for d in domains)

    tax = Taxonomy(version="v1.0", language="ja", domains=tuple(domains))
    for s in tax.leaves():
        s.validate()
    return tax


# Variant rules: (suffix, register_override, density_override, doc_type_override)
# `None` keeps the base value. Each rule must change at least one axis.
_VARIANT_RULES: tuple[tuple[str, str | None, str | None, str | None], ...] = (
    ("v_casual", "casual", None, None),
    ("v_terse_dense", "terse", "dense", None),
    ("v_voice", None, None, "voice_memo"),
)

_VOICE_INCOMPATIBLE = {"form", "doc_export", "ocr_residue", "code_comment"}


def _expand_variants(d: Domain) -> Domain:
    """Append register/density/document-type variants to each scenario.

    Idempotent: a scenario whose id already ends in `.v_*` is skipped, so
    re-running the seed builder cannot double-expand.
    """
    new_subs: list[SubDomain] = []
    for sd in d.sub_domains:
        out: list[Scenario] = list(sd.scenarios)
        for base in sd.scenarios:
            if "." in base.id and base.id.rsplit(".", 1)[-1].startswith("v_"):
                continue
            for suffix, reg, dens, dtype in _VARIANT_RULES:
                doc_type = dtype or base.document_type
                if dtype == "voice_memo" and base.document_type in _VOICE_INCOMPATIBLE:
                    continue
                registers = (reg,) if reg else base.registers
                density = dens or base.entity_density
                if (
                    registers == base.registers
                    and density == base.entity_density
                    and doc_type == base.document_type
                ):
                    continue
                out.append(
                    Scenario(
                        id=f"{base.id}.{suffix}",
                        ja_name=base.ja_name,
                        registers=registers,
                        document_type=doc_type,
                        entity_density=density,
                        expected_entities=base.expected_entities,
                    )
                )
        new_subs.append(SubDomain(id=sd.id, ja_name=sd.ja_name, scenarios=tuple(out)))
    return Domain(id=d.id, ja_name=d.ja_name, sub_domains=tuple(new_subs))


# --------------------------------------------------------------------------
# IO
# --------------------------------------------------------------------------

def _scen_to_dict(s: Scenario) -> dict:
    d = asdict(s)
    d["registers"] = list(s.registers)
    d["expected_entities"] = list(s.expected_entities)
    return d


def to_dict(t: Taxonomy) -> dict:
    return {
        "version": t.version,
        "language": t.language,
        "stats": t.stats(),
        "registers": list(REGISTERS),
        "document_types": list(DOCUMENT_TYPES),
        "entity_densities": list(ENTITY_DENSITIES),
        "domains": [
            {
                "id": d.id,
                "ja_name": d.ja_name,
                "sub_domains": [
                    {
                        "id": sd.id,
                        "ja_name": sd.ja_name,
                        "scenarios": [_scen_to_dict(s) for s in sd.scenarios],
                    }
                    for sd in d.sub_domains
                ],
            }
            for d in t.domains
        ],
    }


def save_yaml(t: Taxonomy, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(
            to_dict(t),
            f,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )


def save_json(t: Taxonomy, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(to_dict(t), f, ensure_ascii=False, indent=2)


def load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)
