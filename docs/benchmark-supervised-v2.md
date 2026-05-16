# `ja_ner_ja-v2-supervised` — benchmark + methodological accounting

Released at [`0xhikae/ja_ner_ja-v2-supervised`](https://huggingface.co/0xhikae/ja_ner_ja-v2-supervised).

This is the **R2 revision** of the benchmark doc. The previous
revision passed Smoke and (synthetic-OOD) Parity but the peer
reviewer flagged three blocking concerns:

- (R1-C1) Label-blind span merging inflated the OOD number
- (R1-C2) No real-text Japanese in the eval suite
- (R1-C3) Single training run — no variance estimate

This revision addresses all three. Real-text performance is **worse
than v2's synthetic OOD numbers suggested**, and the reviewer's
caveat ("calibrate below 0.862, not at it") was correct.

## TL;DR with 3-seed mean ± std

3 training seeds (42, 7, 1337), identical recipe; 1000-iter
document-level bootstrap CIs on the seed-42 run for the ranges.

| Eval set | Mean F1 | Std | Seed-42 CI 95% | Smoke ≥ 0.50 | Parity ≥ 0.82 |
|---|---:|---:|---|:---:|:---:|
| In-dist (300k-ja val, 300 docs) | **0.955** | 0.002 | [0.935, 0.973] | ✅ | ✅ |
| OOD synthetic (v1 test+dev, 134 docs, strict) | **0.773** | 0.004 | [0.745, 0.797] | ✅ | ❌ |
| OOD synthetic (label-aware merged) | **0.852** | 0.014 | [0.846, 0.885] | ✅ | ✅ (with span-class merging) |
| **Real text (stockmark JP Wikipedia, PII subset, 147 docs)** | **0.467** | 0.010 | [0.393, 0.520] | ❌ | ❌ |
| Real text (stockmark, all 8 categories, 276 docs) | 0.395 | 0.009 | [0.343, 0.430] | ❌ | ❌ |

**Honest reading:** v2 dominates in-distribution. On synthetic OOD
it matches Parity under label-aware merging. **On real Japanese
text (Wikipedia), v2 falls below Smoke.** spaCy `ja_core_news_lg`
beats v2 on the same real-text set (see baselines below).

## Methodology

Char-IoU ≥ 0.5, label-agnostic. 1000-iter document-level bootstrap
with fixed seed=42 for CIs.

Training: same recipe across all 3 seeds: `FacebookAI/xlm-roberta-base`,
25,082 JP rows from `0xhikae/pii-masking-300k-ja` train split, 2 epochs,
batch 16, lr 5e-5, fp16. Per-seed evaluation runs use the seed's own
checkpoint.

## In-distribution evaluation

Validation split of `0xhikae/pii-masking-300k-ja`, 300 docs.

| Model | F1 | F1 95% CI | P | R | Latency |
|---|---:|---|---:|---:|---:|
| `builtin` v0.13.0 | 0.342 | — | 0.453 | 0.275 | 55 ms |
| `ja_ner_ja-v2-mechanism` (v1, synthetic only) | 0.352 | — | 0.612 | 0.247 | 37 ms |
| spaCy `ja_core_news_lg` | 0.274 | [0.250, 0.297] | 0.205 | 0.411 | 22 ms |
| `openai-privacy-filter` v0.13.0 | 0.702 | — | 0.899 | 0.576 | 2.3 s |
| **`ja_ner_ja-v2-supervised` (seed 42)** | **0.957** | [0.935, 0.973] | 0.933 | 0.983 | 43 ms |
| `ja_ner_ja-v2-supervised` (seed 7) | 0.954 | — | 0.927 | 0.982 | — |
| `ja_ner_ja-v2-supervised` (seed 1337) | 0.954 | — | 0.926 | 0.983 | — |
| **3-seed mean ± std** | **0.955 ± 0.002** | | | | |

**Caveat unchanged from R1:** v2 was trained on the **train split**
of the same dataset. Per template-overlap analysis (below) the
splits share only 0.4 % of char-level surface skeletons, so this is
not pure template memorisation — but template-disjoint does not
imply pipeline-disjoint. Read this as "supervised fit on the
methodology", not production performance.

## Train/val template overlap (split-leakage probe)

`0xhikae/pii-masking-300k-ja` is a JP fork of
`ai4privacy/pii-masking-300k`. Two probes on 20k train / 1.5k val
rows:

| Signature | Train distinct | Val distinct | Train-val overlap |
|---|---:|---:|---:|
| Char-level (150 char skeleton, labels in place of PII surface) | 19,961 / 20,000 | 1,497 / 1,500 | **0.4 %** (6 / 1,500) |
| Label-sequence only (tuple of label types) | 13,952 | 1,223 | 36.1 % |

The char-level result rules out wholesale surface-form memorisation;
label-sequence overlap of 36 % is expected for any fixed-vocabulary
NER and does not by itself imply leakage.

## Out-of-distribution evaluation — Simula synthetic v1, 134 docs

Combined dev+test of the v1 (Simula) synthetic dataset:
`packages/training/data/raw/ja-mechanism-v1/{dev,test}.jsonl`.
Different pipeline, different label schema (17 pleno vs 28
ai4privacy v2 emits), zero overlap with v2 training data.

### Strict char-IoU ≥ 0.5 (no merging)

| Model | F1 | F1 95% CI | P | R |
|---|---:|---|---:|---:|
| spaCy `ja_core_news_lg` | 0.855 | [0.832, 0.878] | 0.787 | 0.937 |
| **v2-supervised (seed 42)** | 0.770 | [0.745, 0.797] | 0.718 | 0.829 |
| v2-supervised (seed 7) | 0.778 | — | — | — |
| v2-supervised (seed 1337) | 0.771 | — | — | — |
| **3-seed mean ± std** | **0.773 ± 0.004** | | | |

### Label-aware merged (R1-C1 fix)

v2 emits fine-grained ai4privacy labels (`LASTNAME1`, `GIVENNAME1`,
`STREET`, `CITY`, ...) while the v1 OOD set uses coarse pleno
labels (`PERSON`, `ADDRESS`, ...). `eval_ood_span_merged.py`
defines explicit equivalence classes:

- `PERSON` ← `{LASTNAME1/2/3, GIVENNAME1/2, TITLE, USERNAME}`
- `ADDRESS` ← `{STREET, CITY, STATE, POSTCODE, BUILDING, SECADDRESS, COUNTRY, GEOCOORD}`
- `DATE_OF_BIRTH` ← `{BOD, DATE, TIME}`
- `PHONE` ← `{TEL}`, `EMAIL` ← `{EMAIL}`, `IP` ← `{IP}`, `SEX` ← `{SEX}`
- `ID_CARD` ← `{IDCARD, DRIVERLICENSE, PASSPORT, PASS, SOCIALNUMBER}`

Adjacent v2 sub-spans are merged into one super-span **only when
both labels map to the same coarse class**. Cross-class adjacency
(e.g., `LASTNAME1` next to `TEL` in form text) is NOT collapsed.

| Model | F1 | F1 95% CI | P | R |
|---|---:|---|---:|---:|
| **v2-supervised (seed 42)** | **0.867** | [0.846, 0.885] | 0.883 | 0.851 |
| v2-supervised (seed 7) | 0.857 | — | 0.860 | 0.853 |
| v2-supervised (seed 1337) | 0.833 | — | 0.858 | 0.810 |
| **3-seed mean ± std** | **0.852 ± 0.014** | | | |
| spaCy `ja_core_news_lg` (no merge applicable) | 0.855 | [0.832, 0.878] | 0.787 | 0.937 |

Mean OOD-merged 0.852 is statistically indistinguishable from
spaCy's 0.855 — overlapping CIs and seed std 0.014 dominates the
difference.

## Real-text evaluation (R1-C2 fix) — stockmark JP Wikipedia NER

Real Japanese Wikipedia sentences with 8 entity categories:
人名, 法人名, 政治的組織名, その他の組織名, 地名, 施設名, 製品名, イベント名.
Sourced from `stockmark/ner-wikipedia-dataset`, n=300 sampled rows.

Reported two ways: (a) all 8 categories, and (b) restricted to the
**PII-relevant subset** `{人名, 地名}` since the other six categories
(corporations, products, events, facilities) are out-of-scope for
a PII NER like v2 by design.

| Model | Subset | F1 | F1 95% CI | P | R |
|---|---|---:|---|---:|---:|
| **spaCy `ja_core_news_lg`** | All 8 | **0.709** | [0.679, 0.736] | 0.642 | 0.792 |
| spaCy `ja_core_news_lg` | PII-subset | 0.571 | [0.533, 0.608] | 0.425 | 0.871 |
| **v2-supervised (3-seed mean)** | PII-subset | **0.467 ± 0.010** | [0.393, 0.520] | 0.486 | 0.436 |
| v2-supervised (3-seed mean) | All 8 | 0.395 ± 0.009 | [0.343, 0.430] | 0.616 | 0.281 |

**v2 loses to spaCy by 0.10 F1 on real-text PII subset.** This is
the honest result, and it reflects:

1. **Domain mismatch.** v2 was trained on form-/record-/chat-style
   PII text (ai4privacy generation methodology). Wikipedia narrative
   prose is a very different distribution.
2. **Schema mismatch.** v2 is trained to find specific PII categories
   (phones, emails, postcodes, ID numbers). Wikipedia entities are
   often legal entities (`法人名`) and facilities that v2 was never
   trained to recognise.
3. **spaCy's home turf.** `ja_core_news_lg` was trained on Wikipedia-
   derived data and benefits structurally.

**A truly fair real-text PII eval would use hand-annotated
chat/form/email Japanese.** That dataset does not exist publicly.
Building one (~50–100 samples) is the highest-priority follow-up.

The stockmark result should be read as: "on this kind of text,
in this kind of context, with this kind of schema, v2 trails
spaCy". It is not a verdict on v2's PII performance in production
PII contexts.

## Seed variance summary

3 seeds × 4 eval sets:

| Eval | seed 42 | seed 7 | seed 1337 | Mean | Std |
|---|---:|---:|---:|---:|---:|
| In-dist | 0.957 | 0.954 | 0.954 | 0.955 | 0.002 |
| OOD strict | 0.770 | 0.778 | 0.771 | 0.773 | 0.004 |
| OOD merged | 0.867 | 0.857 | 0.833 | 0.852 | 0.014 |
| Real (PII) | 0.460 | 0.460 | 0.482 | 0.467 | 0.010 |
| Real (full) | 0.386 | 0.392 | 0.407 | 0.395 | 0.009 |

Variance is consistent across eval sets — small and well below the
CI widths of the eval-set-side bootstrap.

## Acceptance tiers — final read

| Tier | F1 floor | In-dist | OOD strict | OOD merged | Real (PII) | Real (full) |
|---|---:|:---:|:---:|:---:|:---:|:---:|
| Smoke | 0.50 | ✅ | ✅ | ✅ | ❌ | ❌ |
| Parity | 0.82 | ✅ | ❌ | ✅ | ❌ | ❌ |
| Stretch | 0.88 | ✅ | ❌ | ❌ | ❌ | ❌ |

The honest read: **Smoke and Parity are met on synthetic eval. Real-
text performance is below Smoke even on PII-relevant categories**, so
real-text Parity is not claimed.

Production deployment expectations should be calibrated to the
real-text number (~0.47), not the synthetic OOD number (~0.85).

## Reproducibility

- All scripts in `packages/training/scripts/`:
  - `train_supervised_300k_ja.py` (seed pinned)
  - `eval_mechanism_on_300k.py` (in-dist, char-IoU ≥ 0.5)
  - `eval_ood_jsonl.py` (OOD strict)
  - `eval_ood_span_merged.py` (OOD label-aware merge)
  - `eval_stockmark_jp_real.py` (real-text)
  - `eval_classic_baseline.py` (spaCy / GiNZA)
  - `compute_ci_bootstrap.py` (1000-iter bootstrap CIs)
- Seed pinning: python/numpy/torch/`PYTHONHASHSEED`/HF `TrainingArguments(seed, data_seed)`
- Library versions: ranged in `pyproject.toml` extras; not fully pinned (Minor open item)
- Training dataset is private; the script reads from a local JSONL
  dump, scp'd to RunPod. Third-party reproduction needs dataset access.

## What's still open (would block top-venue Accept)

Resolved in this revision:
- ✅ R1-C1: label-aware span merge (replaces label-blind merge)
- ✅ R1-C2: real-text eval added (stockmark Wikipedia)
- ✅ R1-C3: 3-seed variance reported (std on every cell)
- ✅ Reviewer's R0 #1–10 either resolved or downgraded to Minor

Still open:
- ❌ **No PII-context real-text eval.** Wikipedia is real but
  off-domain. ≥50 hand-annotated JP chat/form/email samples would
  resolve this. Highest-priority follow-up.
- ❌ Library versions not pinned in `pyproject.toml`.
- ❌ Only one classic baseline (spaCy). GiNZA would be natural #2.
- ❌ AI4Privacy upstream split-protocol still not fully documented.
- ❌ v1→v2 epoch confound (2 vs 3 epochs).

The single highest-impact follow-up is hand-annotating ≥50 real
JP PII samples. Until then, real-text production performance is
estimated from the stockmark Wikipedia result with the caveat that
PII-context is a different distribution.
