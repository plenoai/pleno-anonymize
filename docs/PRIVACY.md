# Privacy Policy

Last updated: 2026-03-31

## 1. Data Processed

The pleno-anonymize API processes the following data:

- Text and images submitted by users
- HTTP request metadata (method, path, status code, processing time)

**Data NOT processed:**
- API keys or tokens (proxy endpoints relay headers as-is)
- IP addresses (not recorded at the application layer)

## 2. Processing Purpose

- PII (Personally Identifiable Information) detection and masking
- Secure proxy relay to LLM APIs

## 3. Data Flow

### analyze/redact Endpoints
- Input text is processed locally using NER models + Presidio
- **No data is sent to external services**
- Only results are returned; input data is not retained

### LLM Proxy Endpoints (openai/anthropic/gemini)
- PII is detected and masked in input text
- Only masked text is sent to the user-specified LLM API
- Placeholders in LLM responses are restored to original values
- PII mappings are discarded from memory when the request completes

### LLM Provider Data Retention
- Each provider's (OpenAI/Anthropic/Google) data retention policy applies
- pleno-anonymize only sends masked data to these providers

## 4. Data Retention

| Data Type | Retention Period |
|---|---|
| Input text | Request duration only (in-memory) |
| PII mapping | Request duration only (in-memory) |
| Access logs | No PII; method/path/status only |

- No persistent database is used
- No caching of input data

## 5. Self-Hosting

pleno-anonymize is open source (Apache-2.0) and can be self-hosted on your own infrastructure. When self-hosted, all data is processed within your infrastructure.

## 6. User Rights

- **Right of access**: No input data is retained, so there is no data to disclose
- **Right to erasure**: Input data is automatically discarded when the request completes
- **Contact**: https://github.com/HikaruEgashira/pleno-anonymize/issues
