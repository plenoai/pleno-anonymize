# pleno-anonymize

Japanese-first PII analysis and redaction. The repository ships three artifacts that share a single recognizer registry and NER model:

- **`pleno-anonymize` server** — HTTP API exposing `/api/analyze`, `/api/redact`, and OpenAI / Anthropic / Gemini proxies that mask PII before forwarding upstream.
- **`pleno-anonymize` Python package** — SDK + CLI (`uvx pleno-anonymize scan .`) wrapping the same API. See [`packages/sdk`](packages/sdk).
- **`ja_ner_ja` / `en_ner_en` models** — spaCy NER models trained from this repository's training pipeline.

Endpoint: https://pleno-anonymize.fly.dev (API reference at `/docs`).
Playground: https://plenoai.com/pleno-anonymize/playground

For scanning SaaS sources or filesystems for PII, use [`pleno-dlp`](https://github.com/plenoai/pleno-secret-scanner) — it talks to this server's `/api/analyze` endpoint over HTTP.

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

## Detection backends

| Engine | Install | Speed | When |
|---|---|---|---|
| `builtin` (default) | `pip install pleno-anonymize` | ~50 ms/doc CPU | Japanese-first, regex + checksum for structured IDs, slim deps |
| `openai-privacy-filter` | `pip install "pleno-anonymize[openai]"` | ~2 s/doc CPU, ~30 ms GPU | English-heavy text, secret detection, higher recall |

```bash
# default — builtin Presidio + ja_ner_ja
pleno-anonymize analyze 'Alice Johnson, alice@example.com'

# OPF (downloads ~3GB checkpoint to ~/.opf/privacy_filter on first call)
pleno-anonymize analyze --engine openai-privacy-filter --language en \
  'Alice Johnson, alice@example.com'
```

OPF's 8 native labels normalize into the same `entity_type` taxonomy
(`private_person → PERSON`, `private_email → EMAIL_ADDRESS`, …); `secret`
surfaces as a new `SECRET` class so anonymizer / scanner / proxy stay
backend-agnostic.

### Development baselines — AI4Privacy-style PII datasets

`ai4privacy/pii-masking-300k` (validation split, character-IoU ≥ 0.5,
label-agnostic) is the development baseline for the EN NER / recognizer
pipeline. `0xhikae/pii-masking-300k-ja` uses the same schema and protocol
for the Japanese pipeline. The prior self-made benchmark under
`packages/training/data/benchmark/v0.*/` is **frozen as of v0.13.0** and
kept only for historical traceability. See [`docs/benchmark.md`](docs/benchmark.md)
for the full methodology and [`/ner-improve`](.claude/skills/ner-improve/SKILL.md)
for the improvement loop.

English validation split (`ai4privacy/pii-masking-300k`), 50 docs.

| Engine | Precision | Recall | F1 | Latency/doc (CPU) |
|---|---|---|---|---|
| `builtin` | 0.386 | 0.272 | 0.319 | 53 ms |
| `openai-privacy-filter` | **0.915** | **0.788** | **0.847** | 2.2 s |

Japanese validation split (`0xhikae/pii-masking-300k-ja`), 50 docs.

| Engine | Precision | Recall | F1 | Latency/doc (CPU) |
|---|---|---|---|---|
| `builtin` | 0.453 | 0.275 | 0.342 | 55 ms |
| `openai-privacy-filter` | **0.899** | **0.576** | **0.702** | 2.3 s |
| **[`ja_ner_ja-v2-supervised`](https://huggingface.co/0xhikae/ja_ner_ja-v2-supervised)** (300 docs, in-dist) | **0.931** | **0.982** | **0.956** | **43 ms** |
| **[`ja_ner_ja-v2-supervised`](https://huggingface.co/0xhikae/ja_ner_ja-v2-supervised)** (67 docs, OOD synthetic) | 0.710 | 0.823 | 0.762 | 41 ms |

The supervised v2 model was trained on the train split of `0xhikae/pii-masking-300k-ja`, so the 0.956 figure on its validation split is an in-distribution upper bound (treat with appropriate skepticism — splits are disjoint by construction but share generation methodology). The OOD row evaluates against a completely separate synthetic test set the model has never seen, with a different label schema, and is the more honest generalisation estimate. v1 baseline `ja_ner_ja-v2-mechanism` (synthetic-only, F1 0.352) is kept for methodology comparison — see [`docs/benchmark-mechanism-v1.md`](docs/benchmark-mechanism-v1.md) and [`docs/benchmark-supervised-v2.md`](docs/benchmark-supervised-v2.md).

```bash
uv run --with datasets --package pleno-anonymize --extra openai \
  python packages/sdk/scripts/eval_pii_masking_300k.py \
  --engines builtin openai-privacy-filter \
  --dataset ai4privacy/pii-masking-300k \
  --language English --pleno-language en --limit 50 \
  --output output/pii-300k-eval-en-50.json

uv run --with datasets --package pleno-anonymize --extra openai \
  python packages/sdk/scripts/eval_pii_masking_300k.py \
  --engines builtin openai-privacy-filter \
  --dataset 0xhikae/pii-masking-300k-ja \
  --language Japanese --pleno-language ja --limit 50 --opf-device cpu \
  --output output/pii-300k-ja-eval-ja-50.json
```

## Detected entities

| Class | Backend | Entities |
|---|---|---|
| Free text | spaCy NER `ja_ner_ja` plus Presidio | `PERSON` `ADDRESS` `ORGANIZATION` `DATE_OF_BIRTH` `BANK_ACCOUNT` |
| Structured | regex plus checksum (Luhn, My Number, corporate number) | `PHONE_NUMBER` `MY_NUMBER` `MY_NUMBER_CORPORATE` `CREDIT_CARD` `PASSPORT` `DRIVER_LICENSE` `HEALTH_INSURANCE` `RESIDENCE_CARD` `POSTAL_CODE` `EMAIL_ADDRESS` `IP_ADDRESS` `URL` |
| OPF (opt-in) | `openai/privacy-filter` 1.5B (50M active MoE) | `PERSON` `ADDRESS` `EMAIL_ADDRESS` `PHONE_NUMBER` `URL` `DATE_OF_BIRTH` `BANK_ACCOUNT` `SECRET` |

## Repository layout

| Path | What it is |
|---|---|
| `server/` | FastAPI service — analyze / redact endpoints + LLM proxies |
| `packages/sdk/` | Python SDK + `pleno-anonymize` CLI (analyze / redact / scan); bundles the Presidio recognizer registry under `pleno_anonymize.recognizers` |
| `packages/training/` | spaCy / Hugging Face training pipeline for `ja_ner_ja` and `en_ner_en` |
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
