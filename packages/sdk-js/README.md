# pleno-anonymize

TypeScript SDK and CLI for the [pleno-anonymize](https://github.com/plenoai/pleno-anonymize) PII detection / redaction API.

The same package ships:

- A small `PlenoAnonymize` client for `analyze` / `redact`
- A filesystem **scanner** that walks paths, reads text files, and reports PII per file
- A CLI exposed as `pleno-anonymize` (run via `npx pleno-anonymize`)

It targets the public endpoint at `https://pleno-anonymize.fly.dev` by default and works against any self-hosted deployment.

## Install

```bash
# one-shot via npx (no install)
npx pleno-anonymize scan .

# or as a dependency
npm i pleno-anonymize
```

Requires Node.js **18+** (uses native `fetch`).

## CLI

```text
pleno-anonymize scan <path...>     # walk paths, detect PII per file
pleno-anonymize analyze [text]     # detect entities in text / stdin / --file
pleno-anonymize redact  [text]     # replace detected PII with <PLACEHOLDERS>
pleno-anonymize health             # ping the API
```

Common flags:

| Flag | Description |
|---|---|
| `--endpoint <url>` | API base URL (env: `PLENO_ANONYMIZE_ENDPOINT`) |
| `--api-key <key>` | Bearer token (env: `PLENO_ANONYMIZE_API_KEY`) |
| `--language ja\|en` | Detection language (default `ja`) |
| `--entities A,B,C` | Restrict to specific entity types |
| `--json` | Emit JSON |
| `--fail-on-findings` | Exit `2` on `scan` when PII is found (CI gate) |
| `--concurrency <n>` | Parallel scan requests (default `4`) |
| `--max-bytes <n>` | Per-file byte cap for `scan` (default `262144`) |
| `--ignore a,b` | Extra directory names to skip |
| `--ext .md,.py` | Restrict scan to extensions |
| `-f, --file <path>` | Read input text from file |

### Examples

```bash
# scan the current repo, fail CI on any finding
npx pleno-anonymize scan . --fail-on-findings

# analyze a Japanese string
echo "山田太郎 090-1234-5678 yamada@example.com" \
  | npx pleno-anonymize analyze --language ja

# redact and pipe to file
npx pleno-anonymize redact -f notes.md > notes.redacted.md

# JSON output for tooling
npx pleno-anonymize scan src --json | jq '.byEntity'
```

## SDK

```ts
import { PlenoAnonymize, scanPaths } from "pleno-anonymize";

const client = new PlenoAnonymize({
  // endpoint: "https://pleno-anonymize.fly.dev",   // default
  // apiKey: process.env.PLENO_ANONYMIZE_API_KEY,    // optional Bearer
  defaultLanguage: "ja",
});

const findings = await client.analyze("山田太郎 090-1234-5678");
// [{ entity_type: "PERSON", start: 0, end: 4, score: 0.9, text: "山田太郎" }, ...]

const { text } = await client.redact("Contact john@example.com");
// "Contact <EMAIL_ADDRESS>"

const summary = await scanPaths(client, ["src", "docs"], {
  language: "ja",
  ignore: ["fixtures"],
  onFile: (file) => {
    if (file.findings.length > 0) console.log(file.path, file.findings.length);
  },
});
console.log(summary.byEntity, summary.totalFindings);
```

### API surface

| Export | Purpose |
|---|---|
| `PlenoAnonymize` | HTTP client (`analyze`, `redact`, `health`) |
| `PlenoAnonymizeError` | Thrown on HTTP / timeout / abort failures |
| `scanFile(client, path, opts)` | Analyze a single file |
| `scanPaths(client, paths, opts)` | Walk paths with concurrency, return `ScanSummary` |
| `Finding`, `FileScanResult`, `ScanSummary`, `Language` | Types |

## Detected entities

Free-text NER (`PERSON`, `ADDRESS`, `ORGANIZATION`, `DATE_OF_BIRTH`, `BANK_ACCOUNT`) and structured / regex+checksum classes (`PHONE_NUMBER`, `MY_NUMBER`, `MY_NUMBER_CORPORATE`, `CREDIT_CARD`, `PASSPORT`, `DRIVER_LICENSE`, `HEALTH_INSURANCE`, `RESIDENCE_CARD`, `POSTAL_CODE`, `EMAIL_ADDRESS`, `IP_ADDRESS`, `URL`).

See the [server README](../../README.md) for the full list and language coverage.

## Exit codes (CLI)

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Usage / runtime error |
| `2` | `scan --fail-on-findings` and findings were detected |

## License

[AGPL-3.0](../../LICENSE)
