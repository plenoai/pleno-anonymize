---
date: 2026-05-02
topic: ginza-presidio-baseline-comparison
---

# GiNZA + Presidio Honest Baseline Comparison (S6)

## Summary

ORG/DOB の 2 entity に絞った precision-budget 制約下の recall 比較 × template-slice で OSS NER stack (`ja_core_news_trf` + Presidio + 派生 variant 群) と現行自前 NER (CNN, BERT-base) を計測し、OSS が ORG/DOB に限り decisive に勝てば自前訓練 pipeline の ORG/DOB 担当を partial kill して entity-decoupled (S5) 構成で OSS+Presidio に切替、勝たなければ S1 (`recall@FP-budget` 評価指標再設計) を最優先タスクに昇格させる kill-or-commit 判断装置を作る。Kill propagation は ORG/DOB に narrow され、PERSON / ADDRESS / BANK_ACCOUNT は本 brainstorm の判断対象外 (follow-on brainstorm に委譲)。

---

## Problem Frame

`packages/training/` で運用してきた自前 NER 訓練 pipeline は、過去 8 iter のうち 4 iter が DISCARD で終わっており、KEEP した 4 iter も +1.2~+9.5pt の漸進改善にとどまる (`packages/training/CHANGELOG.md`)。v0.12.0 benchmark では precision 33% / recall 95.2% / F1 49% で本番投入できる比率に達しておらず、特に ORGANIZATION (-63pt drop) と DATE_OF_BIRTH (-45pt drop) が壊れている。

一方、外部の prior art は同領域で OSS 構成が高い recall を出している:
- PubMed 2025 報告: GiNZA + Presidio で日本語 clinical PHI に対し recall 0.995 / F1 0.802
- Microsoft Presidio 日本語拡張パターン (Mamezou 2025) で `ja_core_news_trf` + custom recognizer の integration が確立

ADR-0004 で「自前 spaCy NER + Transformer」を採用した時、この OSS 構成との直接比較は内部 corpus 上では行われていない。OSS が手元の `packages/training/data/benchmark/v0.12.0/ja/raw.json` 上でどの程度の性能を出すかは未測定であり、現行訓練 pipeline の機会コスト (RunPod GPU 時間、iter 回す maintainer 工数、annotation pollution リスク) を払い続ける根拠が empirical には存在しない。

これを measure せずに iter を続けると、(a) OSS が既に勝っている場合は sunk cost、(b) 自前が勝っている場合でも何点差なのか分からないまま「もう少し iter すれば」を続けることになる。S1 (recall@FP-budget) 等の周辺工学投資の出発点 ROI も、この比較なしには評価できない。

---

## Key Flows

- F1. **Measurement run**
  - **Trigger:** maintainer が `pleno-bench compare`(仮称) コマンドを叩く、もしくは RunPod ジョブとして submit
  - **Steps:** OSS variant 群 × within-variant percentile sweep × custom (CNN/BERT) を v0.12.0 corpus 上で並列推論 → per-document, per-template, per-entity の predictions を集計 → ORG/DOB に絞った precision-budget recall + token-overlap F1 + strict-span F1 + bootstrap CI を出力
  - **Outcome:** 比較 artifact (per-template/per-entity score table + Pareto plot data + bootstrap CI) が出力先に保存される
  - **Covered by:** R1, R2, R3, R4, R5, R6, R12, R13

- F2. **Decision evaluation**
  - **Trigger:** F1 の artifact を maintainer が review
  - **Steps:** ORG と DOB それぞれで span-count weighted matched-precision-budget recall (P ≥ 0.7 制約下の recall) を比較 → robust template-slice gate (p10 of per-template recall, min-span ≥ 5 templates のみ) で OSS が custom を下回らないか確認 → kill ゲート (R7 + R8 + R9 全 clear) のいずれか entity で成立すれば partial-kill 経路、それ以外は commit 経路
  - **Outcome:** kill-or-commit 判断が確定し、後続経路の起動条件が満たされる
  - **Covered by:** R7, R8, R9

