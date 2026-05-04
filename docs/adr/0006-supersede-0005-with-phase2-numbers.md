# ADR-0006: Supersede ADR-0005 with Phase 2 Measurement Triad

**Status:** Proposed (#48 first iteration ran 2026-05-03, AC not met; Accepted 昇格は ORG-FP 改善イテレーション後)
**Date:** 2026-05-04 (Phase 2 numbers added 2026-05-03)
**Supersedes:** [ADR-0005: GiNZA + Presidio Partial Supersede of ADR-0004](./0005-ginza-presidio-partial-supersede.md)

---

## Context

ADR-0005 は **rule-strict NO_DECISION × 4** (ORG held-out / ORG in-domain / DOB held-out / DOB in-domain) の状態で Accepted された。Issue #51 が指摘する通り、これは `OSS との比較で勝った負けたが言えない` 情報量の薄い verdict であり、ADR としての decisional value が低い。

NO_DECISION の構造的原因:

1. **OSS floor 不通過** — `matched-precision floor p ≥ 0.7` を 3 OSS variants すべてが ORG で通過せず、`oss_best = None` になり比較成立しない
2. **tied bootstrap CI=(0,0)** — DOB で `mean_diff = 0.000` / CI 幅ゼロ。観測上 strict precision は同等だが、locked rule では tie を NO_DECISION 扱いにしている
3. **measurement triad が ADR に明記されていない** — `corpus / metric / aggregation` の三つ組メタデータが artifact (`comparison.json`) には埋まっていたが、ADR-0005 本文には書き起こされておらず、後続の再現性が弱い

Phase 2 (#48 NER 再訓練) では `custom_cnn` artifact が更新され、同じ measurement script を回しても数値が変わる。さらに #69 で **measurement triad metadata (corpus / metric / aggregation) を含む scores.json** を canonical 計測 output として制定する。これにより:

- ADR-0005 の数値は **historical evidence** に確定 (Phase 1 artifact)
- Phase 2 の新数値は triad metadata 付きで再現可能 (Phase 2 artifact)

選択肢 A (ADR-0005 を更新) と B (ADR-0006 起案) のうち、**B を採用**:

- ADR-0005 の Pre-Registration anchor (`fd0cf4e` / `1fc4270`) は frozen 履歴として保持価値あり
- Phase 2 では measurement の前提 (custom artifact / triad metadata / 補助 metric) が変わるため、同 ADR 内で書き換えると差分が読み取りづらい
- S1 follow-on (recall@FP-budget metric 再設計) を取り込む受け皿としても新 ADR が適切

## Decision

1. **Canonical measurement output を triad metadata 付き `scores.json` (#69) に変更する。**
   - corpus axis: `held-out v0.13.0` / `in-domain v0.12.1-leak-fixed` を明示
   - metric axis: primary を **strict-span F1**、補助に matched-precision-floor recall + token-overlap F1
   - aggregation axis: per-template bootstrap CI 95% + per-document micro-average の二重集計

2. **MERGE / COMMIT / KILL verdict を strict-span F1 で再判定する。**
   - ADR-0005 が依拠した `r7 primary gate (matched-precision floor p≥0.7)` は補助 metric に降格
   - `recall@FP-budget` を S1 brainstorm で metric として正式評価し、本 ADR の Accepted 昇格時に primary 候補として再検討

3. **Pre-Registration anchor は本 ADR で再 freeze する。**
   - Phase 2 plan PR merge SHA / measurement script commit SHA を Accepted 昇格時に追記
   - ADR-0005 の anchor (`fd0cf4e` / `1fc4270`) は historical reference として残す

## Consequences

- **ADR-0005 は historical evidence として存続。** Status は `Superseded by ADR-0006` に更新するが、本文は変更しない (Pre-Registration commitment の毀損を避けるため)
- **S6 metric (matched-precision-floor recall + token-overlap F1) は補助 metric に降格。** primary verdict には用いない。ただし regression detector としては引き続き artifact に出力する
- **Phase 2 完了 (#48 close) 前は本 ADR は Proposed のまま。** placeholder 部分 (下記 Numbers) が埋まり次第、Accepted へ昇格させる別 PR を出す
- **再現性の向上。** triad metadata が ADR/artifact 双方に存在することで、third-party 再走時に corpus/metric/aggregation の食い違いを検出可能になる
- **Negative**: ADR-0005 と ADR-0006 で異なる primary metric を採用するため、Phase 1 vs Phase 2 の数値直接比較は不可。historical comparison が必要な場合は補助 metric (matched-precision-floor recall) で行う

## Numbers

Phase 2 first iteration (`hf_v02_tiny_aug_ext`, 2026-05-03; RunPod RTX A5000, 11 min, ~$0.05):

| 指標 | ORGANIZATION | DATE_OF_BIRTH |
|---|---|---|
| custom variant | `hf_v02_tiny_aug_ext` (deberta-v2-tiny-japanese) | `hf_v02_tiny_aug_ext` |
| strict-span F1 (v0.12.0/ja, micro) | 0.160 (P 0.088 / R 0.838) | 0.613 (P 0.442 / R 1.000) |
| strict-span F1 (v0.13.0 held-out) | (not re-run for this iter; baseline `pleno_ner` p=1.0/r=0.875) | (baseline `pleno_ner` p=1.0/r=1.0) |
| OSS best variant | matched-precision floor not met by any OSS variant | `ja_ginza` |
| OSS strict-span F1 | n/a | p=0.909 / r=1.000 (held-out v0.13.0) |
| verdict (custom_cnn baseline) | NO_DECISION (OSS unfit at p_budget; custom strong on held-out) | NO_DECISION (tied at perfect recall) |
| verdict (Phase 2 hf candidate) | **KILL — precision 8.8 % is well below floor; FP volume dominant** | **KILL — precision 44.2 % below 0.7 floor** |

Phase 2 hf candidate verdict on the v0.12.0 adversarial corpus is **KILL** for both ORG and DOB: the model meets neither the matched-precision floor nor the strict-span F1 of the spaCy `pleno_ner` baseline on this primary metric. The custom_cnn (`pleno_ner`) baseline retains the operational lead pending the next iteration.

Phase 2 measurement triad metadata (per `scores.json` entries):

- corpus: `v0.12.0/ja` (500-doc FP-pressure DLP benchmark)
- metric: `strict_span_f1`
- aggregation: `micro`

Pre-Registration anchor (Accepted 昇格時に追記):

- Phase 2 plan PR merge SHA: `88d2f8c` (`packages/training/Makefile` aug+ext targets, 2026-05-03)
- measurement script commit SHA: `9868ea8` (`evaluate_benchmark` unification #69/#70, 2026-05-03)
- Phase 2 result PR merge SHA: `<!-- this PR -->`

## Validation

Accepted 昇格時に下記を満たすこと:

- [x] Phase 2 artifact (`scores.json` triad metadata 付き) が repo に存在する (`packages/training/data/benchmark/v0.12.0/ja/scores.json` `hf_v02_tiny_aug_ext` 項)
- [x] ORG / DOB ともに verdict が MERGE / COMMIT / KILL のいずれか決定的になっている (Phase 2 hf candidate: KILL/KILL; custom_cnn baseline 維持)
- [x] Numbers セクションの placeholder がすべて実数値に置換されている
- [x] Pre-Registration anchor (Phase 2 plan PR merge SHA / measurement script commit SHA) が記録されている
- [ ] #48 close — first iteration AC 未達、ORG-FP follow-up issue 起票後に判断
- [ ] NO_DECISION が再発する場合は S1 brainstorm を起案し、recall@FP-budget metric への切替を別 ADR (-0007) で議論する

## Follow-on

- **S1 brainstorm (recall@FP-budget)**: 本 ADR Accepted 昇格と同期して metric 再設計を完了させる。primary 切替が決まれば ADR-0007 を起案
- **PERSON / ADDRESS / BANK_ACCOUNT**: ADR-0005 同様、本 ADR の scope 外。#49 (BANK_ACCOUNT recognizer) follow-on で個別 ADR
- **server image / cold-start**: OSS hybrid 同梱の意思決定は本 ADR でも保留。verdict が KILL となった場合のみ再検討対象

## References

- Issue: [#51 [Phase 3] ADR-0005 reissue — Phase 2 結果で書き直し or supersede](https://github.com/plenoai/pleno-anonymize/issues/51)
- Blocked by: [#48 NER 再訓練 (Phase 2)](https://github.com/plenoai/pleno-anonymize/issues/48), [#69 measurement triad scores.json schema](https://github.com/plenoai/pleno-anonymize/issues/69)
- Superseded ADR: [docs/adr/0005-ginza-presidio-partial-supersede.md](./0005-ginza-presidio-partial-supersede.md)
- Related: [docs/adr/0004-custom-ja-ner-model.md](./0004-custom-ja-ner-model.md), [docs/adr/0003-spacy-llm-presidio.md](./0003-spacy-llm-presidio.md)
