# Backend Comparison on ai4privacy/pii-masking-300k

**Last updated:** 2026-05-12
**Authors:** pleno-anonymize team
**Status:** Initial release. Sample size will grow as additional runs land
(see [§7](#7-reproducibility)).

## Abstract

We compare two interchangeable detection backends shipped with
`pleno-anonymize` — the default `builtin` engine (Microsoft Presidio +
in-house spaCy NER) against `openai-privacy-filter` (OPF, the open-source
`openai/privacy-filter` checkpoint, 1.5B parameters / 50M active
mixture-of-experts) [\[1\]](#references). Our intended evaluation target
was the Japanese subset of `ai4privacy/pii-masking-300k`
[\[2\]](#references), since pleno-anonymize is a JP-first product; we
verified empirically that **the dataset contains zero Japanese rows**
(en/fr/de/it/es/nl only, confirmed by enumerating all 47,728 validation
examples and by the HF dataset card's `cardData.language` field). We
therefore report English-only results and explicitly flag that the
operationally decisive JP-first comparison cannot be made on this
dataset. Using label-agnostic character-IoU ≥ 0.5 span matching on the
English split (n = 300), OPF reaches **F1 = 0.818** (P = 0.904, R =
0.747) against `builtin`'s **F1 = 0.315** (P = 0.390, R = 0.265). A
50-document pilot agreed within 0.03 F1, so the estimate is stable.
OPF runs roughly **200× slower per document on CPU** (2.2 s vs 11 ms).
The gap is concentrated in (i) free-text person names where the
in-house EN NER is not trained on the AI4Privacy label scheme, and (ii)
locale-specific structured identifiers we ship no recognizer for. The
findings justify shipping OPF as an **opt-in** backend (PR #140) but
not changing the default until the Japanese comparison has been run on
a dataset that actually contains Japanese. We additionally verified
that the larger sibling release `ai4privacy/pii-masking-openpii-1m`
(1.4 M rows) is also Japanese-free (23 European languages only), so
the next JP-capable target is pleno's internal held-out JP corpora;
sourcing or building an independent JP PII benchmark is called out
as future work (§8).

## 1. Motivation

The `pleno-anonymize` proxy intercepts requests to LLM APIs (OpenAI,
Anthropic, Gemini) and masks PII before they leave the user's perimeter.
Missed entities (false negatives) directly translate to PII leakage; over-
matching (false positives) degrades the downstream LLM response. Until
now, the only backend has been a regex-and-NER pipeline tuned for Japanese.
OpenAI's April 2026 release of an Apache-2.0 token-classification model
specifically targeted at this workload [\[1\]](#references) raises the
question: *does the production default need to change?* This document
answers that question quantitatively.

## 2. Systems Under Test

| System | Version | Components |
|---|---|---|
| `builtin` | `pleno-anonymize` 0.2.0 | Presidio Analyzer 2.2; spaCy 3.8; `en_ner_en` 0.1.0 (proprietary, trained on JP-first data with English augmentation); 12 regex + checksum recognizers (Luhn, mynumber, etc.) |
| `openai-privacy-filter` | `opf` @ `f7f00ca` (main, 2026-05-12 snapshot) | 1.5B-parameter pre-norm transformer encoder, 8 layers, GQA (14Q/2KV), 128-expert sparse MoE (top-4), 128k context, BIOES Viterbi decoding [\[1\]](#references). Checkpoint `openai/privacy-filter`, 2.8 GB safetensors |

Both engines expose the same `analyze(text) -> list[Finding]` interface in
the pleno SDK; the only difference at the call site is `engine=` in the
factory.

## 3. Dataset

**`ai4privacy/pii-masking-300k`** [\[2\]](#references). We verified the
language inventory by streaming the entire validation split (n =
47,728): the dataset contains six European languages and **does not
contain Japanese**.

| Language | Validation rows |
|---|---:|
| French | 8,413 |
| German | 8,120 |
| Italian | 7,976 |
| English | 7,946 |
| Spanish | 7,816 |
| Dutch | 7,457 |
| **Japanese** | **0** |

The HF dataset card's `cardData.language` field independently confirms
`["en", "fr", "de", "it", "es", "nl"]` only. This is a **fundamental
limitation for this study**: pleno-anonymize is JP-first and OPF's model
card explicitly notes "primarily English; selected multilingual
robustness evaluation reported" [\[1\]](#references), so the operationally
most important comparison — JP-first vs OPF on Japanese — is not
possible on this dataset. See §6.3 and §8 for our follow-up plan.

For this report we therefore evaluate on the dataset's English subset.
The first 50 English validation rows are retained (deterministic
ordering from the HF streaming iterator). The dataset annotates 27
fine-grained PII classes (e.g. `LASTNAME1`, `LASTNAME2`, `POSTCODE`,
`IP`, `EMAIL`, `USERNAME`, `TIME`); we use only the character span
boundaries and ignore the labels for scoring (see §4).

We report two sample sizes for the English split. The n = 50 result is
retained for traceability against §5; the n = 300 result is the primary
estimate. Numbers agree within 0.03 F1 across all engines (§5.4),
indicating the 50-document estimate was not unstable.

| Quantity | n = 50 | n = 300 |
|---|---:|---:|
| Language filter | English | English |
| Gold spans | 316 | 1,776 |
| Mean spans / document | 6.32 | 5.92 |
| Distinct gold labels observed | 27 | 28 |

## 4. Evaluation Protocol

### 4.1 Span match criterion

A predicted span (P) is judged a true positive (TP) against a gold span
(G) when their character intersection-over-union exceeds a threshold τ:

$$\text{IoU}(P, G) = \frac{|P \cap G|}{|P \cup G|} \;\geq\; \tau, \quad \tau = 0.5$$

Greedy assignment matches each gold span to its highest-IoU unmatched
predicted span. Unmatched predicted spans count as false positives (FP);
unmatched gold spans as false negatives (FN). Label classes are
**deliberately ignored** during matching for two reasons:

1. The two backends emit disjoint label vocabularies (8 OPF classes vs
   pleno's ~17 classes vs the dataset's 27 fine-grained classes); any 1:1
   mapping would be lossy and arbitrary.
2. The proxy's operational concern is "was sensitive content masked?" —
   the placeholder type matters less than the masking decision itself.

We report a per-label recall breakdown (§5.3) for diagnostic insight but
not for the headline metric.

### 4.2 Metrics

$$P = \frac{\text{TP}}{\text{TP}+\text{FP}}, \quad
R = \frac{\text{TP}}{\text{TP}+\text{FN}}, \quad
F_1 = \frac{2PR}{P + R}$$

Latency is wall-clock time around `engine.analyze(text)`, averaged across
documents, single-threaded, no batching. Cold model load is excluded
(warm-up call before the timed loop).

### 4.3 Hardware

| Component | Spec |
|---|---|
| CPU | Apple M-series, 12 cores |
| RAM | 64 GB |
| GPU | None (deliberately — establishes the CPU-only floor) |
| OS | macOS 25.3 (Darwin) |
| Python | 3.12.8 |
| PyTorch | 2.11.0 (CPU build) |

GPU numbers are interpolated from the OPF model card claim of ≈30 ms/doc
on a single A100 [\[1\]](#references); we have not measured them
ourselves and flag this as future work.

## 5. Results

### 5.1 Headline (English, τ = 0.5)

Primary estimate (n = 300):

| Engine | Precision | Recall | F1 | Latency / doc | TP | FP | FN |
|---|---:|---:|---:|---:|---:|---:|---:|
| `builtin` | 0.390 | 0.265 | 0.315 | 11 ms | 470 | 734 | 1,306 |
| `openai-privacy-filter` | **0.904** | **0.747** | **0.818** | 2,239 ms | 1,327 | 141 | 449 |

Original pilot (n = 50), retained for traceability:

| Engine | Precision | Recall | F1 | Latency / doc | TP | FP | FN |
|---|---:|---:|---:|---:|---:|---:|---:|
| `builtin` | 0.386 | 0.272 | 0.319 | 53 ms | 86 | 137 | 230 |
| `openai-privacy-filter` | 0.915 | 0.788 | 0.847 | 2,203 ms | 249 | 23 | 67 |

OPF dominates on all three quality metrics by a wide margin at both
sample sizes. The **precision** gap (+0.51 at n = 300) shows OPF is not
buying recall with false positives; the **recall** gap (+0.48) shows the
in-house EN NER simply does not cover the dataset's vocabulary. The
latency gap (≈200× at n = 300, partly inflated by `builtin` running
warmer on the larger sample) remains qualitatively the same: OPF runs a
1.5B-parameter transformer per call.

### 5.4 Sample-size sensitivity

| Engine | F1 (n = 50) | F1 (n = 300) | |Δ| |
|---|---:|---:|---:|
| `builtin` | 0.319 | 0.315 | 0.004 |
| `openai-privacy-filter` | 0.847 | 0.818 | 0.029 |

The pilot's headline numbers held to within 0.03 F1 at 6× the sample
size. OPF's slight regression (0.847 → 0.818) is consistent with the
pilot landing on an easier slice of the iteration order; we treat the
n = 300 estimate as the operating number.

### 5.2 Span counts

| Quantity | `builtin` | `openai-privacy-filter` |
|---|---:|---:|
| Gold spans (constant) | 316 | 316 |
| Predictions emitted | 223 | 272 |
| Predictions that matched gold (TP) | 86 | 249 |
| Unmatched predictions (FP) | 137 | 23 |

OPF emits ~22% more predictions than `builtin` but only ~10% of OPF's
predictions are spurious, vs ~61% for `builtin`. In other words,
`builtin`'s low precision is not "it predicts everything" — it's "what it
does predict often doesn't line up with gold span boundaries."

### 5.3 Per-label recall (label-agnostic match, recall attributed to gold label)

Where each engine wins or loses on the 27 dataset classes:

| Gold class | `builtin` R | OPF R | Δ |
|---|---:|---:|---:|
| `EMAIL` | 1.000 | 1.000 | 0.000 |
| `IP` | 1.000 | 1.000 | 0.000 |
| `SOCIALNUMBER` | 1.000 | 1.000 | 0.000 |
| `PASSPORT` | 0.840 | 1.000 | +0.160 |
| `DRIVERLICENSE` | 0.500 | 1.000 | +0.500 |
| `USERNAME` | 0.385 | 0.962 | +0.577 |
| `TEL` | 0.455 | 1.000 | +0.545 |
| `DATE` | 0.222 | 1.000 | +0.778 |
| `BOD` (date of birth) | 0.250 | 1.000 | +0.750 |
| `STREET` | 0.154 | 1.000 | +0.846 |
| `IDCARD` | 0.059 | 1.000 | +0.941 |
| `TIME` | 0.038 | 0.154 | +0.116 |
| `TITLE` | 0.000 | 0.500 | +0.500 |
| `GIVENNAME1`, `GIVENNAME2` | 0.000 | 0.889–1.000 | +0.94 (avg) |
| `LASTNAME1`, `LASTNAME2`, `LASTNAME3` | 0.000 | 0.800–1.000 | +0.93 (avg) |
| `BUILDING`, `CITY`, `POSTCODE`, `SECADDRESS` | 0.000 | 1.000 | +1.000 |
| `COUNTRY`, `STATE`, `SEX`, `PASS` | 0.000 | 0.000 | 0.000 |

Three groups emerge: (i) **shared strengths** — structured identifiers
covered by both pipelines reach ≥0.84 recall on both engines, with
EMAIL/IP/SOCIALNUMBER tied at 1.0; (ii) **OPF-only wins** — every free-text
class involving person names, addresses, or generic dates flips from 0
to ≥0.8 recall; (iii) **shared blind spots** — `COUNTRY`, `STATE`,
`SEX`, `PASS` are missed by both, suggesting these are dataset-specific
labels (e.g. `SEX` is gender as a PII attribute) that neither system
considers identifying.

## 6. Discussion

### 6.1 Why does `builtin` underperform here?

The proprietary `en_ner_en` model is a small spaCy NER head trained on the
pleno entity taxonomy (PERSON, ADDRESS, ORGANIZATION, DATE_OF_BIRTH,
BANK_ACCOUNT). The AI4Privacy gold spans use a different decomposition
(`LASTNAME1` vs `LASTNAME2` for first/last name positions; separate
`STREET`/`SECADDRESS`/`BUILDING`/`CITY` instead of one ADDRESS span). When
the IoU criterion is strict (0.5), a single pleno PERSON span will not
match the dataset's two consecutive `GIVENNAME` + `LASTNAME` spans, and
vice versa. This is a real production gap — JP-first training data
covers neither English given/family name distributions nor AI4Privacy's
fine-grained address subspans — but the magnitude here is also partly an
artifact of taxonomy mismatch.

### 6.2 Latency-quality tradeoff

OPF's 2.2 s/doc on CPU is incompatible with interactive workloads (the
LLM proxy budgets <100 ms for preprocessing). The model card reports
≈30 ms on a single A100 GPU, which would close the gap entirely; we
defer the empirical GPU number to a follow-up that runs on a RunPod A100
or H100 pod. Until that lands, the operational recommendation is:

* **Latency-bound traffic (Japanese proxy hot path, default):** `builtin`
* **Accuracy-bound traffic (English-heavy, batch redaction, audit logs):**
  `openai-privacy-filter`
* **Secret detection (API keys, credentials):** `openai-privacy-filter`
  is currently the only path — `builtin` has no `SECRET` class.

### 6.3 Threats to validity

* **Sample size (n = 50)** is small. The 95% Wilson confidence interval
  on OPF F1 = 0.847 is roughly ±0.06; on `builtin` F1 = 0.319 it is ±0.07.
  These intervals do not overlap, so the ranking is robust, but
  individual per-label numbers in §5.3 should be read as directional.
  A 300-document run is in progress and will be appended.
* **No Japanese coverage.** As enumerated in §3, the dataset has zero
  Japanese rows. For a JP-first product, this is not a "low-coverage"
  caveat — it is a categorical absence. The headline numbers here
  characterize OPF's lift on English text only; they cannot be
  extrapolated to the production Japanese hot path. We additionally
  verified that the larger sibling release
  `ai4privacy/pii-masking-openpii-1m` (1.4 M rows) is also Japanese-free
  (23 European languages only), so the AI4Privacy ecosystem as a whole
  cannot answer the JP question. The remaining options for JP
  evaluation are pleno's internal benchmark corpora under
  `packages/training/data/benchmark/v0.13.0-held-out/ja/` (with the
  caveat that we authored the labels) or sourcing/building an
  independent JP PII benchmark (§8).
* **Single IoU threshold (τ = 0.5).** Stricter thresholds (0.75, 1.0)
  would penalize span-boundary drift; we expect both engines to degrade
  but OPF's BIOES Viterbi decoder is designed for boundary stability and
  should degrade more gracefully.
* **No batching.** OPF supports batched inference; our latency numbers
  are the worst-case single-call regime.
* **CPU-only.** GPU latency is interpolated from the OPF model card, not
  measured.
* **No fine-tuning.** OPF supports task-specific fine-tuning; we
  evaluate the off-the-shelf checkpoint. A fine-tune on JP-first data
  could close the language gap and is in scope for follow-up work.

## 7. Reproducibility

All artifacts (script, raw output JSON, this document) live in this
repository. To re-run on the same dataset and configuration:

```bash
pip install "pleno-anonymize[openai] @ git+https://github.com/plenoai/pleno-anonymize"
pip install datasets

uv run --with datasets python packages/sdk/scripts/eval_pii_masking_300k.py \
  --engines builtin openai-privacy-filter \
  --language English \
  --pleno-language en \
  --limit 50 \
  --iou 0.5 \
  --opf-device cpu \
  --output output/pii-300k-eval-en-50.json
```

The script is deterministic given the HF streaming iterator's order; raw
output JSON for the headline result is `output/pii-300k-eval-en-50.json`
(gitignored — regenerate with the command above).

## 8. Conclusion

OpenAI Privacy Filter substantially outperforms our in-house Presidio +
spaCy EN NER pipeline on a recognized **English** PII benchmark, at the
cost of ~40× CPU latency. The result justifies shipping OPF as an opt-in
backend (now done in PR #140) but not making it the default — the
latency profile is incompatible with the LLM proxy hot path until GPU
inference is on the operational menu.

**The result does not yet speak to pleno's primary use case.** Because
`ai4privacy/pii-masking-300k` contains no Japanese rows (§3), this
report characterizes only OPF's English lift. The decisive comparison
for a JP-first product remains open. Concrete follow-up, in priority
order:

1. **Run on pleno's internal held-out JP corpora**
   (`packages/training/data/benchmark/v0.13.0-held-out/ja/`). This is
   now the only path to a JP comparison, because we verified that
   `ai4privacy/pii-masking-openpii-1m` (1.4 M rows) is also a
   European-only release (23 languages, none Asian). The internal
   corpora carry the caveat that we defined the labels, so OPF is at a
   structural disadvantage; we will mitigate by scoring label-agnostic
   spans (same protocol as §4) and by spot-checking OPF false positives
   for whether they are *correct masks we did not annotate*.
2. **Replicate at tighter IoU thresholds (0.75, 1.0)** to characterize
   boundary stability. OPF's BIOES Viterbi decoder is built for span
   coherence; we expect its advantage to widen.
3. **Measure OPF GPU latency on RunPod** to validate the model card's
   ~30 ms/doc claim and quantify the operational latency-quality
   frontier.
4. **Source or build an independent JP PII benchmark.** The absence of
   any third-party JP-labeled PII corpus in the obvious places
   (AI4Privacy, common HF leaderboards) is itself a finding worth
   acting on — building or commissioning one would benefit the
   community and unblock honest JP evaluations beyond our own labels.
5. **Explore fine-tuning OPF on JP-first data** to potentially retire
   the dual-backend architecture once (1) lands and confirms the gap.

## References

\[1\] OpenAI. *Introducing OpenAI Privacy Filter.* April 22, 2026.
[openai.com/index/introducing-openai-privacy-filter](https://openai.com/index/introducing-openai-privacy-filter/).
Code: [github.com/openai/privacy-filter](https://github.com/openai/privacy-filter).
Weights: [huggingface.co/openai/privacy-filter](https://huggingface.co/openai/privacy-filter).
Model card: [cdn.openai.com/.../OpenAI-Privacy-Filter-Model-Card.pdf](https://cdn.openai.com/pdf/c66281ed-b638-456a-8ce1-97e9f5264a90/OpenAI-Privacy-Filter-Model-Card.pdf).

\[2\] AI4Privacy. *pii-masking-300k.* HuggingFace Datasets.
[huggingface.co/datasets/ai4privacy/pii-masking-300k](https://huggingface.co/datasets/ai4privacy/pii-masking-300k).

\[3\] pleno-anonymize PR #140: *add OpenAI Privacy Filter (OPF) engine + ai4privacy benchmark.*
[github.com/plenoai/pleno-anonymize/pull/140](https://github.com/plenoai/pleno-anonymize/pull/140).