- F3a. **Post-decision branch — in-scope (本 brainstorm で完結する作業)**
  - **Trigger:** F2 の判断結果
  - **Steps (kill 経路 in-scope):** ADR-0005 を起草し ADR-0004 を ORG/DOB 範囲のみ supersede → 比較 artifact を `experiments/log.jsonl` に記録 → 後続 brainstorm 起票 (server 切替 / packages/training/ 再定義 / PERSON-ADDRESS-BANK_ACCOUNT 評価)
  - **Steps (commit 経路 in-scope):** 比較 artifact を `experiments/log.jsonl` に記録 → S1 (`recall@FP-budget` 評価指標再設計) を次タスクに昇格 → ADR-0004 を「OSS との実測比較で支持された」として補注追記
  - **Outcome:** S6 brainstorm が close する条件を満たす
  - **Covered by:** R10, R11

- F3b. **Post-decision branch — out-of-scope (follow-on brainstorm に委譲)**
  - **Trigger:** F3a で kill 経路成立かつ ADR-0005 起草完了
  - **Steps:** server 側 NER backbone の OSS+Presidio 切替実装 / `packages/training/` の今後の主用途再定義 / S2/S4 等の優先順位調整 / PERSON/ADDRESS/BANK_ACCOUNT の独立比較 — これらは別 brainstorm で要件化
  - **Outcome:** 本 brainstorm scope 外、本 doc では設計しない
  - **Covered by:** Scope Boundaries (follow-on 明示)

---

## Requirements

**比較 scope と対象**

- R1. 比較対象 entity は `ORGANIZATION` と `DATE_OF_BIRTH` の 2 つに限定する。PERSON / ADDRESS / BANK_ACCOUNT は本比較の decisive 対象から除外する。Kill 経路成立時の切替も ORG/DOB のみに narrow する (entity-decoupled 構成、R10 参照)。
- R2. OSS variant pool は **3 構成**: `ja_core_news_trf`, `ja_ginza`, `ja_core_news_md` (CPU-only lower-bound として annotate)。各 variant は Microsoft Presidio (既存 server 側 `recognizers_ja.py` と同等の設定) と組み合わせて評価する。`ja_ginza_electra` は除外 (Dependencies の dep conflict 参照)。
- R3. Custom 側は現行配布の CNN モデルと BERT-base モデルの両方を sweep 対象に含める。OSS 3 構成 vs custom 2 構成は完全 symmetric ではないが、R7 で Bonferroni-style multiple-comparisons 補正を適用することで best-of-best inflation を統計的に補正する (M22)。

**測定設計 (Pareto + Slice)**

- R4. ORG / DOB それぞれについて、**per-variant percentile sweep** (within-variant rank top-k%、k ∈ {10, 20, 30, 50, 70, 90, 100} の 7 点) を実施し、precision/recall plane 上の Pareto frontier を構築できるデータを出す。spaCy default `ner` の per-span confidence は信頼できないため、各 variant の出力 span を「予測 entity ごとの内部 score」または「予測順位」で rank し、上位 k% を採用する。custom 側 (CNN/BERT) も同形式の percentile sweep を適用 (variant 間で symmetric)。
- R5. corpus は `packages/training/data/benchmark/v0.12.0/ja/raw.json` を gold とし、各文書の `_meta.template` フィールドを slice key として per-template の precision/recall を別途集計する。Span match 判定は **token-overlap F1 (relaxed: ≥ 50% IoU)** と **strict-span F1 (exact boundary)** の両方を report し、両者で gate 結論が一致することを kill 確定の必要条件とする (M9: UD/GSD-style 境界規則差で OSS が systematically penalize されるのを回避)。新規 adversarial ラベル付けは行わない。
- R6. 計算負荷の高い処理 (transformer 推論、複数 variant × percentile の組合せ) は RunPod GPU pod 上で並列実行する。Time-box は GPU 使用時間 ≤ 6h。`ja_core_news_trf` 系および `ja_ginza` の重い variant を優先順位の上位に置き、軽い variant (md, custom CNN) を後段に置く決定論的順序で実行する (R12 参照、time-box 超過時の partial-result 取り扱いに必要)。

