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

### Local (default) vs cloud offload

By default, `pleno-scan` runs **locally** with the same Presidio + spaCy NER (`ja_ner_ja`) + regex pipeline that powers the pleno-anonymize server. The ML model is required: it ships as a workspace dependency and `uv sync` installs it automatically. Local scans detect free-text PII (`PERSON`, `ADDRESS`, `ORGANIZATION`) in addition to all the regex-backed entities.

Pass `--base-url` (or set `PLENO_BASE_URL`) to **offload** the same pipeline to a remote pleno-anonymize endpoint — useful when you don't want to load the model locally (e.g. lightweight CI runners) or when scanning a huge repo from a workstation.

```sh
# Local (default): NER + regex, single-process, model loaded once
uv run pleno-scan dir ./my-repo

# Offload to a hosted endpoint
uv run pleno-scan dir ./my-repo --base-url https://pleno-anonymize.fly.dev

# CI-friendly env var
PLENO_BASE_URL=https://pleno-anonymize.fly.dev pleno-scan dir ./my-repo

# Auth (if your endpoint requires it)
uv run pleno-scan dir ./my-repo \
    --base-url https://internal.example.com \
    --api-key "$PLENO_API_KEY"

# Throttle parallel HTTP requests in offload mode
uv run pleno-scan dir ./my-repo --base-url ... --concurrency 4
```

| Mode | Where compute runs | Network | Memory | Use when |
|---|---|---|---|---|
| Local (default) | This machine | none | model loaded once (~200MB) | normal use |
| Cloud (`--base-url`) | Remote pleno-anonymize | required | none | thin CI runners, very large scans |

Both modes return the same entity set; the only difference is *where* the model runs. Git history scanning always uses regex-only matching (per-line NER is wasteful for short diff lines).

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
