# pleno-anonymize

Japanese-first PII analysis and redaction. The repository ships three artifacts that share a single recognizer registry and NER model:

- **`pleno-anonymize` server** — HTTP API exposing `/api/analyze`, `/api/redact`, and OpenAI / Anthropic / Gemini proxies that mask PII before forwarding upstream.
- **`pleno-anonymize` Python package** — SDK + CLI (`uvx pleno-anonymize scan .`) wrapping the same API. See [`packages/sdk`](packages/sdk).
- **`pleno_anonymize_ja` / `pleno_anonymize_en` models** — spaCy NER models trained from this repository's training pipeline.

Endpoint: https://pleno-anonymize.fly.dev (API reference at `/docs`).
Playground: https://plenoai.com/pleno-anonymize/playground

For scanning SaaS sources or filesystems for PII, use [`pleno-dlp`](https://github.com/plenoai/pleno-secret-scanner) — it talks to this server's `/api/analyze` endpoint over HTTP.

## Use the CLI

```bash
# one-shot scan via uvx (no install) — fail CI on any finding
uvx pleno-anonymize scan . --fail-on-findings

# analyze / redact stdin in-process (downloads the NER wheel on first run)
echo "連絡先 山田太郎 090-1234-5678" | uvx pleno-anonymize analyze --language ja
echo "連絡先 山田太郎 090-1234-5678" | uvx pleno-anonymize redact  --language ja
```

Defaults to local execution (Presidio + `pleno_anonymize_{ja,en}` in-process). Add `--base-url https://pleno-anonymize.fly.dev` to offload to the hosted server. Full flag reference: [`packages/sdk`](packages/sdk).

## Use the API

```bash
curl -X POST https://pleno-anonymize.fly.dev/api/analyze \
  -H "content-type: application/json" \
  -d '{"text":"連絡先 山田太郎 090-1234-5678","language":"ja"}'
```

Routing chat traffic through the LLM proxy masks PII before the request reaches the upstream provider, then restores the original values in the response.

| Endpoint | Upstream |
|---|---|
| `POST /api/openai/*` | OpenAI Chat Completions / Responses |
| `POST /api/anthropic/*` | Anthropic Messages |
| `POST /api/gemini/*` | Google Gemini |

## Use the SDK

```python
from pleno_anonymize import PlenoAnonymize, scan_paths

# default: local engine, auto-downloads pleno_anonymize_ja on first call
engine = PlenoAnonymize()
engine.analyze("山田太郎 090-1234-5678", language="ja")
engine.redact("Contact john@example.com", language="en")

# walk paths, aggregate findings per entity
summary = scan_paths(engine, ["src", "docs"], language="ja")
print(summary.by_entity, summary.total_findings)

# remote mode — same surface, no local model footprint
PlenoAnonymize(base_url="https://pleno-anonymize.fly.dev").analyze("...")
```

Full API surface: [`packages/sdk`](packages/sdk).

## Detection backends

| Engine | Install | Speed | When |
|---|---|---|---|
| `builtin` (default) | `pip install pleno-anonymize` | ~50 ms/doc CPU | Japanese-first, regex + checksum for structured IDs, slim deps |
| `openai-privacy-filter` | `pip install pleno-anonymize 'opf @ git+https://github.com/openai/privacy-filter@main'` | ~2 s/doc CPU, ~30 ms GPU | English-heavy text, secret detection, higher recall |

```bash
# default — builtin Presidio + pleno_anonymize_ja
pleno-anonymize analyze 'Alice Johnson, alice@example.com'

# OPF (downloads ~3GB checkpoint to ~/.opf/privacy_filter on first call)
pleno-anonymize analyze --engine openai-privacy-filter --language en \
  'Alice Johnson, alice@example.com'
```

OPF's 8 native labels normalize into the same `entity_type` taxonomy
(`private_person → PERSON`, `private_email → EMAIL_ADDRESS`, …); `secret`
surfaces as a new `SECRET` class so anonymizer / scanner / proxy stay
backend-agnostic.

### Quality

Both models are spaCy tok2vec NER trained on `ai4privacy/pii-masking-300k` (EN) and `0xhikae/pii-masking-300k-ja` (JA). In-distribution F1 ≈ 0.96–0.97; on real-text news (CoNLL-2003 EN F1 ≈ 0.57, stockmark JP Wikipedia F1 ≈ 0.47) the models trail spaCy's `*_core_web_lg` baselines because they are tuned for form-/record-style PII, not narrative prose. For higher recall on English prose, use the `openai-privacy-filter` engine. Full numbers, CIs, and methodology: [`docs/benchmark-pleno-anonymize-en.md`](docs/benchmark-pleno-anonymize-en.md), [`docs/benchmark-pleno-anonymize-ja.md`](docs/benchmark-pleno-anonymize-ja.md).

## Detected entities

| Class | Backend | Entities |
|---|---|---|
| Free text | spaCy NER `pleno_anonymize_ja` plus Presidio | `PERSON` `ADDRESS` `ORGANIZATION` `DATE_OF_BIRTH` `BANK_ACCOUNT` |
| Structured | regex plus checksum (Luhn, My Number, corporate number) | `PHONE_NUMBER` `MY_NUMBER` `MY_NUMBER_CORPORATE` `CREDIT_CARD` `PASSPORT` `DRIVER_LICENSE` `HEALTH_INSURANCE` `RESIDENCE_CARD` `POSTAL_CODE` `EMAIL_ADDRESS` `IP_ADDRESS` `URL` |
| OPF (opt-in) | `openai/privacy-filter` 1.5B (50M active MoE) | `PERSON` `ADDRESS` `EMAIL_ADDRESS` `PHONE_NUMBER` `URL` `DATE_OF_BIRTH` `BANK_ACCOUNT` `SECRET` |

## Repository layout

| Path | What it is |
|---|---|
| `server/` | FastAPI service — analyze / redact endpoints + LLM proxies |
| `packages/sdk/` | Python SDK + `pleno-anonymize` CLI (analyze / redact / scan); bundles the Presidio recognizer registry under `pleno_anonymize.recognizers` |
| `packages/training/` | spaCy / Hugging Face training pipeline for `pleno_anonymize_ja` and `pleno_anonymize_en` |
| `packages/models/` | Trained NER model artifacts |
| `packages/wasm-tokenizer/` | Rust tokenizer compiled to WASM for browser-side preprocessing |
| `website/` | Vite + React playground hosted at plenoai.com |

## Self-host

```bash
docker build -t pleno-anonymize .
docker run -p 8080:8080 pleno-anonymize
```

`fly.toml` is included. `flyctl deploy --local-only` ships the same image to fly.io.

## License

[AGPL-3.0](LICENSE) · [Privacy Policy](docs/PRIVACY.md) · [DPIA](docs/DPIA.md)