**判断ゲート (S1 と整合した metric)**

- R7. **Kill 判断の primary gate (S1 整合 metric):** ORG と DOB の少なくとも一方で、precision ≥ 0.7 制約下での **span-count weighted aggregate recall** において「OSS 最良 variant の最良 percentile − custom 最良 variant の最良 percentile ≥ max(3pt, 2× bootstrap noise floor)」を満たすこと。weighting は span-count weighted (template ごとの ORG/DOB span 数で加重)。Multiple-comparisons 補正として、OSS 3 variants × 7 percentiles と custom 2 variants × 7 percentiles の合計 35 config を比較するため、Bonferroni-style に bootstrap CI の significance level を α/35 で評価する。F1 は副次的に report するのみで gate には用いない (S1 の `recall@FP-budget` 方針と整合)。
- R8. **Kill 判断の robustness gate:** R7 を満たした entity について、(a) **min-span threshold** — template ごとに ORG/DOB 合計 spans ≥ 5 の templates のみを worst-template 評価対象とし、5 未満の templates は "other" バケットに集約 (M5: low-span template の量子化 noise 排除)、(b) 評価対象 templates の **p10 of per-template recall** で OSS が custom を下回らない (差 ≥ 0pt)、(c) **token-overlap と strict-span の両指標で R7/R8 結論が一致**、3 条件全成立で kill 確定。
- R9. R7 が成立しない、または R7 のみ成立で R8 が崩れる場合、結果は commit 判断とする。拮抗 (差が `max(3pt, 2× noise_floor)` 以内) は commit 側に倒す (false-kill 回避)。S5 (entity-decoupled 評価) への移行可否は本 brainstorm では確定させず、commit 経路の S1 タスクに合流させる (Scope Boundaries と整合、M4)。

**Post-decision artifact**

- R10. **Kill 経路 (partial kill, ORG/DOB のみ):** ADR-0005 を起草し ADR-0004 を **ORG/DOB 範囲についてのみ** supersede する。新 ADR は (a) ORG/DOB の NER backbone を OSS+Presidio に切替える decision、(b) PERSON / ADDRESS / BANK_ACCOUNT は本 ADR の対象外であり別 brainstorm で評価する旨、(c) 関連する後続 brainstorm (S2 LLM verifier、S4 Browser-WASM、entity-decoupled 全面評価、`packages/training/` の今後の主用途) との関係を明記。`packages/training/` の主用途再定義は本 ADR/本 brainstorm では行わず follow-on に委譲 (M13)。
- R11. **Commit 経路:** `packages/training/experiments/log.jsonl` に比較結果のエントリを追加し、ADR-0004 に「OSS との honest baseline 比較で ORG/DOB 範囲では支持された / されなかった」旨の補注追記を行う。次タスクとして S1 (`recall@FP-budget` 評価指標再設計) を最優先に昇格させる。

**Measurement infrastructure**

- R12. **Time-box 超過時の取り扱い:** R6 の 6h hard limit を超過した場合、その時点で完走している variant × percentile pool で `R7/R8` を評価可能なのは「**全 OSS variants が完走** かつ **全 custom variants が完走**」かつ「percentile sweep のうち少なくとも 3 点 (低・中・高) が完走」した条件下のみ。それを満たさない場合は **no decision** とし、original requirements を維持したまま再キューする。partial pool で arbitrary subset の judgement は禁止 (M10: Sunk Cost Protection 違反防止)。
- R13. **Bootstrap CI:** 全 metric は bootstrap (n=1000, span-level resampling) で 95% CI を算出し、artifact に含める。R7 の noise floor は corpus と variant ごとに事前推定 (measurement 開始前に計算し pin)、結果を見てから動かさない (M8)。

---

## Acceptance Examples

