# pleno-anonymize

Local-first Japanese PII detection and redaction — SDK + CLI.

The package ships:

- A `PlenoAnonymize` factory that defaults to running Presidio + the spaCy `pleno_anonymize_ja` / `pleno_anonymize_en` models **in-process** (no network at scan time).
- An optional remote mode (`--base-url` / `base_url=`) that talks to a hosted `pleno-anonymize` server — same wire protocol as `https://pleno-anonymize.fly.dev`.
- A filesystem **scanner** (`scan_paths`) that walks paths and reports PII per file.
- The `pleno-anonymize` CLI installed as a `[project.scripts]` entry — run with `uvx pleno-anonymize`, `pipx run pleno-anonymize`, or after `pip install pleno-anonymize`.

## Install

```bash
# one-shot via uvx (no install)
uvx pleno-anonymize scan .

# or as a dependency
uv add pleno-anonymize
pip install pleno-anonymize
```

Requires Python **3.12+**.

The first time you scan a language, the matching NER wheel
(`pleno_anonymize_ja` / `pleno_anonymize_en`, hosted on Hugging Face) is fetched and pip-installed
into the active environment. Pre-install with:

```bash
uvx pleno-anonymize models install ja
uvx pleno-anonymize models install en
```

Or disable auto-install with `--no-auto-download` (falls back to a blank
tokenizer + pattern recognizers — regex/checksum classes still detect, but
free-text NER classes won't).

## CLI

```text
pleno-anonymize scan <path...>     # walk paths, detect PII per file
pleno-anonymize analyze [text]     # detect entities in text / stdin / --file
pleno-anonymize redact  [text]     # replace detected PII with <PLACEHOLDERS>
pleno-anonymize models {install,status}
pleno-anonymize health             # ping --base-url (remote mode only)
```

Common flags:

| Flag | Description |
|---|---|
| `--base-url <url>` | Use a hosted endpoint instead of running locally (env: `PLENO_ANONYMIZE_BASE_URL`) |
| `--api-key <key>` | Bearer token for `--base-url` (env: `PLENO_ANONYMIZE_API_KEY`) |
| `--language ja\|en` | Detection language (default `ja`) |
| `--entities A,B,C` | Restrict to specific entity types |
| `--no-auto-download` | Do not pip-install missing NER wheels (local mode only) |
| `--json` | Emit JSON |
| `--fail-on-findings` | Exit `2` from `scan` when PII is found (CI gate) |
| `--workers <n>` | Parallel scan workers (default `4`) |
| `--max-bytes <n>` | Per-file byte cap for `scan` (default `262144`) |
| `--ignore a,b` | Extra directory names to skip |
| `--ext .md,.py` | Restrict scan to extensions |
| `-f, --file <path>` | Read input text from file |

### Examples

```bash
# scan the current repo locally, fail CI on any finding
uvx pleno-anonymize scan . --fail-on-findings

# analyze a Japanese string with the local model
echo "山田太郎 090-1234-5678 yamada@example.com" \
  | uvx pleno-anonymize analyze --language ja

# same call, but offload to the hosted server
echo "山田太郎 090-1234-5678" \
  | uvx pleno-anonymize analyze \
      --base-url https://pleno-anonymize.fly.dev

# redact and pipe to file
uvx pleno-anonymize redact -f notes.md > notes.redacted.md

# JSON output for tooling
uvx pleno-anonymize scan src --json | jq '.byEntity'
```

## SDK

```python
from pleno_anonymize import PlenoAnonymize, scan_paths

# default: local engine, auto-downloads pleno_anonymize_ja on first call
engine = PlenoAnonymize()
findings = engine.analyze("山田太郎 090-1234-5678", language="ja")
# [Finding(entity_type='PERSON', start=0, end=4, score=0.85, text='山田太郎'), ...]

result = engine.redact("Contact john@example.com", language="en")
# RedactResult(text='Contact <EMAIL_ADDRESS>')

summary = scan_paths(
    engine,
    ["src", "docs"],
    language="ja",
    ignore=["fixtures"],
    on_file=lambda f: f.findings and print(f.path, len(f.findings)),
)
print(summary.by_entity, summary.total_findings)

# remote mode — same surface, no local model footprint
remote = PlenoAnonymize(base_url="https://pleno-anonymize.fly.dev")
remote.analyze("...")
```

### Optional Jev context filter (Presidio extension)

`JevContextFilter` extends Presidio's `LemmaContextAwareEnhancer`. It preserves
the default lemma enhancement and then evaluates each extracted span with up
to 160 Unicode characters before and after it. Enable it explicitly:

```python
from pleno_anonymize import PlenoAnonymize
from pleno_anonymize.presidio import JevContextFilter

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
dotenvx run -f .dev.vars -- python packages/sdk/scripts/eval_jev_context.py \
  --output /tmp/jev-context-smoke.json
```

It records every decision, including injection examples and a sensitive span
with an incorrect entity label. This is a candidate-level integration smoke
test, not a NER precision/recall benchmark.

### API surface

| Export | Purpose |
|---|---|
| `PlenoAnonymize(base_url=None, ...)` | Factory: returns `LocalEngine` (default) or `RemoteEngine` |
| `LocalEngine` | In-process Presidio + spaCy + recognizer registry |
| `RemoteEngine` | HTTP client (stdlib `urllib`) for a hosted server |
| `PlenoAnonymizeError` | Raised by `RemoteEngine` on HTTP / transport failures |
| `scan_file(engine, path, ...)` | Analyze a single file |
| `scan_paths(engine, paths, ...)` | Walk paths with worker pool, return `ScanSummary` |
| `Finding`, `RedactResult`, `FileScanResult`, `ScanSummary` | Dataclasses |

### Environment variables

| Var | Purpose |
|---|---|
| `PLENO_ANONYMIZE_BASE_URL` | Default `--base-url` |
| `PLENO_ANONYMIZE_API_KEY` | Default `--api-key` |
| `NO_COLOR` | Disable ANSI colors in CLI output |

## Detected entities

Free-text NER (`PERSON`, `ADDRESS`, `ORGANIZATION`, `DATE_OF_BIRTH`, `BANK_ACCOUNT`) and structured / regex+checksum classes (`PHONE_NUMBER`, `MY_NUMBER`, `MY_NUMBER_CORPORATE`, `CREDIT_CARD`, `PASSPORT`, `DRIVER_LICENSE`, `HEALTH_INSURANCE`, `RESIDENCE_CARD`, `POSTAL_CODE`, `EMAIL_ADDRESS`, `IP_ADDRESS`, `URL`).

See the [server README](../../README.md) for the full list.

## Exit codes (CLI)

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Usage / runtime error |
| `2` | `scan --fail-on-findings` and findings were detected |

## License

[Apache-2.0](../../LICENSE)
