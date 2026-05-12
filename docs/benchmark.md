# Backend Comparison on ai4privacy/pii-masking-300k

**Last updated:** 2026-05-12
**Authors:** pleno-anonymize team
**Status:** Initial release. Sample size will grow as additional runs land
(see [§7](#7-reproducibility)).

## Abstract

We compare two interchangeable detection backends shipped with
`pleno-anonymize` — the default `builtin` engine (Microsoft Presidio +
in-house spaCy NER `en_ner_en`) against `openai-privacy-filter` (OPF, the
open-source `openai/privacy-filter` checkpoint, 1.5B parameters / 50M active
mixture-of-experts) [\[1\]](#references) — on the English validation split of
`ai4privacy/pii-masking-300k` [\[2\]](#references). Using label-agnostic
character-IoU ≥ 0.5 span matching, OPF reaches **F1 = 0.847** (P = 0.915,
R = 0.788) against `builtin`'s **F1 = 0.319** (P = 0.386, R = 0.272) on a
50-document sample, at roughly **40× higher per-document latency on CPU**
(2.2 s vs 53 ms). The gap is concentrated in two regions: (i) free-text
person names (`LASTNAME{1,2,3}`, `GIVENNAME{1,2}`) where the in-house EN NER
is not trained on the AI4Privacy label scheme; and (ii) locale-specific
structured identifiers (`POSTCODE`, `STREET`, `BUILDING`, `SECADDRESS`) for
which we currently ship no recognizer. We argue the result motivates a
**tiered routing policy** rather than wholesale replacement: keep `builtin`
on the latency-sensitive Japanese hot path, and offer OPF as an opt-in
backend for accuracy-sensitive or English-heavy traffic.

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

**`ai4privacy/pii-masking-300k`** [\[2\]](#references), validation split,
streamed and filtered to `language == "English"`. The first 50 examples
that satisfy the filter are retained (deterministic ordering from the HF
streaming iterator). The dataset annotates 27 fine-grained PII classes
(e.g. `LASTNAME1`, `LASTNAME2`, `POSTCODE`, `IP`, `EMAIL`, `USERNAME`,
`TIME`); we use only the character span boundaries and ignore the labels
for scoring (see §4).

| Quantity | Value |
|---|---|
| Sample size, *n* | 50 documents |
| Gold spans | 316 |
| Mean spans / document | 6.32 |
| Distinct gold labels observed | 27 |

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

### 5.1 Headline (n = 50, English, τ = 0.5)

| Engine | Precision | Recall | F1 | Latency / doc | TP | FP | FN |
|---|---:|---:|---:|---:|---:|---:|---:|
| `builtin` | 0.386 | 0.272 | 0.319 | 53 ms | 86 | 137 | 230 |
| `openai-privacy-filter` | **0.915** | **0.788** | **0.847** | 2,203 ms | 249 | 23 | 67 |

OPF dominates on all three quality metrics by a wide margin. The
**precision** gap (+0.53) shows OPF is not buying recall with false
positives; the **recall** gap (+0.52) shows the in-house EN NER simply
does not cover the dataset's vocabulary. The latency gap (≈41×) is
expected: OPF runs a 1.5B-parameter transformer per call.

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
* **Single language (English).** OPF is trained primarily on English
  [\[1\]](#references); pleno's `builtin` is JP-first. AI4Privacy's
  Japanese coverage is sparse, so we cannot run the symmetric experiment
  on this dataset.
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
spaCy EN NER pipeline on a recognized English PII benchmark, at the cost
of ~40× CPU latency. The result justifies shipping OPF as an opt-in
backend (now done in PR #140) but not making it the default — the
latency profile is incompatible with the LLM proxy hot path until GPU
inference is on the operational menu. Future work: (i) replicate on a
larger sample and tighter IoU thresholds; (ii) measure GPU latency on
RunPod; (iii) evaluate the symmetric Japanese case once a comparable
dataset exists; (iv) explore fine-tuning OPF on JP-first data to retire
the dual-backend architecture entirely.

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