- AE1. **Covers R7, R8, R9, R13.** Given v0.12.0 corpus 上で計測した per-entity per-template の matched-precision (P ≥ 0.7) recall artifact、when ORG の span-count weighted recall が `ja_core_news_trf + Presidio` で 0.78、custom BERT で 0.71 (差 +7pt、noise_floor 2pt → threshold max(3pt, 4pt)=4pt をクリア、Bonferroni 補正後の bootstrap 95% CI も非ゼロ)、かつ min-span ≥ 5 templates の p10 per-template recall で OSS 0.62 / custom 0.58 (差 +4pt)、かつ token-overlap と strict-span の両指標で同方向、then partial-kill 判断成立 (R7・R8 全条件クリア、ORG のみ kill)。
- AE2. **Covers R7, R8, R9.** Given 同上 artifact、when ORG span-count weighted recall で OSS が custom を +6pt 上回るが、p10 per-template recall で OSS が custom を 4pt 下回る (R8(b) 崩れ)、then kill 判断は不成立。commit 経路に進む。
- AE3. **Covers R7, R9, R13.** Given 同上 artifact、when ORG/DOB ともに span-count weighted recall 差が `max(3pt, 2× noise_floor)` 以内 (拮抗) もしくは Bonferroni 補正後の bootstrap CI がゼロを跨ぐ、then commit 判断 (拮抗は commit 側に倒す)。S1 タスク昇格を実行。
- AE4. **Covers R6, R12.** Given RunPod ジョブが 6h を超過、when 完走 variant pool が R12 の最低条件 (全 OSS + 全 custom variant 完走 かつ percentile 3 点以上) を満たさない、then **no decision**、original requirements を保持して再キュー (partial pool での arbitrary judgement は禁止)。R12 最低条件を満たす場合のみ、決定論的優先順 (重い variant を先に完走させる) で得られた pool に R7/R8 を適用する。
- AE5. **Covers R5.** Given token-overlap F1 で OSS +5pt、strict-span F1 で OSS −2pt、then 両指標で結論が一致しないため R8(c) 崩れ、commit 判断 (annotation-convention mismatch の可能性が示唆されたため、別途 boundary normalization を S1 task に含めて再評価)。

---

## Success Criteria

- **Human outcome (kill-or-commit が下せる):** maintainer が比較 artifact を review した時点で、迷いなく partial-kill 経路 (ORG/DOB のみ) / commit 経路のいずれかを選択できる。判断後に「もっと別の variant も測ればよかった」「別の slice では結果が違うかも」という後悔が残らない。
- **Sunk cost protection:** 判断ゲート (R7, R8, R9, R12) は measurement 開始前に確定済みであり、結果を見てから閾値・weighting・noise floor を動かさない。拮抗時は false-kill 回避で commit 側に倒すルールが事前合意されている。Time-box 超過時の partial-result 取り扱い (R12) も事前定義済み。
- **Downstream handoff:** ce-plan が本 requirements doc を読んで、(a) 比較スクリプトの構成、(b) RunPod GPU pod の組み立てと runbook 整備、(c) artifact 出力先と書式 (per-entity per-template per-variant per-percentile + bootstrap CI)、(d) ADR-0005 / experiments/log.jsonl への書き込みフォーマット を invent せず planning できる。
- **再現性:** 同じ corpus version で同じ commit から再実行すれば、artifact の数値が再現する (seed pin、model version pin、Presidio recognizer pack の hash pin、bootstrap seed pin)。
- **時間予算:** measurement → 判断 → ADR or experiments/log.jsonl 更新 までを 1-2 週間に収める。

---

## Scope Boundaries

**Entity 分類 3 グループ表 (M19):**

| グループ | Entities | 本 brainstorm での扱い |
| --- | --- | --- |
| (1) 本 brainstorm 比較対象 | `ORGANIZATION`, `DATE_OF_BIRTH` | R7/R8 の kill-or-commit 判断対象 |
| (2) 後続 brainstorm scope | `PERSON`, `ADDRESS`, `BANK_ACCOUNT` | 比較・kill 対象外、別 brainstorm で独立評価 |
| (3) ADR-0004 既決 (Presidio Regex 担当) | `PHONE_NUMBER`, `MY_NUMBER`, `CREDIT_CARD`, `EMAIL_ADDRESS`, `PASSPORT`, `DRIVER_LICENSE`, `IP_ADDRESS` | 形式依存、比較対象外、変更なし |

