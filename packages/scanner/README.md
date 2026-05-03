# pleno-scan

PII scanner for source repositories. Japanese-first. gitleaks/trufflehog UX.

## Why

`pleno-anonymize` redacts PII at request time via a proxy. `pleno-scan`
catches PII **before** it reaches a remote repo or LLM — running locally,
in CI, or as a pre-commit hook.

## Install (within this monorepo)

```sh
uv sync
```

## Usage

```sh
# Scan a directory
uv run pleno-scan dir ./my-repo

# Scan local git repo, including history
uv run pleno-scan git ./my-repo

# Clone-and-scan a public GitHub repo (shallow)
uv run pleno-scan github octocat/Hello-World

# Scan every repo in an org (requires gh CLI)
uv run pleno-scan github --org my-company

# Pre-commit guard (in .git/hooks/pre-commit or lefthook config)
uv run pleno-scan protect

# Capture current findings as a baseline so they stop appearing
uv run pleno-scan baseline ./my-repo --out .plenoignore-baseline.json
```

### Output formats

- `--report-format human` (default) — colorized table
- `--report-format json` — machine-readable
- `--report-format sarif` — upload to GitHub Code Scanning

### Verification

Each finding is annotated with one of:

- `passed` — checksum-validated (Luhn / My Number / corp number) **or**
  contextual keyword nearby
- `failed` — checksum failed (likely false positive)
- `unverified` — no validator and no context boost

Use `--only-verified` to suppress unverified/failed findings (trufflehog-style).

### Suppressing findings

`.plenoignore` (gitleaks-style):

```
docs/samples/**         # path glob
PHONE_NUMBER            # entity-wide
finding:7a3b8c9d        # specific finding fingerprint
```

Inline:

```py
SUPPORT_PHONE = "0120-123-456"  # pleno:ignore PHONE_NUMBER
EXAMPLE_EMAIL = "noreply@example.com"  # pleno:ignore
```

### Exit codes

- `0` — no findings (or `--exit-zero`)
- `1` — findings present
- `2` — usage error

## Detected entities

Japanese: `PHONE_NUMBER`, `MY_NUMBER`, `CREDIT_CARD`, `PASSPORT`,
`DRIVER_LICENSE`, `IP_ADDRESS`, `EMAIL_ADDRESS`, `MY_NUMBER_CORPORATE`,
`HEALTH_INSURANCE`, `RESIDENCE_CARD`, `POSTAL_CODE`, `URL`, `BANK_ACCOUNT`.

NER-based entities (`PERSON`, `ADDRESS`, `ORGANIZATION`) require the deep
mode (planned).

## How it stays fast

- Multiprocess regex pass (one worker per CPU core).
- Built-in skip list for noisy directories (`node_modules`, `.git`, etc.).
- `.gitignore`-aware.
- Binary file detection (NUL byte probe).
- 1 MB per-file size cap by default.
- Git history pass uses `--unified=0` and only scans **added** lines.
