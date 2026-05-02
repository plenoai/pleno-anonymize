---
date: 2026-05-02
topic: practical-model
focus: 実用可能なモデルの実現
mode: repo-grounded
---

# Ideation: 実用可能な日本語 PII 匿名化モデルの実現

## Grounding Context

### Codebase Context (pleno-anonymize)
- Python 3.12 + FastAPI (server) + spaCy NER + Microsoft Presidio のハイブリッド構成
- Layout: `packages/training/`, `packages/models/`, `server/`, `website/`, `packages/wasm-tokenizer/`
- ADR-0004 の分担: 文脈依存 (PERSON/ORG/ADDRESS/DOB/BANK) = NER、形式依存 (電話/マイナンバー/カード/メール) = Presidio Regex
- Two-tier model: CNN ~50MB (Lambda 512MB) / BERT-base ~440MB (fly.io / GPU 想定)
- Training: GPT-5 系 LLM 合成データ → spaCy 訓練 (RunPod GPU) → ONNX export
- Deploy: Lambda Container (cold start ~30s) + `fly.toml` (併存)
- 実験管理: `experiments/log.jsonl`, `packages/training/CHANGELOG.md`, `docs/adr/`
- 既存資産: `export_onnx.py`, `wasm-tokenizer`, `error_analysis.py`, hand-authored DLP corpus (v0.10.0+)

### Pain Points (Critical)
- v0.12.0 benchmark (88% negative docs): **precision 33% / recall 95.2% / F1 49%** — FP 過多で本番投入不能
- ORGANIZATION: 84.5% → 21.6% (-63pt)、DATE_OF_BIRTH: 91.2% → 46.2% (-45pt)
- iter08a: 全テンプレートからの bulk LLM データ生成 → annotation pollution → catastrophic regression (-26.2%)
- 「Negative augmentation reduces FP but dilutes positive patterns」 — naive data mix では FP/recall がゼロサム
- score_weights による recall ブースト (iter03/iter09) は 2 回 DISCARD 済み
- Dev 95.8% vs Bench v0.12.0 49% の指標乖離 (Dev は誤導指標)
- RunPod OOM: 8vCPU/16GB 未満で 30k+ docs は必ず落ちる
- ADR-0001 (Lambda 前提) と現状 (`fly.toml` 併存) が乖離

### Leverage Points
- Error-analysis-driven loop (iter04 +9.5%, iter10 +1.2%) — 実証済み高 ROI
- Two-stage 分解 (high-recall stage1 + FP-classifier stage2) は ADR レベルで未着手
- ONNX export 既存 + WASM tokenizer 既存 → ブラウザ完結推論パスが技術的に開いている
- v0.10.0 以降 hand-authored DLP corpus に移行済み (OpenAI 依存解消済み)

### External Context (2025-2026)
- **GiNZA + Presidio (PubMed 2025)**: 日本語 clinical PHI で **recall 0.995 / precision 0.672 / F1 0.802**, recall-first 設計
- **Japanese medical PHI benchmark (Sci Reports 2026)**: Mistral-Small-3.2 24B + Self-Refine = **91.54/100 (GPT-4.1 比 97.8%)**, Self-Refine は 87-88pt 以下のモデルに **+6.92pt** threshold 効果
- **ACL 2025 data-constrained synthesis**: 合成データのみで gold 比 -2~3pt F1, **annotator quality がボトルネック**, 量より質
- **NVIDIA Nemotron-PII / GLiNER-PII**: 100K 合成、55+ カテゴリ、92% recall / 64% F1、**英語のみ**
- **ai4privacy DeBERTa-v3**: F1 0.9757, 54 entity, 6 言語、**日本語なし**
- **競合**: Private AI (多言語汎用), Tonic (dev/test data), Skyflow (tokenization vault), Presidio (OSS基盤) — **日本語 span NER 特化の本番モデルは市場空白**
- Spam-filter analogy: active learning で label cost 50-80% 削減