**その他の scope 境界:**

- Kill 判断後の切替計画は ORG/DOB のみの partial kill に narrow する (entity-decoupled 構成、R1/R10 と整合)。PERSON / ADDRESS / BANK_ACCOUNT は本 brainstorm の措定対象外であり、kill 経路成立時も自動的に OSS+Presidio に移すことはしない。これらの entity の評価は follow-on brainstorm で扱う。
- LLM stage2 verifier (S2) の組み込み・combined-system 比較は本 brainstorm の scope 外。S6 結果を受けた後続 brainstorm で扱う。
- Browser-WASM (S4) の検証は orthogonal で本 brainstorm に含めない。
- 本番 traffic 上の shadow inference は scope 外。template-based slicing で代替する判断を済ませている。
- 関西方言 / OCR noise / 英日混在 等の adversarial slice の手作業ラベリングは行わない。`_meta.template` ベースの slicing で十分とする (corpus に既存)。
- Active learning ループ (S3)、Per-prompt CI gate / κ-gate (S7) は kill-or-commit 判断後にどちらの経路でも必要だが、本 brainstorm では設計しない。
- 拮抗 (差 < `max(3pt, 2× noise_floor)`) の場合は R9 に従い commit 側に倒す。S5 (entity-decoupled 構成) の評価は本 brainstorm scope 外であり、commit 経路成立時に S1 に合流して個別評価する (R9 と Scope Boundaries で同一 resolution、M4)。
- **Follow-on brainstorm 委譲事項 (kill 経路成立時):** server 側 NER backbone 切替の実装段取り、`packages/training/` の今後の主用途再定義 (recognizer YAML 量産パイプラインへの転用可否含む)、PERSON/ADDRESS/BANK_ACCOUNT の独立比較、S2/S4 等後続 brainstorm の優先順位再評価 — これらは本 brainstorm では設計しない (M13, M15)。

---

## Key Decisions

