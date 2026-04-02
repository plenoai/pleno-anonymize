# pleno-anonymize

Japanese-first PII anonymization service — **F1 96.2% (ja) / 97.9% (en)**

Purpose-built for Japanese PII that generic NER models miss: My Number, full-width phone numbers, Japanese-style addresses. Also provides transparent LLM API proxies with automatic PII redaction.

- **Playground:** https://plenoai.com/pleno-anonymize/playground
- **Benchmark:** https://plenoai.com/pleno-anonymize/benchmark
- **API Docs:** https://anonymize.plenoai.com/docs

## Use Cases

- **PII protection for LLM calls** — Auto-mask PII before sending to OpenAI/Anthropic/Gemini, restore in response
- **Log & data cleaning** — Anonymize internal logs and customer data
- **Compliance** — Pre-process data for APPI (Japan) and GDPR compliance

## Model Performance

| Language | Dev F1 | Precision | Recall | Benchmark (v0.4.0) |
|---|---|---|---|---|
| ja (CNN) | 96.2% | 96.2% | 96.6% | 85.7% |
| en (Transformer) | 97.9% | 97.3% | 98.6% | 72.5% |

### Benchmark Progress (ja)

| Benchmark | v0.2.0 | v0.3.0 | v0.4.0 | v0.5.0 |
|---|---|---|---|---|
| Overall F1 | — | 80.7% | 85.7% | 83.4% |
| PERSON | — | 86.6% | 90.7% | 86.5% |
| ADDRESS | — | 86.7% | 83.6% | 86.0% |
| ORGANIZATION | — | 70.5% | 78.5% | 86.4% |
| DATE_OF_BIRTH | — | 78.2% | 87.6% | 67.6% |
| BANK_ACCOUNT | — | 84.4% | 82.8% | 83.0% |

> Dev F1 measures performance on held-out training data. Benchmark F1 measures on adversarial, real-world-style test sets that progressively increase in difficulty. spaCy's built-in model (`ja_core_news_lg`) achieves ~60-70% F1 on Japanese PII detection.

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
