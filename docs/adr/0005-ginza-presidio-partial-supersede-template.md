# ADR-0005: GiNZA + Presidio Partial Supersede of ADR-0004 (Template — Draft)

**Status:** Draft (本 plan 完了後 ADR を起案、measurement 結果記入で Accepted)
**Date:** 2026-05-02
**Supersedes (partial):** [ADR-0004: Custom Japanese NER Model](./0004-custom-ja-ner-model.md) — ORG/DOB 範囲のみ

---

## Context

ADR-0004 は `pleno-anonymize` の Japanese NER backbone として CNN + BERT-base の自前訓練 pipeline を採用する決定。v0.12.0 benchmark で precision 33% / F1 49% に留まり、特に `ORGANIZATION` (-63pt) と `DATE_OF_BIRTH` (-45pt) で壊れている。

このギャップを埋めるべく、OSS NER stack (`ja_core_news_trf` / `ja_ginza` / `ja_core_news_md` + Presidio) との直接比較 measurement を実施 (origin: [docs/brainstorms/2026-05-02-ginza-presidio-baseline-comparison-requirements.md](../brainstorms/2026-05-02-ginza-presidio-baseline-comparison-requirements.md)、plan: [docs/plans/2026-05-02-001-feat-ginza-presidio-baseline-measurement-plan.md](../plans/2026-05-02-001-feat-ginza-presidio-baseline-measurement-plan.md))。

## Decision

**ORG/DOB 範囲のみ ADR-0004 を supersede する。**

<!-- 結果記入: 採用 OSS variant 名 -->
<!-- 例: 「`ja_core_news_trf` + Presidio (k=50 percentile slice)」 -->
採用 OSS variant: **[TBD — measurement 結果から記入]**

<!-- 結果記入: ORG verdict -->
`ORGANIZATION` verdict: **[TBD — KILL | COMMIT | NO_DECISION]**

<!-- 結果記入: DOB verdict -->
`DATE_OF_BIRTH` verdict: **[TBD — KILL | COMMIT | NO_DECISION]**

<!-- 結果記入: artifact reference -->
Artifact: `packages/training/experiments/artifacts/<run_id>/comparison.json`

<!-- 結果記入: anchor SHA -->
Pre-Registration anchor PR SHA: **[TBD — plan PR の merge SHA]**

## Consequences

- **In-scope (本 ADR で更新):**
  - ORG/DOB の NER backbone 切替 (kill 経路時): server 側 `MultiLangSpacyNlpEngine` を OSS+Presidio variant でラップ
  - PERSON / ADDRESS / BANK_ACCOUNT は **本 ADR の対象外** — follow-on brainstorm で個別評価 (origin Deferred to Follow-Up Work 参照)
  - 形式依存 entity (PHONE / MY_NUMBER / CARD / EMAIL / PASSPORT / DRIVER_LICENSE / IP_ADDRESS) は ADR-0004 既決のまま (本 ADR 対象外)

- **Operational consequences (kill 経路時):**
  <!-- 結果記入: partial-kill になった場合は 2 backbone 同時 load の運用方針を記述 -->
  - [TBD if partial-kill: span overlap arbitration policy]
  - [TBD if partial-kill: server image size + cold-start impact]

- **Pre-Registration 整合 (P0-4):**
  本決定は plan PR merge SHA で frozen された verdict-decision-rule に基づく。Override がある場合は `experiments/log.jsonl` audit trail への記録が必須。

## Validation

<!-- 結果記入: bootstrap CI / Bonferroni 補正下での主要指標 -->
- matched-precision recall (precision ≥ 0.7 budget) at best operating point:
  - ORGANIZATION OSS best: **[TBD]** (95% CI [TBD, TBD])
  - ORGANIZATION custom best: **[TBD]** (95% CI [TBD, TBD])
  - DATE_OF_BIRTH OSS best: **[TBD]** (95% CI [TBD, TBD])
  - DATE_OF_BIRTH custom best: **[TBD]** (95% CI [TBD, TBD])
- p10 of per-template recall (R8b robustness gate): **[TBD]**
- token-overlap F1 と strict-span F1 の verdict 一致 (R8c): **[TBD]**

## References

- Origin brainstorm: [docs/brainstorms/2026-05-02-ginza-presidio-baseline-comparison-requirements.md](../brainstorms/2026-05-02-ginza-presidio-baseline-comparison-requirements.md)
- Implementation plan: [docs/plans/2026-05-02-001-feat-ginza-presidio-baseline-measurement-plan.md](../plans/2026-05-02-001-feat-ginza-presidio-baseline-measurement-plan.md)
- ADR being partial-superseded: [docs/adr/0004-custom-ja-ner-model.md](./0004-custom-ja-ner-model.md)
- Related: [docs/adr/0003-spacy-llm-presidio.md](./0003-spacy-llm-presidio.md)
