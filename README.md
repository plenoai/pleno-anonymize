# pleno-anonymize

Japanese-first PII anonymization service — **F1 97.7% (ja) / 97.9% (en)**

Purpose-built for Japanese PII that generic NER models miss: My Number, full-width phone numbers, Japanese-style addresses. Also provides transparent LLM API proxies with automatic PII redaction.

- **Playground:** https://plenoai.com/pleno-anonymize/playground
- **Benchmark:** https://plenoai.com/pleno-anonymize/benchmark
- **API Docs:** https://anonymize.plenoai.com/docs

## Use Cases

- **PII protection for LLM calls** — Auto-mask PII before sending to OpenAI/Anthropic/Gemini, restore in response
- **Log & data cleaning** — Anonymize internal logs and customer data
- **Compliance** — Pre-process data for APPI (Japan) and GDPR compliance

## Model Performance

| Language | F1 | Precision | Recall | Benchmark |
|---|---|---|---|---|
| ja (CNN) | 97.7% | 98.2% | 97.1% | [Details](https://plenoai.com/pleno-anonymize/benchmark) |
| en (Transformer) | 97.9% | 97.3% | 98.6% | [Details](https://plenoai.com/pleno-anonymize/benchmark) |

> spaCy's built-in model (`ja_core_news_lg`) achieves ~60-70% F1 on Japanese PII detection. pleno-anonymize significantly improves accuracy through fine-tuning on Japanese PII-specific data.

## Quick Start

### Docker (recommended)

```bash
docker build -t pleno-anonymize .
docker run -p 8080:8080 pleno-anonymize
```

### Local Development

```bash
# Server
cd server && uv sync && uv run uvicorn src.app:app --port 8080

# Frontend
cd website && npm install && npm run dev
```

### Verify

```bash
curl -X POST http://localhost:8080/api/analyze \
  -H "Content-Type: application/json" \
  -d '{"text": "John Doe lives at 123 Main St. Email: john@example.com", "language": "en"}'
```

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

### Health Checks

| Endpoint | Purpose |
|---|---|
| `GET /health` | Liveness probe |
| `GET /ready` | Readiness probe (NLP models loaded) |

## Deployment & Operations

### Resource Requirements

| Item | Value |
|---|---|
| Memory | Minimum 1GB |
| Docker image | ~1.2GB (includes NER models) |
| Cold start | ~15-30s (initial model loading) |

### Environment Variables

| Variable | Description | Default |
|---|---|---|
| `OPENAI_API_BASE` | OpenAI API base URL | `https://api.openai.com` |
| `ANTHROPIC_API_BASE` | Anthropic API base URL | `https://api.anthropic.com` |
| `GEMINI_API_BASE` | Gemini API base URL | `https://generativelanguage.googleapis.com` |

> Only required for LLM proxy endpoints. The analyze/redact endpoints need no environment variables.

## Data Flow

```
[User] → [pleno-anonymize API]
               │
               ├─ /api/analyze, /api/redact
               │    → Local NER + Presidio for PII detection/redaction
               │    → No external API calls
               │
               └─ /api/openai/*, /api/anthropic/*, /api/gemini/*
                    → PII detection → Mask → LLM API → Restore response
                    → Mapping lives in request memory only (never persisted)
```

- analyze/redact endpoints never send data externally
- Proxy endpoints only send masked text to LLM APIs
- PII mappings are discarded from memory when the request completes

## Project Structure

```
server/          # FastAPI backend
website/         # React frontend (GitHub Pages)
packages/
  models/        # NER models (ja: v0.1.0, en: v0.1.0)
  training/      # Model training pipeline
docs/
  PRIVACY.md     # Privacy policy
  DPIA.md        # Data Protection Impact Assessment
  adr/           # Architecture Decision Records
```

## License & Legal

**AGPL-3.0** — Modifications must be released under the same license.

- Internal use: No AGPL disclosure obligation for modifications
- SaaS distribution: Source code disclosure required when serving over a network

Details: [Privacy Policy](docs/PRIVACY.md) / [DPIA](docs/DPIA.md)