### Past Learnings (docs/adr/, training CHANGELOG, experiments/log.jsonl)
- データ品質 > データ量 > パラメータチューニング (8 iter で実証)
- 8 iter 中 4 KEEP は全て「弱点を狙ったデータ生成/拡張」、4 DISCARD は全て「naive 増量 or 重み調整」
- CNN width 拡張 (iter02) は ROI 負 (7→24.7MB / 4→13.4ms / ADDRESS -8.3%)

---

## Ranked Ideas

### 1. 評価指標を Recall@FP-budget に再設計し、per-entity ratchet floor CI と benchmark lockfile で固める

**Description:** 主指標を F1 から「FP rate ≤ 0.5% 制約下での recall」(Neyman-Pearson) に切り替え、Dev set を release gate から除外して hand-authored DLP corpus の `worst-slice` (ORG / DOB / 古い住所 / OCR noise / 英日 mixed) を gating metric とする。`benchmarks/vX.Y.lock` に corpus SHA / annotation guideline hash / scorer version を pin し、PR ごとに entity-wise floor (例: `ORG ≥ 21.6%`, `DOB ≥ 46.2%`) を CI で gate、KEEP 採用時に floor を ratchet 上昇させる。

**Warrant:**
- `direct:` v0.12.0 benchmark (precision 33% / recall 95.2% / F1 49%, ORG -63pt, DOB -45pt) と Dev 95.8% vs Bench 49% の 47pt 乖離が `experiments/log.jsonl` に記録されている。
- `external:` PubMed 2025 GiNZA+Presidio が recall 0.995 / precision 0.672 を「実用構成」と報告 — 臨床 PHI で recall-first はデファクト。
- `reasoned:` 47pt の指標乖離は noise ではなく定義のずれ。これを直さない限り後続の iter は dev に overfit し続ける。

**Rationale:** 「実用可能か」の判定そのものが現状壊れている。F1 49% は本来 reject 対象だが、recall@FP-budget 観点では「stage2 に渡す候補列挙器として既に PASS かもしれない」候補。逆に Dev 95.8% は誤導指標で、これに合わせた最適化は本番に転移しない。指標を直し ratchet 化することで naive data mix の zero-sum 交換 (S7 の前提) が機械的に検出可能になり、後続 6 個の survivor が乗る土台になる。

**Downsides:** 既存実験ログのスコアが retrospective に再評価不能 (lockfile 切る前の数字は legacy)。release のハードルが一時的に上がりリリース頻度が落ちる。

**Confidence:** 90%
**Complexity:** Low (CI gate と評価スクリプトの差し替えのみ、訓練側に手を入れない)
**Status:** Unexplored

---

### 2. High-recall stage1 + LLM verifier stage2 の Two-stage cascade

**Description:** stage1 を「recall 99% / FP 50% を許容する極端 recall 検出器」として GiNZA + Presidio Regex + 緩い CNN の union で構成し、stage2 で span 単位に Mistral-Small-3.2 24B クラスのローカル LLM を Self-Refine 付きで「これは本当に PII か?」を二値判定させて FP を落とす。stage2 への入力は span だけなのでトークン数が桁違いに少なくレイテンシ・コストが両立する。出力は `{entity_type, span, confidence}` の DSL に constrained decoding。

**Warrant:**
- `external:` Sci Reports 2026 で Mistral-Small-3.2 + Self-Refine が **91.54/100 (GPT-4.1 比 97.8%)**、87-88pt 以下のモデルに **+6.92pt** threshold 効果。PubMed 2025 GiNZA+Presidio で recall 0.995 達成 → stage1 候補の存在を実証。
- `direct:` 自社 v0.12.0 で precision 33% / recall 95.2% — recall 軸は既に達成、precision 軸は現アーキテクチャでは限界 (iter03/iter09 の score_weights 2回 DISCARD が示す)。
- `reasoned:` precision を学習で上げる方向は「Negative augmentation reduces FP but dilutes positive」のゼロサムを踏むが、二段に分解すれば stage1 は recall に専念、stage2 は precision に専念できる。

