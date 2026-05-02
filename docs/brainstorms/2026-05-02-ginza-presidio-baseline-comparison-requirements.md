---
date: 2026-05-02
topic: ginza-presidio-baseline-comparison
---

# GiNZA + Presidio Honest Baseline Comparison (S6)

## Summary

ORG/DOB の 2 entity に絞った Pareto-frontier × template-slice 比較で OSS NER stack (`ja_core_news_trf` + Presidio + 派生 variant 群) と現行自前 NER (CNN, BERT-base) を計測し、OSS が ≥3pt 勝てば自前訓練 pipeline を全面 kill して全 entity を OSS+Presidio+custom recognizer pack に切替、勝たなければ S1 (`recall@FP-budget` 評価指標再設計) を最優先タスクに昇格させる kill-or-commit 判断装置を作る。

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
  - **Steps:** OSS variant 群 × threshold sweep × custom (CNN/BERT) を v0.12.0 corpus 上で並列推論 → per-document, per-template, per-entity の predictions を集計 → ORG/DOB に絞った F1/precision/recall + Pareto frontier データを出力
  - **Outcome:** 比較 artifact (per-template/per-entity score table + Pareto plot data) が出力先に保存される
  - **Covered by:** R1, R2, R3, R4, R5, R6

- F2. **Decision evaluation**
  - **Trigger:** F1 の artifact を maintainer が review
  - **Steps:** ORG と DOB それぞれで mean per-template F1 を比較 → worst-template F1 で OSS が custom を下回らないか確認 → kill ゲート (Mean ≥3pt 勝ち AND worst-template ≥0pt) のいずれか entity で成立すれば kill 経路、それ以外は commit 経路
  - **Outcome:** kill-or-commit 判断が確定し、後続経路の起動条件が満たされる
  - **Covered by:** R7, R8, R9

- F3. **Post-decision branch**
  - **Trigger:** F2 の判断結果
  - **Steps (kill 経路):** ADR-0005 を起草し ADR-0004 を supersede → server 側 NER backbone を OSS stack に切替準備 → `packages/training/` の主用途を recognizer YAML 量産パイプラインに再定義 → 関連 brainstorm (S2 LLM verifier, S4 Browser-WASM) の優先順位を更新
  - **Steps (commit 経路):** 比較 artifact を `experiments/log.jsonl` に記録 → S1 (recall@FP-budget 評価指標再設計) を次タスクに昇格 → ADR-0004 を「OSS との実測比較で支持された」として補注追記
  - **Outcome:** 次の作業対象が明確になり、S6 brainstorm が close する
  - **Covered by:** R10, R11

---

## Requirements

**比較 scope と対象**

- R1. 比較対象 entity は `ORGANIZATION` と `DATE_OF_BIRTH` の 2 つに限定する。PERSON / ADDRESS / BANK_ACCOUNT は本比較の decisive 対象から除外する。
- R2. OSS variant pool は最低 4 構成: `ja_core_news_md`, `ja_core_news_trf`, `ja_ginza`, `ja_ginza_electra`。各 variant は Microsoft Presidio (既存 server 側 `recognizers_ja.py` と同等の設定) と組み合わせて評価する。
- R3. Custom 側は現行配布の CNN モデルと BERT-base モデルの両方を sweep 対象に含める。

**測定設計 (Pareto + Slice)**

- R4. ORG / DOB それぞれについて、threshold sweep (≥7点、min 0.3 / max 0.9 を含む) を実施し、precision/recall plane 上の Pareto frontier を構築できるデータを出す。
- R5. corpus は `packages/training/data/benchmark/v0.12.0/ja/raw.json` を gold とし、各文書の `_meta.template` フィールドを slice key として per-template の F1/precision/recall を別途集計する (新規 adversarial ラベル付けは行わない)。
- R6. 計算負荷の高い処理 (transformer 推論、複数 variant × threshold の組合せ) は RunPod GPU pod 上で並列実行する。Time-box は GPU 使用時間 ≤ 6h。

**判断ゲート**

- R7. Kill 判断の primary gate: ORG と DOB の少なくとも一方で「mean per-template F1 (OSS 最良 variant の最良 threshold) − mean per-template F1 (custom 最良 variant の最良 threshold) ≥ +3pt」を満たすこと。
- R8. Kill 判断の robustness gate: R7 を満たした entity について、worst-template F1 で OSS が custom を下回らない (差 ≥ 0pt)。両 gate 同時成立で kill 確定。
- R9. R7 が成立しない、または R7 のみ成立で R8 が崩れる場合、結果は commit 判断とする。拮抗 (差 ±3pt 以内) は commit 側に倒す (false-kill 回避)。

