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

[AGPL-3.0](../../LICENSE)