**Rationale:** v0.12.0 の本番投入不可性は recall 不足ではなく FP 過多。stage2 を独立コンポーネントにすれば、訓練を止めずに precision を 33% → 80%+ に押し上げる経路が成立し、Self-Refine 効果 +6.92pt がそのまま使える。代替形態として **rewriter LLM (Sci Reports 2026 Mistral 91.54 そのまま)** が span 検出を完全置換する超ラディカル版で、span は監査ログの副産物になる。

**Downsides:** stage2 の LLM レイテンシ (1-3s/req)、リアルタイム API では非同期 webhook パターンが必要。fly.io GPU エンドポイントが必須。stage2 のプロンプトと calibration が新たな failure mode。

**Confidence:** 85%
**Complexity:** Medium (stage1 は既存資産の再構成、stage2 は新規 LLM サーバー)
**Status:** Unexplored

---

### 3. Production shadow eval → CNN/BERT disagreement → LLM-as-annotator → κ-gate の closed-loop active learning

**Description:** 本番 traffic を sample で shadow inference (current model + candidate model 並列) し、両者の span 不一致と CNN/BERT 不一致 (= disagreement) を週次の review queue に流す。disagreement span だけを GPT-5 / Claude に再アノテートさせ、人間は二重ラベル 10% で Cohen's κ を実時間計測 (κ < 0.7 のソースは隔離)。新ラベルは entity-wise で次 iter のデータに 1:1 で投入し、`error_analysis.py` を post-eval hook 化して bucket 単位 prompt に直結させる。最初は ORG/DOB に絞った 200-500 spans から start。

**Warrant:**
- `external:` Spam-filter analogy で active learning は label cost 50-80% 削減実証済み。ACL 2025: annotator quality がボトルネック、量より質。
- `direct:` `error_analysis.py` 既存 (実装済みだがループ未統合)、iter04 +9.5% / iter10 +1.2% は error-analysis-driven で実証。8 iter 中 KEEP した 4 件は全て「弱点を狙った」生成。
- `reasoned:` 合成データ依存の限界は本番分布との gap (Dev/Bench 47pt 乖離の根本原因)。disagreement-as-uncertainty は理論的に最強の query strategy で、3 つの問題 (Dev/Bench gap・annotator quality・error-analysis loop の人手依存) を 1 本の pipeline で同時に解く。

**Rationale:** 「データ品質 > データ量 > ハイパラ」(8 iter で実証) を pipeline default に格下げし、人間の規律に依存しない構造に変える。S1 の per-entity floor が ratchet するたびに最も寄与する disagreement bucket が自動で next iter の燃料になり、iter08a 型の bulk pollution が起こる隙間が消える。

**Downsides:** 顧客同意 (consent) のデザインが必要 — fly.io API に `X-Pleno-Consent` のような flag と契約条項。LLM-as-annotator のコスト ($10-50/月で済むはずだが pseudo-PII 含むので Anthropic API 優先)。κ ラベルのための二重アノテーション工数。

**Confidence:** 88%
**Complexity:** High (consent + shadow infra + queue + LLM annotator + κ gate)
**Status:** Unexplored

---

### 4. Browser-local WASM 推論を primary deployment にして zero-trust データフローを実現

**Description:** 既存の `export_onnx.py` + `packages/wasm-tokenizer` を tiny-CNN (5-12MB, BERT-base からの distillation) と組み合わせ、`<textarea>` の oninput でストリーミング匿名化する SDK を primary deployment にする。サーバー側 (fly.io / Lambda) は監査ログ・ベンチマーク・モデル配布のみ担う。Lambda Container パスは廃止し、ADR-0001 を新 ADR-0005 (WASM primary / fly GPU は audit / LLM stage2 は非同期) で supersede。

