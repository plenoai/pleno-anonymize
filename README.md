# pleno-anonymize

Japanese-first PII anonymization service — **Dev F1 95.8% (ja, in-domain held-out) · Adversarial F1 49.0% (ja, v0.12.0 FP-pressure)**[^adv]

Purpose-built for Japanese PII that generic NER models miss: My Number, full-width phone numbers, Japanese-style addresses. Also provides transparent LLM API proxies with automatic PII redaction.

- **Playground:** https://plenoai.com/pleno-anonymize/playground
- **Benchmark:** https://plenoai.com/pleno-anonymize/benchmark
- **API Docs:** https://anonymize.plenoai.com/docs

## Use Cases

- **PII protection for LLM calls** — Auto-mask PII before sending to OpenAI/Anthropic/Gemini, restore in response
- **Log & data cleaning** — Anonymize internal logs and customer data
- **Compliance** — Pre-process data for APPI (Japan) and GDPR compliance

## Model Performance

Two complementary metrics are tracked. See [Methodology — Dev F1 vs Adversarial F1](#methodology--dev-f1-vs-adversarial-f1) for why they diverge.

| Language | Dev F1 | Dev Precision | Dev Recall | Adversarial F1 (latest) |
|---|---|---|---|---|
| ja (CNN) | 95.8% | 96.0% | 95.6% | **49.0%** (v0.12.0)[^adv] |
| en (Transformer) | 97.9% | 97.3% | 98.6% | 72.5% (v0.4.0) |

### Benchmark Progress (ja, adversarial strict-span F1)

| Benchmark | v0.4.0 | v0.5.0 | v0.12.0 | v0.13.0 |
|---|---|---|---|---|
| Overall F1 | **86.9%** | **86.7%** | 49.0% | _pending #48_ |
| Overall Precision | — | — | 33.0% | _pending #48_ |
| Overall Recall | — | — | 95.2% | _pending #48_ |
| PERSON F1 | 88.5% | 89.6% | 83.7% | _pending #48_ |
| ADDRESS F1 | 84.0% | 89.2% | 88.4% | _pending #48_ |
| ORGANIZATION F1 | 84.5% | 89.5% | 21.6% | _pending #48_ |
| DATE_OF_BIRTH F1 | 91.2% | 71.5% | 46.2% | _pending #48_ |
| BANK_ACCOUNT F1 | 86.9% | 86.1% | 49.0% | _pending #48_ |

### Per-entity Adversarial Precision / Recall (ja, v0.12.0)[^adv]

| Entity | Precision | Recall | F1 |
|---|---|---|---|
| PERSON | 72.0% | 100.0% | 83.7% |
| ADDRESS | 84.0% | 93.3% | 88.4% |
| ORGANIZATION | 12.3% | 89.2% | 21.6% |
| DATE_OF_BIRTH | 30.9% | 91.3% | 46.2% |
| BANK_ACCOUNT | 32.4% | 100.0% | 49.0% |

> spaCy's built-in model (`ja_core_news_lg`) achieves ~60-70% F1 on Japanese PII detection on similar adversarial corpora.

## Methodology — Dev F1 vs Adversarial F1

- **Dev F1** measures performance on a held-out portion of `generated.json` — the same synthetic distribution the model is trained on. It captures upper-bound capacity on in-domain templates.
- **Adversarial F1** (v0.12.0 FP-pressure suite) measures strict-span micro F1 on a curated DLP corpus that mimics real-world docs: OCR/key-value collapse, dense negative documents (88% negative-only), and lexically diverse organization names. It is the production-honest number.
- The two diverge because the FP-pressure suite stresses **precision** under heavy negatives — recall stays high (95%+) while precision collapses on `ORGANIZATION` and `BANK_ACCOUNT` (12% / 32%). Dev F1 cannot surface this because its negative density and entity diversity are far lower.
- Adversarial scores are emitted by `pleno_ner_training.evaluate_benchmark` and persisted at [`packages/training/data/benchmark/v0.12.0/ja/scores.json`](packages/training/data/benchmark/v0.12.0/ja/scores.json) (`pleno_ner` entry). Treat this file as the canonical source of truth; Dev F1 lives in the spaCy training meta and is reported for capacity tracking only.

[^adv]: Source: [`packages/training/data/benchmark/v0.12.0/ja/scores.json`](packages/training/data/benchmark/v0.12.0/ja/scores.json) — `pleno_ner` entry, strict-span micro F1, 500 docs (440 negative). v0.13.0 row is reserved for the post-#48 retrain.

## Quick Start

```bash
curl -X POST https://anonymize.plenoai.com/api/analyze \
  -H "Content-Type: application/json" \
  -d '{"text": "John Doe lives at 123 Main St. Email: john@example.com", "language": "en"}'
```

Or try the [Playground](https://plenoai.com/pleno-anonymize/playground) in your browser.

## Supported Entities

### NER Model (ja/en)
| Entity | Description |
|---|---|
| `PERSON` | Person names |
| `ADDRESS` | Addresses |
| `ORGANIZATION` | Organization names |
| `DATE_OF_BIRTH` | Date of birth |
| `BANK_ACCOUNT` | Bank account info |

### Pattern-based
| Entity | Description |
|---|---|
| `EMAIL_ADDRESS` | Email addresses |
| `PHONE_NUMBER` | Phone numbers (full-width/half-width) |
| `MY_NUMBER` | My Number (Japanese individual number) |
| `MY_NUMBER_CORPORATE` | Corporate number |
| `CREDIT_CARD` | Credit card numbers |
| `PASSPORT` | Passport numbers |
| `DRIVER_LICENSE` | Driver license numbers |
| `HEALTH_INSURANCE` | Health insurance card numbers |
| `RESIDENCE_CARD` | Residence card numbers |
| `POSTAL_CODE` | Postal codes |
| `IP_ADDRESS` | IP addresses |
| `URL` | URLs |

## API

### `POST /api/analyze` — PII Detection

```bash
curl -X POST https://anonymize.plenoai.com/api/analyze \
  -H "Content-Type: application/json" \
  -d '{"text": "John Doe, phone: 090-1234-5678", "language": "en"}'
```

### `POST /api/redact` — PII Redaction

```bash
curl -X POST https://anonymize.plenoai.com/api/redact \
  -H "Content-Type: application/json" \
  -d '{"text": "John Doe, phone: 090-1234-5678", "language": "en"}'
```

### LLM API Proxy

Automatically masks PII in requests before forwarding to LLM APIs, then restores original values in responses.

| Endpoint | Upstream API |
|---|---|
| `POST /api/openai/*` | OpenAI (Chat Completions & Responses API) |
| `POST /api/anthropic/*` | Anthropic Messages API |
| `POST /api/gemini/*` | Google Gemini API |

```bash
curl -X POST https://anonymize.plenoai.com/api/openai/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -d '{"model": "gpt-4o", "messages": [{"role": "user", "content": "Tell me about John Doe"}]}'
```

## Self-hosting

```bash
docker build -t pleno-anonymize .
docker run -p 8080:8080 pleno-anonymize
```

## License

[AGPL-3.0](LICENSE) / [Privacy Policy](docs/PRIVACY.md) / [DPIA](docs/DPIA.md)