- **比較 entity を ORG / DOB の 2 つに narrow し、kill propagation も ORG/DOB のみの partial kill に narrow した (M1):** PERSON / ADDRESS / BANK_ACCOUNT も含めると測定範囲が膨れて 1-2 週の time-box に収まらない。ORG (-63pt drop) と DOB (-45pt drop) は v0.12.0 で最も壊れている entity であり、ここで OSS が勝てば ORG/DOB に限定して切替えれば情報量は確保できる。PERSON は custom が最強の entity であり、ORG/DOB の勝利を 5 entity 全面 kill に extrapolate するのは warrant されない。S5 (entity-decoupled architecture) と整合。
- **Kill gate metric を F1 から S1 整合の matched-precision recall (precision ≥ 0.7 制約下の span-count weighted recall) に切り替えた (M2, M14):** ideation S1 で「F1 でなく recall@FP-budget」を主指標方針としたばかりで、S6 でも同 metric で gate を引かないと本 brainstorm 自体の artifact が S1 起動で陳腐化する。R7 metric を S1 と整合させたので S6/S1 ordering の競合は解消。
- **集計単位を span-count weighted aggregation に固定した (M3):** template ごとの span 数が大きく不均一 (ORG=37 spans / DOB=23 spans across 6 templates、最小 2 spans)。文書数 weighting は低 span template に過剰な発言権を与え、span-count weighting は単位 entity あたりの recall に直結する。Q2/Q5 はこれで Resolve 済み (Outstanding Questions 参照)。
- **R8 に min-span threshold (templates with ≥ 5 ORG/DOB spans のみ評価) と p10 robust statistic を導入 (M5):** worst-template F1 は 1 span FN で ~33pt 量子化動するため noise gate になる。p10 + min-span threshold で robust 化。
- **Token-overlap F1 と strict-span F1 の両方を report し gate 結論一致を要求 (M9):** ja_core_news_trf / GiNZA は UD/GSD 境界規則 (例: 「株式会社」suffix 除外) で訓練されており、自社 corpus の redact-perspective ラベリングと systematic に boundary 差が出る。両指標で結論一致を要求すれば boundary-only artifact での kill を防げる。
- **Bootstrap CI と動的 noise floor を導入 (M8):** ORG=37 spans / DOB=23 spans 規模では +3pt は CI 内に収まる可能性が高い。`max(3pt, 2× noise_floor)` の動的閾値と Bonferroni 補正で false-kill リスクを抑制。
- **Threshold sweep を per-variant percentile sweep に変更 (M11):** spaCy default `ner` は per-span confidence を信頼できる形で出さない。`beam_ner` 切替は「OSS as it ships」前提を破るため不採用。各 variant の予測 span を内部 score / rank で並べ、within-variant top-k% を採用する percentile sweep に統一 (custom 側も同形式で symmetric)。
- **Optimization budget を symmetric 化 + multiple-comparisons 補正 (M22):** OSS 3 variants × 7 percentiles = 21 vs custom 2 variants × 7 percentiles = 14 で大きさを揃え、R7 で Bonferroni-style に bootstrap CI を α/35 で評価。Asymmetric best-of-best inflation を抑制。
- **`ja_ginza_electra` を OSS variant pool から drop (M12):** `spacy-transformers <1.2` を要求し `packages/training/pyproject.toml` の `>=1.3` と conflict、同 venv で同居不可。別 venv で評価する価値も薄いため pool から drop し 3 構成 (`ja_core_news_trf`, `ja_ginza`, `ja_core_news_md`) に。
- **`ja_core_news_md` を CPU-only lower-bound として包含する rationale を annotate (M21):** trf に Pareto-dominate される前提だが、CPU-only 環境で得られる下限値を示すことで OSS 全 variant が CPU でも custom に勝てる場合の意味づけが可能。
- **`packages/training/` の主用途再定義は本 brainstorm/ADR-0005 から削除し follow-on に委譲 (M13):** 「recognizer YAML 量産パイプライン」は finite mission であり durable identity ではない。kill 後の `packages/training/` 取り扱いは別 brainstorm で要件化。Maintainer prior「自前が ≥5pt で勝つ」は anecdotal で evidence-supported ではないため、measurement で覆る前提として明記 (FYI1)。
- **F3 kill 経路を in-scope (F3a) と out-of-scope (F3b) に 2 段分割 (M15):** ADR-0005 起草 + experiments/log 記録までが本 brainstorm scope 内、server 切替実装 / packages/training/ 再定義 / 他 brainstorm 優先順位調整 / PERSON-ADDRESS-BANK_ACCOUNT 評価は follow-on brainstorm で要件化。
- **Time-box 超過時の partial-result 取り扱いを R12 で事前定義 (M10):** AE4 の post-hoc 「partial 判断許容」は Sunk Cost Protection 違反となる。完走条件 (全 OSS + 全 custom variant 完走 + percentile 3 点以上) を満たす場合のみ判断可、満たさない場合は no decision で再キュー。
- **Adversarial slice の代替に `_meta.template` を使う:** corpus は per-document `_meta.template` フィールドを既に持つ (`ocr_forms_a.txt`, `払戻請求OCR`, `本人確認OCR` 等の template 識別子)。新規 slice ラベリングは avoidable cost。p10 per-template + min-span threshold で slice-aware comparison の意図を満たす。
- **Pareto-frontier (B) と slice-aware (D) を最初から併用:** 単一構成 (A) で「拮抗」が出ると無駄な再 measurement が発生する。最初から variants × percentiles を sweep し template slice で aggregate する設計にすれば、結果の解釈がワンショットで終わる。
- **計算負荷の高い処理は RunPod 並列:** maintainer の global rule (`use runpod for training`) と整合。`ja_core_news_trf` 系 transformer 推論は GPU で大幅に加速される。Custom (CNN) と Presidio Regex は CPU で十分。なお既存 `packages/training/docs/runpod-training.md` は CPU 専用 runbook のみで GPU runbook は未整備、Outstanding Questions で Resolve Before Planning に昇格 (M20)。