**Post-decision artifact**

- R10. Kill 経路では ADR-0005 を起草し ADR-0004 を supersede する。新 ADR は (a) OSS stack を新 NER backbone とする決定、(b) `packages/training/` の主用途を recognizer YAML 量産に再定義する方針、(c) 関連する後続 brainstorm (S2 LLM verifier、S4 Browser-WASM、recognizer-as-code パイプライン) との関係を明記する。
- R11. Commit 経路では `packages/training/experiments/log.jsonl` に比較結果のエントリを追加し、ADR-0004 に「OSS との honest baseline 比較で支持された」旨の補注追記を行う。次タスクとして S1 (`recall@FP-budget` 評価指標再設計) を最優先に昇格させる。

---

## Acceptance Examples

- AE1. **Covers R7, R8, R9.** Given v0.12.0 corpus 上で計測した per-entity per-template F1 の最終 artifact、when ORG の mean per-template F1 が `ja_core_news_trf + Presidio` で 0.78、custom BERT で 0.74 (差 +4pt) かつ worst-template で OSS 0.61 / custom 0.59 (差 +2pt)、then kill 判断成立 (R7・R8 両方クリア)。
- AE2. **Covers R7, R8, R9.** Given 同上 artifact、when ORG mean F1 で OSS +5pt 勝つが worst-template で OSS が custom を 4pt 下回る、then kill 判断は不成立 (R7 はクリアだが R8 が崩れる)。commit 経路に進む。
- AE3. **Covers R7, R9.** Given 同上 artifact、when ORG/DOB ともに mean F1 差が ±2pt 以内に収まる、then commit 判断 (拮抗は commit 側に倒す)。S1 タスク昇格を実行。
- AE4. **Covers R6.** Given RunPod ジョブが想定 6h を超過、when 完走前に GPU 課金が time-box を超える見込み、then 残りの variant × threshold 組合せを skip し、その時点で得られたデータで判断を試みる (部分結果の判断ゲート適用は許容)。

---

## Success Criteria

- **Human outcome (kill-or-commit が下せる):** maintainer が比較 artifact を review した時点で、迷いなく kill 経路 / commit 経路のいずれかを選択できる。判断後に「もっと別の variant も測ればよかった」「別の slice では結果が違うかも」という後悔が残らない。
- **Sunk cost protection:** 判断ゲート (R7, R8, R9) は measurement 開始前に確定済みであり、結果を見てから閾値を動かさない。拮抗時は false-kill 回避で commit 側に倒すルールが事前合意されている。
- **Downstream handoff:** ce-plan が本 requirements doc を読んで、(a) 比較スクリプトの構成、(b) RunPod ジョブの組み立て、(c) artifact 出力先と書式、(d) ADR-0005 / experiments/log.jsonl への書き込みフォーマット を invent せず planning できる。
- **再現性:** 同じ corpus version で同じ commit から再実行すれば、artifact の数値が再現する (seed pin、model version pin、Presidio recognizer pack の hash pin)。
- **時間予算:** measurement → 判断 → ADR or experiments/log.jsonl 更新 までを 1-2 週間に収める。

---

## Scope Boundaries

- PERSON / ADDRESS / BANK_ACCOUNT entity の比較は本 brainstorm の scope 外。Kill 判断後の切替計画では全 entity が OSS+Presidio に移るが、移行可否の measurement は ORG/DOB の結果で代行する。
- 形式依存 entity (`PHONE_NUMBER`, `MY_NUMBER`, `CREDIT_CARD`, `EMAIL_ADDRESS`, `PASSPORT`, `DRIVER_LICENSE`, `IP_ADDRESS`) は ADR-0004 で Presidio Regex 担当が確定済みのため比較対象外。
- LLM stage2 verifier (S2) の組み込み・combined-system 比較は本 brainstorm の scope 外。S6 結果を受けた後続 brainstorm で扱う。
- Browser-WASM (S4) の検証は orthogonal で本 brainstorm に含めない。
- 本番 traffic 上の shadow inference は scope 外。template-based slicing で代替する判断を済ませている。
- 関西方言 / OCR noise / 英日混在 等の adversarial slice の手作業ラベリングは行わない。`_meta.template` ベースの slicing で十分とする (corpus に既存)。
- Active learning ループ (S3)、Per-prompt CI gate / κ-gate (S7) は kill-or-commit 判断後にどちらの経路でも必要だが、本 brainstorm では設計しない。
- 拮抗 (差 ±3pt 以内) の場合に「entity-decoupled (S5)」へ移行するか commit に留めるかの選択は本 brainstorm では確定させず、commit 経路の S1 タスクと合わせて再検討する。

---

## Key Decisions