**Warrant:**
- `direct:` ONNX export と WASM tokenizer が既に存在 (`packages/wasm-tokenizer`, `export_onnx.py`)、ADR-0001 (Lambda) と `fly.toml` の現状乖離が grounding に明記。CNN 50MB は Lambda 512MB を踏まえた既存サイズで、distillation で 5-12MB に縮められる。
- `external:` Piiranha が token-accuracy 99.44% / 17 entity で WASM 級小型でも実用域を実証。Skyflow の vault モデルを越える「PII がサーバーを横断しない」プライバシー保証は法務・医療・金融で購買決定要因。
- `reasoned:` Lambda cold start 30s 問題、Lambda/fly 二重メンテのオーバーヘッド、SaaS 課金設計の困難さ — 全部 browser-local で同時に消える。価格モデルが per-MAU SaaS から self-host/freemium SDK にピボット可能で、日本語 span NER 空白市場における distribution moat になる。

**Rationale:** 「PII を匿名化するためにサーバーに PII を送る」というビジネスモデル上の矛盾を解消する一手。「実用可能なモデル」の制約が「Lambda 512MB に乗る」から「WASM ロード時間内に DL できる」に変わり、CNN を太らせない方向 (iter02 の DISCARD と整合) を強制できる。Cold start 30s 問題、デプロイ二重化、競合 (Private AI / Skyflow) との差別化 — 4 つの問題を 1 本で消す。

**Downsides:** WASM ランタイムが対応していないブラウザ・モバイルで使えない (フォールバックが必要)。distillation のチューニングが新規ワークロード。stage2 LLM (S2) は browser には乗らないので「リアルタイム=WASM、audit=fly LLM」の two-tier 思想を新 ADR で確立する必要。

**Confidence:** 82%
**Complexity:** Medium (distillation + SDK + ADR-0005 起草、既存 WASM 資産の延長)
**Status:** Unexplored

---

### 5. Per-entity decomposed detector で ORG/DOB drop を構造的に解消

**Description:** 全エンティティを単一モデルで学習するのを止め、共有 encoder + per-entity head の multi-task (もしくは entity ごとに完全独立な軽量 detector) に変更。各 head は entity-specific な FP/recall threshold を持ち、独立にリリース・ロールバック可能。第一弾は ORG / DOB / PERSON の 3 head 構成、形式依存 entity (PHONE / マイナンバー / CARD / EMAIL) は ADR-0004 通り Presidio Regex に残す。

**Warrant:**
- `direct:` v0.12.0 で ORG -63pt、DOB -45pt の catastrophic drop は単一モデル内の干渉現象 (iter08a の -26.2% も同様)。`experiments/log.jsonl` に「あるエンティティ向けの改善が他を壊す」パターンが反復記録されている。
- `external:` Multi-head NER は標準的アーキテクチャパターン。ai4privacy DeBERTa-v3 も entity-typed classification head 構成。
- `reasoned:` 電話番号は recall=99% / FP=0.01% を要求するが、ORG は recall=85% / FP=2% で十分許容される — 同じ loss surface に押し込める理由がない。

**Rationale:** S1 の per-entity ratchet floor と直接整合し、「ORG だけロールバック・PERSON は新版」という運用が物理的に可能になる。data quality の局所悪化が全体を壊さなくなり、S3 の disagreement-driven AL も entity ごとに独立した queue になり error-analysis の解像度が一段上がる。

**Downsides:** モデル数が増えメンテナンス対象が増える (mitigation: shared encoder で容量共通化)。entity 間の表現共有から得ていた汎化が一部失われる可能性 (要 A/B 測定)。

**Confidence:** 78%
**Complexity:** Medium (spaCy custom architecture か HF transformers への切替、訓練・配布パイプライン更新)
**Status:** Unexplored

---

### 6. GiNZA+Presidio honest baseline を社内ベンチで再現し、自前訓練の kill-or-commit を判断する