---

## Dependencies / Assumptions

- **Corpus 利用可能性:** `packages/training/data/benchmark/v0.12.0/ja/raw.json` が gold standard として比較可能であり、entity span tag が ORG/DOB について十分量含まれている (確認済: ORG=37 spans / DOB=23 spans across 6 templates、最小 template DOB 2 spans)。Span 数の絶対値は小さいため bootstrap CI と動的 noise floor を必須化 (R13)。
- **OSS variant インストール可能性:** `ja_core_news_md` / `ja_core_news_trf` / `ja_ginza` の 3 つが pip / uv 経由でインストール可能 (`spacy[ja]>=3.8` は既に依存にあり)。`ja_ginza_electra` は `spacy-transformers <1.2` を要求し `packages/training/pyproject.toml` の `>=1.3` と conflict するため pool から除外 (Key Decisions M12 参照)。別 venv で評価する道はあるが本 brainstorm scope 外。
- **RunPod 利用可能性と GPU runbook gap:** maintainer が RunPod アカウントを保持しており、GPU pod (8vCPU/16GB 以上) を 6h 確保できる前提。ただし既存 `packages/training/docs/runpod-training.md` は CPU 専用で GPU 推論 runbook は未整備 — Outstanding Questions Q1 で Resolve Before Planning に昇格 (M20)。
- **Presidio 設定の同等性:** server 側 `recognizers_ja.py` の Regex pack を OSS 比較側でも使う。今回比較するのは NER 部分の差分であり、Regex 側を変えると比較が混線する。
- **Percentile sweep の妥当性:** spaCy default `ner` は per-span confidence を信頼できる形で出さないため、各 variant の予測 span を内部 score / 順位で rank し within-variant top-k% を採用する percentile sweep を全 variant に統一適用する (R4)。`beam_ner` 切替は「OSS as it ships」前提を破るため採用しない。
- **Noise floor 事前推定:** R7 の動的閾値 `max(3pt, 2× noise_floor)` の noise floor は measurement 開始前に bootstrap で推定し pin する (結果を見てから動かさない、Sunk Cost Protection)。
- **Maintainer prior の扱い:** 「自前が ≥5pt で勝つ」prior は anecdotal で evidence-supported ではない。本 brainstorm の measurement で覆る前提で進める (FYI1)。

---

## Outstanding Questions

### Resolve Before Planning

- **[Affects R6, R10][Resolved in this doc — surfaced for visibility]** Q2 (template-slice の集計単位) と Q5 (R7 weighting): **span-count weighted aggregation を採用** (Key Decisions 参照)。本 doc 内で resolve 済み、planning では再検討しない (M3)。
- **[Affects R6][Needs research]** Q1: GPU pod size, CUDA version, install path 含む RunPod GPU runbook の作成。既存 runbook (`packages/training/docs/runpod-training.md`) は CPU 専用、GPU 推論用 runbook は未整備 (M20)。Planning 開始までに最低限の draft が必要。
- **[Affects R10][User decision]** Q4: Kill 経路成立時に ADR-0005 起草と並行して server 切替 PR を出すか、ADR 起草のみに留めて切替 PR は follow-on brainstorm に回すか。本 doc は default を follow-on 委譲 (F3b) に置いているが、maintainer 判断で前倒し可能。

### Deferred to Planning

- [Affects R6][Technical] RunPod ジョブを単一 pod で variant × percentile sweep を回すか、variant ごとに別 pod に並列化するか — コスト最適とログ集約の trade-off で決定。
- [Affects R10][Scope] Follow-on brainstorm 群 (server 切替 / packages/training/ 再定義 / PERSON-ADDRESS-BANK_ACCOUNT 評価) の起票順序と相互依存関係。
