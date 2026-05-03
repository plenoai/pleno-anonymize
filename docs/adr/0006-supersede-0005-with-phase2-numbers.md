# ADR-0006: Supersede ADR-0005 with Phase 2 Measurement Triad

**Status:** Proposed (#48 完了後に Accepted へ昇格)
**Date:** 2026-05-04
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

<!-- pending #48 results -->

| 指標 | ORGANIZATION | DATE_OF_BIRTH |
|---|---|---|
| custom best variant | <!-- pending #48 --> | <!-- pending #48 --> |
| strict-span F1 (held-out v0.13.0) | <!-- pending #48 --> | <!-- pending #48 --> |
| strict-span F1 (in-domain) | <!-- pending #48 --> | <!-- pending #48 --> |
| OSS best variant | <!-- pending #48 --> | <!-- pending #48 --> |
| OSS strict-span F1 | <!-- pending #48 --> | <!-- pending #48 --> |
| `mean_diff` (custom − oss) | <!-- pending #48 --> | <!-- pending #48 --> |
| bootstrap CI 95% | <!-- pending #48 --> | <!-- pending #48 --> |
| verdict (MERGE / COMMIT / KILL) | <!-- pending #48 --> | <!-- pending #48 --> |

Phase 2 measurement triad metadata:

- corpus: `<!-- pending #69 scores.json schema -->`
- metric: `<!-- pending #69 scores.json schema -->`
- aggregation: `<!-- pending #69 scores.json schema -->`

Pre-Registration anchor (Accepted 昇格時に追記):

- Phase 2 plan PR merge SHA: `<!-- pending -->`
- measurement script commit SHA: `<!-- pending -->`

## Validation

Accepted 昇格時に下記を満たすこと:

- [ ] #48 が close されており、Phase 2 artifact (`scores.json` triad metadata 付き) が repo に存在する
- [ ] ORG / DOB ともに verdict が MERGE / COMMIT / KILL のいずれか決定的になっている
- [ ] NO_DECISION が再発する場合は S1 brainstorm を起案し、recall@FP-budget metric への切替を別 ADR (-0007) で議論する
- [ ] Numbers セクションの placeholder がすべて実数値に置換されている
- [ ] Pre-Registration anchor (Phase 2 plan PR merge SHA / measurement script commit SHA) が記録されている

## Follow-on

- **S1 brainstorm (recall@FP-budget)**: 本 ADR Accepted 昇格と同期して metric 再設計を完了させる。primary 切替が決まれば ADR-0007 を起案
- **PERSON / ADDRESS / BANK_ACCOUNT**: ADR-0005 同様、本 ADR の scope 外。#49 (BANK_ACCOUNT recognizer) follow-on で個別 ADR
- **server image / cold-start**: OSS hybrid 同梱の意思決定は本 ADR でも保留。verdict が KILL となった場合のみ再検討対象

## References

- Issue: [#51 [Phase 3] ADR-0005 reissue — Phase 2 結果で書き直し or supersede](https://github.com/plenoai/pleno-anonymize/issues/51)
- Blocked by: [#48 NER 再訓練 (Phase 2)](https://github.com/plenoai/pleno-anonymize/issues/48), [#69 measurement triad scores.json schema](https://github.com/plenoai/pleno-anonymize/issues/69)
- Superseded ADR: [docs/adr/0005-ginza-presidio-partial-supersede.md](./0005-ginza-presidio-partial-supersede.md)
- Related: [docs/adr/0004-custom-ja-ner-model.md](./0004-custom-ja-ner-model.md), [docs/adr/0003-spacy-llm-presidio.md](./0003-spacy-llm-presidio.md)