- **比較 entity を ORG / DOB の 2 つに narrow した:** PERSON / ADDRESS / BANK_ACCOUNT も含めると測定範囲が膨れて 1-2 週の time-box に収まらない。ORG (-63pt drop) と DOB (-45pt drop) は v0.12.0 で最も壊れている entity であり、ここで OSS が勝てば他 entity でも有利な可能性が高く、勝てなければ自前訓練の存在意義は確実に守られる、という非対称な情報量を持つ。
- **Kill threshold を ≥3pt に設定した (拮抗は commit 側):** maintainer の prior は「自前が ≥5pt で勝つ」。この prior に対し ≥3pt の OSS 勝ちは prior を覆すに十分な surprise であり、かつ measurement noise (template によるばらつき) より大きい。差 ±3pt 以内は false-kill リスクが高いので commit 側に倒す。
- **Adversarial slice の代替に `_meta.template` を使う:** corpus は per-document `_meta.template` フィールドを既に持つ (`ocr_forms_a.txt`, `払戻請求OCR`, `本人確認OCR` 等の template 識別子)。新規 slice ラベリングは avoidable cost。worst-template F1 を robustness gate に使うことで slice-aware comparison の意図を満たす。
- **Pareto-frontier (B) と slice-aware (D) を最初から併用:** 単一構成 (A) で「拮抗」が出ると無駄な再 measurement が発生する。最初から variants × thresholds を sweep し template slice で aggregate する設計にすれば、結果の解釈がワンショットで終わる。
- **計算負荷の高い処理は RunPod 並列:** maintainer の global rule (`use runpod for training`) と整合。`ja_core_news_trf` 系 transformer 推論は GPU で大幅に加速される。Custom (CNN) と Presidio Regex は CPU で十分。
- **Kill receiver は ADR-0005 と recognizer YAML 量産に再定義した `packages/training/`:** 自前 NER 訓練 pipeline は廃止しても、DLP corpus と prompt 群は資産として残る。これらを「Presidio recognizer pack の LLM 量産パイプライン」に再利用することで、過去 8 iter の知見を kill 後も生かせる。

---

## Dependencies / Assumptions

- **Corpus 利用可能性:** `packages/training/data/benchmark/v0.12.0/ja/raw.json` が gold standard として比較可能であり、entity span tag が ORG/DOB について十分量含まれている (確認済: head 部分で ORG/DOB span を確認)。
- **OSS variant インストール可能性:** `ja_core_news_md` / `ja_core_news_trf` / `ja_ginza` / `ja_ginza_electra` の 4 つが pip / uv 経由でインストール可能 (`spacy[ja]>=3.8` は既に依存にあり)。`ja_ginza_electra` は GiNZA 公式配布物だが Python 3.12 互換性は要確認 — 不可なら variant pool から外して合計 3 構成で進める。
- **RunPod 利用可能性:** maintainer が RunPod アカウントを保持しており、GPU pod (8vCPU/16GB 以上) を 6h 確保できる。OOM 教訓 (`packages/training/docs/runpod-training.md`) を踏まえ、推論専用なら 16GB で十分の見込み。
- **Presidio 設定の同等性:** server 側 `recognizers_ja.py` の Regex pack を OSS 比較側でも使う。今回比較するのは NER 部分の差分であり、Regex 側を変えると比較が混線する。
- **Threshold 比較の妥当性:** spaCy の NER score 出力は softmax 後の confidence と仮定。GiNZA / `ja_ginza_electra` でも同等の confidence が取れる前提 (取れない variant が出た場合は決定論的 threshold ではなく固定推論結果のみで比較)。

---

## Outstanding Questions

### Resolve Before Planning

- なし。判断 gate (R7, R8, R9) と scope は本 brainstorm で全て locking 済み。

### Deferred to Planning

- [Affects R6][Technical] RunPod ジョブを単一 pod で variant × threshold sweep を回すか、variant ごとに別 pod に並列化するか — コスト最適とログ集約の trade-off で決定。
- [Affects R5][Technical] template-slice の集計単位を「文書数」と「entity span 数」のどちらにするか — 文書数が少ない template が ORG/DOB span を多く含む場合に metric が歪むリスクの評価が必要。
- [Affects R2][Needs research] `ja_ginza_electra` の Python 3.12 互換性確認 — 非互換なら variant pool から除外して合計 3 構成。
- [Affects R10][User decision] Kill 経路成立時に server 側の切替を「即時 PR」とするか「ADR-0005 起草のみ → 切替 PR は別タスク」とするかの段取り。
- [Affects R7][Technical] 「mean per-template F1」の計算で template ごとの weight を均等にするか、template に含まれる span 数で重み付けするか。
