# pleno-presidio-extras

Optional [Presidio](https://microsoft.github.io/presidio/) custom elements
maintained by pleno. Nothing here ships inside `pleno-anonymize`; install this
package and opt into each element explicitly.

Each element depends only on `presidio-analyzer`, so it works with a vanilla
`AnalyzerEngine` as well as with the `pleno-anonymize` SDK.

```sh
pip install pleno-presidio-extras
```

## JevContextFilter

`JevContextFilter` extends Presidio's `LemmaContextAwareEnhancer`. It preserves
the default lemma enhancement and then evaluates each extracted span with up
to 160 Unicode characters before and after it via TypeSafe's Jev API, removing
confident false positives. Enable it explicitly:

```python
from pleno_anonymize import PlenoAnonymize
from pleno_presidio_extras import JevContextFilter

# Set TYPESAFE_API_KEY in the environment; do not put secrets in source code.
context_filter = JevContextFilter(min_confidence=0.9)
engine = PlenoAnonymize(context_aware_enhancer=context_filter)
findings = engine.analyze("担当者は山田太郎です。", language="ja")
redacted = engine.redact("担当者は山田太郎です。", language="ja")
```

For an existing Presidio setup, pass the same instance to
`AnalyzerEngine(context_aware_enhancer=context_filter, ...)`. It works with
custom recognizers and with both analysis and anonymization consumers. The
SDK's remote and OpenAI Privacy Filter engines reject this option.

**Data transfer:** this opt-in sends unmasked entity text, entity types, and
bounded surrounding context to `https://api.typesafe.ai/v1/systemone`.
The default SDK and hosted service remain unchanged. This extension does not
log or cache input, keys, or remote error bodies; TypeSafe's own processing
and retention terms apply. No additional dependency is required.

Jev chooses `entity`, `false_positive`, or `uncertain`. A candidate is removed
only when `false_positive` wins and **both** its probability and the returned
`confidence` meet `min_confidence` (default `0.9`, configurable in `(0.5, 1]`).
These are distinct quantities in the [TypeSafe confidence contract](https://docs.typesafe.ai/confidence).
Uncertainty, missing/malformed answers, timeouts, and HTTP errors preserve the
original detection. A different sensitive entity type also means keep. The
original Presidio score and offsets are preserved; `score_threshold` retains
its normal Presidio meaning. Filtering happens before Presidio deduplication.

Use `evaluate` for an audit or threshold study without dropping candidates:

```python
# results: list[RecognizerResult] from your existing Presidio analyzer
evaluated = context_filter.evaluate(text, results)
for result in evaluated:
    print(result.start, result.end, result.recognition_metadata["jev"])
```

This returns copies of all candidates, including those marked `keep=False`.
Metadata includes the resolved model, choice, probabilities, confidence, and
threshold for evaluated candidates. Unavailable evaluations have
`status="unavailable", keep=True`. Spans longer than 2,048 characters are kept
without transfer (`status="span_too_long"`). Exact duplicate spans of the same
type share an evaluation; separate occurrences retain their own context.
`context_chars` accepts 1–1,024 characters per side; `timeout` defaults to
10 seconds per request. Requests use serial batches of eight candidates.
Only the bounded text context is sent; Presidio's optional `context` keywords
continue to affect the lemma enhancer locally.

`model="jev-latest"` is the default. Pin a supported model ID for reproducible
evaluation. The default threshold is a policy starting point, not a calibrated
PII accuracy guarantee: measure false positives, retained true entities, and
latency on representative held-out data before relying on removals. Prompt
instructions cannot guarantee resistance to adversarial input.

A reproducible live smoke test sends only checked-in synthetic EN/JA examples:

```sh
dotenvx run -f .dev.vars -- python packages/presidio-extras/scripts/eval_jev_context.py \
  --output /tmp/jev-context-smoke.json
```

It records every decision, including injection examples and a sensitive span
with an incorrect entity label. This is a candidate-level integration smoke
test, not a NER precision/recall benchmark.
