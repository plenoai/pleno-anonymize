# pleno-anonymize

Japanese-first PII analysis and redaction. The repository ships three artifacts that share a single recognizer registry and NER model:

- **`pleno-anonymize` server** — HTTP API exposing `/api/analyze`, `/api/redact`, and OpenAI / Anthropic / Gemini proxies that mask PII before forwarding upstream.
- **`pleno-anonymize` npm package** — TypeScript SDK + CLI (`npx pleno-anonymize scan .`) wrapping the same API. See [`packages/sdk-js`](packages/sdk-js).
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

## Detected entities

| Class | Backend | Entities |
|---|---|---|
| Free text | spaCy NER `ja_ner_ja` plus Presidio | `PERSON` `ADDRESS` `ORGANIZATION` `DATE_OF_BIRTH` `BANK_ACCOUNT` |
| Structured | regex plus checksum (Luhn, My Number, corporate number) | `PHONE_NUMBER` `MY_NUMBER` `MY_NUMBER_CORPORATE` `CREDIT_CARD` `PASSPORT` `DRIVER_LICENSE` `HEALTH_INSURANCE` `RESIDENCE_CARD` `POSTAL_CODE` `EMAIL_ADDRESS` `IP_ADDRESS` `URL` |

## Repository layout

| Path | What it is |
|---|---|
| `server/` | FastAPI service — analyze / redact endpoints + LLM proxies |
| `packages/sdk-js/` | TypeScript SDK + `npx pleno-anonymize` CLI (analyze / redact / scan) |
| `packages/recognizers/` | Pure-Python Presidio recognizer registry (regex + checksum validators) |
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
