# pleno-pii-scanner

CLI that detects Japanese PII in repository contents, commit history, and staged hunks.

## Setup

Run straight from PyPI — no clone, no `uv sync`:

```sh
uvx pleno-pii-scanner --help
```

The `ja_ner_ja` spaCy model is downloaded into the uvx-managed environment on first NER invocation. To pin a persistent install, use `uv tool install pleno-pii-scanner` instead. Workspace contributors get the model wheel preinstalled via `uv sync` (it lives in the `dev` dependency group).

### Higher-precision HF backend (opt-in)

For DLP-grade workloads where false-positive `<ORGANIZATION>` masks are unacceptable, opt into the HuggingFace token-classification backend (model `model/v0.13.0`, `0xhikae/ja-ner-onnx@v0.13.0`). It applies a per-label confidence floor (default `ORGANIZATION=0.99`) — overall F1 0.452 → 0.701 vs the spaCy baseline on the `v0.12.0/ja` adversarial corpus.

```sh
PLENO_PII_SCANNER_BACKEND=hf \
  uvx --with 'pleno-pii-scanner[hf]' pleno-pii-scanner dir <path>
```

Tunables:

- `PLENO_PII_SCANNER_THRESHOLDS=ORGANIZATION=0.99,PERSON=0.0` — per-label confidence floor (default ORG=0.99).
- `PLENO_PII_SCANNER_HF_MODEL` / `PLENO_PII_SCANNER_HF_REVISION` — pin to a custom HF Hub repo / revision (default `0xhikae/ja-ner-onnx@v0.13.0`).

The HF backend adds ~600 MB of torch + transformers; the default install stays lightweight.

## Subcommands

```sh
uvx pleno-pii-scanner dir <path>                # walk a directory
uvx pleno-pii-scanner git <path>                # working tree plus commit history
uvx pleno-pii-scanner github <owner>/<repo>     # shallow clone, then scan
uvx pleno-pii-scanner github --org <org>        # enumerate org repos via gh CLI, then scan all
uvx pleno-pii-scanner baseline <path>           # write current findings as a suppression list
uvx pleno-pii-scanner protect                   # scan only staged hunks for pre-commit hooks
```

## Local vs. offload

Default mode runs Presidio, spaCy NER, and regex on this machine. Pass `--base-url` to offload the same pipeline to a remote pleno-anonymize endpoint.

```sh
uvx pleno-pii-scanner dir ./my-repo --base-url https://pleno-anonymize.fly.dev
PLENO_BASE_URL=... uvx pleno-pii-scanner dir ./my-repo
uvx pleno-pii-scanner dir ./my-repo --base-url ... --api-key "$PLENO_API_KEY"
```

Both modes return the same entity set. Git history scans always use regex only, since per-line NER is not worth the cost on short diff lines.

## Detected entities

NER from `ja_ner_ja` plus Presidio: `PERSON` `ADDRESS` `ORGANIZATION` `DATE_OF_BIRTH` `BANK_ACCOUNT`

Regex plus checksum: `PHONE_NUMBER` `MY_NUMBER` `MY_NUMBER_CORPORATE` `CREDIT_CARD` `PASSPORT` `DRIVER_LICENSE` `HEALTH_INSURANCE` `RESIDENCE_CARD` `POSTAL_CODE` `EMAIL_ADDRESS` `IP_ADDRESS` `URL`

`URL`, `HEALTH_INSURANCE`, and `DRIVER_LICENSE` are excluded from the default profile because they fire too often in source repos. Pass `--entities ALL` to include them, or `--entities PHONE_NUMBER,EMAIL_ADDRESS` to scan a specific subset.

## Verification

Each finding carries one of three labels.

- `passed` — checksum validated by Luhn, My Number, or corporate-number rules, or a contextual keyword sits within range.
- `failed` — checksum failed; likely a false positive.
- `unverified` — no validator matched and no contextual keyword was found.

`--only-verified` keeps `passed` only.

## Output

| `--report-format` | Use case |
|---|---|
| `human` default | colorized table on stdout |
| `json` | machine-readable |
| `sarif` | SARIF 2.1.0 for GitHub Code Scanning |

`--report-path FILE` writes to a file. Exit code is `0` for no findings, `1` when findings are present, `2` for usage errors.

## Suppression

A `.plenoignore` file at the repo root is read automatically.

```
docs/samples/**          # path glob in gitignore syntax
PHONE_NUMBER             # entity-wide
finding:7a3b8c9d         # specific finding fingerprint
```

Inline directives:

```py
SUPPORT_PHONE = "0120-123-456"  # pleno:ignore PHONE_NUMBER
EXAMPLE_EMAIL = "user@example.com"  # pleno:ignore
```

`pleno-pii-scanner baseline` writes a fingerprint list of current findings; passing `--baseline FILE` later suppresses those known findings.

## Key flags

| Flag | Default | Role |
|---|---|---|
| `--entities` | default profile | restrict detection set, `PHONE,EMAIL` or `ALL` |
| `--language` | `ja` | analysis language, `ja` or `en` |
| `--base-url` | unset | offload to a remote pleno-anonymize |
| `--api-key` | unset | Bearer token for offload |
| `--concurrency` | 8 | parallel HTTP requests in offload mode |
| `--include` / `--exclude` | unset | gitignore-style file filters |
| `--max-file-size` | 1 MB | files larger than this are skipped |
| `--only-verified` | off | keep `passed` findings only |
| `--report-format` | `human` | `human`, `json`, or `sarif` |
| `--baseline` | unset | fingerprint JSON of known findings to suppress |

`.gitignore`, a built-in skip list for `.git`, `node_modules`, `.venv`, `dist`, `build`, `vendor`, and similar directories, and a NUL-byte binary check are all on by default.