**Description:** PubMed 2025 報告の「GiNZA + Presidio で recall 0.995」を社内 hand-authored DLP corpus 上で再現実装し、現行 hybrid CNN+BERT との 4 軸 (precision / recall / latency / コスト) honest comparison を出す。差分が ≤ 5pt なら自前 NER 訓練を停止し、「GiNZA + Presidio + S2 stage2 verifier + recognizer-as-code (LLM 生成 YAML pipeline)」に pivot することも俎上に載せる。逆に差分が大きければ自前訓練の ROI が確定し、S3/S5 への投資が正当化される。

**Warrant:**
- `external:` GiNZA + Presidio (PubMed 2025) で recall 0.995 / F1 0.802 達成事例。Microsoft Presidio の ja_core_news_trf integration pattern (Mamezou 2025) は確立した実装手順。
- `direct:` 自社 8 iter のうち 4 DISCARD が naive 系列 (iter05/08/11/08a)、KEEP 4 件も +1.2~+9.5pt と漸進的 — 「自前 from-scratch 訓練の ROI が頭打ちかもしれない」シグナル。
- `reasoned:` OSS baseline に勝てているかを実測しないと、すべての改善投資 (S2/S3/S5) の出発点 ROI が不明。これを測らずに iter を続けるのは sunk cost に縛られた行動。

**Rationale:** 「実用可能なモデル」の最短路が自前学習の継続なのか OSS + 周辺工学なのかを決める分岐点。kill 判断なら RunPod 訓練・experiments/log.jsonl・iter 運用全体が `packages/training/` から消え、リソースを S2 stage2 verifier と S4 SDK に集中できる。commit 判断なら逆に S3/S5 への投資の意味が明確になる。

**Downsides:** baseline 再現に 1-2 週かかる。「自前モデルが OSS に勝てない」結果が出ると組織的にきつい (= 必要な真実)。pivot した場合、過去 8 iter の知見の一部 (合成データ生成 prompt 群) は資産として残るが訓練 pipeline 自体は廃棄。

**Confidence:** 95% (測定すべきという主張への confidence)
**Complexity:** Low (1-2週の comparison 実装)
**Status:** Unexplored

---

### 7. Data quality firewall: provenance + per-prompt held-out CI + auto-DISCARD gate + κ agreement

**Description:** 全 annotation に `source` (human / llm-bulk / llm-self-refine / synthetic / production-AL) と `quality_score` を必須 metadata として保持。新プロンプト追加 PR では当該プロンプトのみで生成した 200 docs を held-out に保留し slice 単体の F1 と annotation consistency を CI で検証 (iter08a 型再発を構造的に防ぐ)。さらに GitHub Actions に bench gate を入れ、PR の training artifact を ONNX export して DLP corpus 上で eval、precision < 70% または entity-wise drop > 10pt なら merge block (iter08a の -26.2% を pre-merge で検出)。本番データ流入時は S3 の κ agreement を gate にして低品質ソースを隔離。

**Warrant:**
- `direct:` iter08a の -26.2% catastrophic regression は「全テンプレ bulk LLM 生成 → annotation pollution」が原因と grounding に明記、`experiments/log.jsonl` に記録。CHANGELOG.md は手動運用で「なぜ DISCARD か」の構造化タグが薄い。
- `external:` ACL 2025: annotator quality がボトルネック、量より質。NVIDIA Nemotron-PII は 92% recall / 64% F1 と低 precision で合成 annotation 汚染と一致。
- `reasoned:` 「データ品質 > データ量 > ハイパラ」(過去 8 iter の経験則) を pipeline 構造に焼き込む。汚染は「品質チェックを忘れる」ことで起こるので、人間規律ではなく CI gate で物理的に通せなくする必要がある。

**Rationale:** S3 の active learning が回り始めると新ラベルが流入し続けるため、汚染検出を前提にした data-flow design が同時に必要。iter08a 型 catastrophic regression を merge 前に検出することで、過去 8 iter で DISCARD に費やした $$ と時間 (推定 4 RunPod ジョブ分) を構造的に節約。S1 の ratchet floor + S5 の per-entity head と組み合わせると、「どのソースのどの entity ラベルが floor を割ったか」が因果まで特定できる。

