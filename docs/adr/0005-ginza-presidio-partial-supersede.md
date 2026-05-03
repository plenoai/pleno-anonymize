# ADR-0005: GiNZA + Presidio Partial Supersede of ADR-0004

**Status:** Accepted (rule-strict NO_DECISION; operational signal supports custom-side commit. ADR-0004 は ORG/DOB 範囲で **supersede しない**。)
**Date:** 2026-05-02
**Supersedes (partial):** [ADR-0004: Custom Japanese NER Model](./0004-custom-ja-ner-model.md) — ORG/DOB 範囲を提案対象としたが、本 measurement では trigger 不発火

---

## Context

ADR-0004 は `pleno-anonymize` の Japanese NER backbone として CNN + BERT-base の自前訓練 pipeline を採用する決定。v0.12.0 benchmark で precision 33% / F1 49% に留まり、特に `ORGANIZATION` (-63pt) と `DATE_OF_BIRTH` (-45pt) で壊れているように見えていた。

このギャップを埋めるべく、OSS NER stack (`ja_core_news_trf` / `ja_ginza` / `ja_core_news_md` + Presidio) との直接比較 measurement を実施 (origin: [docs/brainstorms/2026-05-02-ginza-presidio-baseline-comparison-requirements.md](../brainstorms/2026-05-02-ginza-presidio-baseline-comparison-requirements.md)、plan: [docs/plans/2026-05-02-001-feat-ginza-presidio-baseline-measurement-plan.md](../plans/2026-05-02-001-feat-ginza-presidio-baseline-measurement-plan.md))。

## Decision

**ORG/DOB 範囲で ADR-0004 を supersede しない。** Pre-Registration locked rule 上 kill 判定が出ておらず、operational signal も custom 側を支持しているため、現行の custom NER backbone を維持する。

採用 OSS variant: **None** — OSS hybrid は本 measurement では正式採用されない。`ja_ginza + Presidio` のみが DOB で matched-precision floor をクリアしたが、custom_cnn が strict precision で同等以上のため切替動機が無い。

`ORGANIZATION` verdict: **NO_DECISION** (rule-strict, held-out v0.13.0)
- `oss_best = None` (3 OSS variants すべて matched-precision floor p ≥ 0.7 を満たさず)
- `custom_best = custom_cnn` (overlap p=1.000 / r=0.875、strict p=1.000 / r=0.875)

`DATE_OF_BIRTH` verdict: **NO_DECISION** (rule-strict, held-out v0.13.0)
- `r7_primary_gate=False`、`r8a_min_span_filter=True`、`r8b_p10_robust=True`、`r8c_dual_metric_agree=True`
- `r7_diff_sign=tied`、`r7_diff_ci=(0.0, 0.0)`、`n_eligible_templates=1`

Operational signal (locked verdict と独立、ADR の trigger ではない):
- ORG: 唯一スコアを返す custom_cnn を採用継続 — OSS hybrid は precision budget を満たさず代替不可
- DOB: custom_cnn (p=1.000 / r=1.000) > ja_ginza (p=0.909 / r=1.000) で +9.1pt strict precision

Artifact: `packages/training/experiments/artifacts/measure-heldout-2026-05-02/comparison.json` (held-out v0.13.0, 80 docs, 3 unseen templates) および `measure-2026-05-02/comparison.json` (in-domain v0.12.1-leak-fixed, 495 docs)

Pre-Registration anchor: plan commit **`fd0cf4e`** / implementation PR #34 merge **`1fc4270`**

## Consequences

- **In-scope (本 ADR で確認):**
  - ORG/DOB の NER backbone は ADR-0004 の custom NER (custom_cnn) を **継続**
  - server image の OSS NER 同梱は不要 (Dockerfile 現状維持)
  - PERSON / ADDRESS / BANK_ACCOUNT は **本 ADR の対象外** — follow-on brainstorm で個別評価 (origin Deferred to Follow-Up Work 参照)
  - 形式依存 entity (PHONE / MY_NUMBER / CARD / EMAIL / PASSPORT / DRIVER_LICENSE / IP_ADDRESS) は ADR-0004 既決のまま (本 ADR 対象外)

- **Operational consequences:**
  - 切替が発生しないため、span overlap arbitration / 2-backbone load の運用課題は本 ADR ではクローズ
  - server image size + cold-start に対する OSS hybrid の影響は本 ADR で評価せず

- **Pre-Registration 整合 (P0-4):**
  本決定は plan PR merge SHA で frozen された verdict-decision-rule に基づく。Locked rule 上は NO_DECISION であり、ADR-0004 supersede の trigger は発火していない。Override (operational COMMIT signal の格上げ) を行う場合は S1 follow-on brainstorm で metric 再設計し、別 ADR (-0006) を起案する (本 ADR は変更しない)。

## Validation

held-out v0.13.0 (80 docs, 3 unseen templates) における主要指標:

| 指標 | ORGANIZATION | DATE_OF_BIRTH |
|---|---|---|
| OSS best variant | None (floor 不通過) | `ja_ginza` |
| OSS best p (overlap) | — | 0.909 |
| OSS best r (overlap) | — | 1.000 |
| custom best variant | `custom_cnn` | `custom_cnn` |
| custom best p (overlap / strict) | 1.000 / 1.000 | 1.000 / 1.000 |
| custom best r (overlap / strict) | 0.875 / 0.875 | 1.000 / 1.000 |
| `mean_diff` (oss − custom) | n/a (oss missing) | 0.000 |
| bootstrap CI 95% | n/a | (0.000, 0.000) |
| `p10_oss` (per-template recall) | n/a | 1.000 |
| `r8c_dual_metric_agree` (overlap vs strict) | true | true |

in-domain v0.12.1-leak-fixed (495 docs) における対応指標:

| 指標 | ORGANIZATION | DATE_OF_BIRTH |
|---|---|---|
| OSS best | None (floor 不通過) | `ja_ginza` (p=0.958 r=1.000) |
| custom best | `custom_cnn` (p=0.919 r=0.919) | `custom_cnn` (p=0.958 r=1.000) |
| `mean_diff` | n/a | 0.000 |
| bootstrap CI 95% | n/a | (0.000, 0.000) |

汎化性 (in-domain → held-out): 崩壊せず — ORG precision +0.081 / recall -0.044、DOB precision +0.042 / recall ±0.000。「訓練 template を丸暗記しているだけ」仮説は実証的に却下。

## Follow-on

- **S1 brainstorm**: primary metric を F1 / matched-precision floor から `recall@FP-budget` に書き換え、operational signal を公式 verdict に取り込めるかを評価。本 ADR を Override する場合は別 ADR (-0006) を起案
- **custom_bert variant**: RunPod 訓練未完。完了後、5-baseline 比較を再実施
- **PERSON / ADDRESS / BANK_ACCOUNT**: 本 ADR scope 外、follow-on brainstorm で個別評価

## References

- Origin brainstorm: [docs/brainstorms/2026-05-02-ginza-presidio-baseline-comparison-requirements.md](../brainstorms/2026-05-02-ginza-presidio-baseline-comparison-requirements.md)
- Implementation plan: [docs/plans/2026-05-02-001-feat-ginza-presidio-baseline-measurement-plan.md](../plans/2026-05-02-001-feat-ginza-presidio-baseline-measurement-plan.md)
- Public benchmark report: [packages/training/docs/benchmark-2026-05-02.md](../../packages/training/docs/benchmark-2026-05-02.md)
- ADR not superseded (this measurement): [docs/adr/0004-custom-ja-ner-model.md](./0004-custom-ja-ner-model.md)
- Related: [docs/adr/0003-spacy-llm-presidio.md](./0003-spacy-llm-presidio.md)

## Status update (2026-05-04): superseded by ADR-0006

Issue #51 で指摘された通り、本 ADR は **rule-strict NO_DECISION × 4** で decisional value が低い。Phase 2 (#48) の NER 再訓練と #69 (measurement triad scores.json) の導入を機に、新計測前提で再 verdict を行う [ADR-0006](./0006-supersede-0005-with-phase2-numbers.md) を起案した。

- 本 ADR は **historical evidence** として保持する (Pre-Registration commitment 毀損回避のため本文は変更しない)
- Phase 1 measurement (held-out v0.13.0 / in-domain v0.12.1-leak-fixed) の数値は本 ADR を一次出典として参照可
- Primary metric は ADR-0006 で strict-span F1 に切替予定。本 ADR の S6 metric (matched-precision-floor recall + token-overlap F1) は補助 metric に降格