**Downsides:** schema migration が experiments/log.jsonl の既存 entries に必要。per-prompt held-out CI の運用コスト (PR ごとに 200 docs 生成 + eval、$1-3/PR)。κ 計測のための二重アノテーション工数 (S3 と共有)。

**Confidence:** 85%
**Complexity:** Medium (Pydantic schema + CI workflow + benchmark gate、訓練パイプラインへの metadata 配線)
**Status:** Unexplored

---

## Rejection Summary

| # | Idea | Reason Rejected |
|---|------|-----------------|
| F1#5 | RunPod gradient checkpoint + spot-resilient checkpointing | 戦術的 infra、「実用可能なモデル」の ambition floor 未満 (S6 が決定的な問いを先に置く) |
| F2#1 | Stop training; ship rule-router | S6 (honest baseline 比較) のラディカル版だが、measure-first の S6 に統合 |
| F2#2 | Invert dev/bench (dev removed) | S1 の評価指標再設計に統合済み |
| F2#4 | Remove one-shot training, only error-analysis loop | S3 の closed-loop active learning に統合済み |
| F2#5 | Kill Lambda; fly one-tier | S4 (Browser-WASM primary) で Lambda が自動的に廃止される (上位互換) |
| F2#8 | Auto-gen ADR/CHANGELOG from log.jsonl | tooling polish、ambition floor 未満 |
| F3#3 | Synthetic data as weak labels (soft loss) | S3 + S7 でカバー (provenance + κ-gate が等価以上の効果) |
| F3#4 | LLM rewriter approach (PII-free regeneration) | S2 の rationale に「代替形態」として明示。単独 survivor とすると S2 と直接競合し survivor 数が膨らむため統合 |
| F3#7 | Sell benchmark/adapter library, not model | subject-replacement 寄り (匿名化サービス → データ事業へのピボット)、subject 内の reframe を超える |
| F4#1 | experiments/log.jsonl Pydantic schema CLI | S7 の前提として暗黙的に必要だが、独立 survivor とするには tooling 寄り |
| F4#8 | RunPod 訓練 Python CLI (manifest駆動) | 同上、S6 で訓練廃止判断後は不要になる可能性あり |
| F5#1 | Air-gap diode architecture | warrant が `reasoned:` のみで具体的実装路が薄い、speculation 比率が高い |
| F5#2 | Pharmacovigilance PRR drift detector | 興味深い analogy だが S3 の disagreement mining でカバー |
| F5#5 | Perceptual hashing co-reference | S5 (per-entity decomposition) と部分重複、独立 survivor にするには narrow |
| F5#6 | TSA risk-based document routing | S2 (stage1+stage2) のバリエーション、cascade 思想に統合 |
| F5#8 | AV signature DB + heuristic 二層 | S6 の recognizer-as-code pipeline と並列、統合済み |
| F6#5 | Adversarial generation loop with ratio lock | S7 の per-prompt CI + S3 disagreement で代替可能、controlled 範囲を超えると iter08a 再演リスク |
| F6#7 | GLiNER-style zero-shot 日本語 | 「別のモデルを作る」アプローチ、S6 の baseline-first 判断後に検討する派生選択肢 |

---

## Cross-cutting Synthesis Notes

7 survivors は依存関係を持つ。実行順序の自然な流れ:

```
S6 (kill-or-commit 判断, 1-2週)
  ├── kill 経路 → S2 + S4 + S6 派生 recognizer pipeline
  └── commit 経路 → S1 (評価改革) → S5 (per-entity 再構築) → S3 (AL loop) → S7 (data firewall)
                         ↓
                       S2 (stage2 LLM verifier) は両経路で有効
                         ↓
                       S4 (Browser-WASM) は orthogonal、いつ着手してもよい
```

S6 を最初に置くべき理由: 自前訓練が OSS baseline に勝てていない場合、S1/S3/S5/S7 の投資は機会損失になる。逆に勝てているなら S1 が必須。
