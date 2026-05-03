# pleno-anonymize

Japanese-first PII anonymization. Two surfaces share the same NER and Presidio pipeline.

- **`pleno-anonymize` server** — HTTP service exposing `/api/analyze`, `/api/redact`, and OpenAI / Anthropic / Gemini proxies.
- **`pleno-pii-scanner` CLI** — gitleaks- and trufflehog-style scanner that walks repositories and commit history for PII.

Endpoint: https://pleno-anonymize.fly.dev with API reference at `/docs`.
Playground: https://plenoai.com/pleno-anonymize/playground

## Use the service over HTTP

```bash
curl -X POST https://pleno-anonymize.fly.dev/api/analyze \
  -H "content-type: application/json" \
  -d '{"text":"連絡先 山田太郎 090-1234-5678","language":"ja"}'
```

Routing through an LLM proxy masks PII before the request reaches the provider, then restores the original values in the response.

| Endpoint | Upstream |
|---|---|
| `POST /api/openai/*` | OpenAI Chat Completions / Responses |
| `POST /api/anthropic/*` | Anthropic Messages |
| `POST /api/gemini/*` | Google Gemini |

## Scan repositories with the CLI

No clone required — run straight from PyPI with `uvx`:

```bash
uvx pleno-pii-scanner dir ./my-repo           # directory walk
uvx pleno-pii-scanner git ./my-repo           # working tree plus commit history
uvx pleno-pii-scanner github owner/repo       # shallow clone, then scan
uvx pleno-pii-scanner protect                 # pre-commit guard on staged hunks
```

The `ja_ner_ja` spaCy model is downloaded on first NER run, so the initial invocation is slower. Subsequent runs reuse the cached install.

To offload analysis to a remote pleno-anonymize, pass `--base-url`:

```bash
uvx pleno-pii-scanner dir ./my-repo --base-url https://pleno-anonymize.fly.dev
```

Full reference at [`packages/pii-scanner/README.md`](packages/pii-scanner/README.md).

## Detected PII

| Class | Backend | Entities |
|---|---|---|
| Free text | spaCy NER `ja_ner_ja` and Presidio | `PERSON` `ADDRESS` `ORGANIZATION` `DATE_OF_BIRTH` `BANK_ACCOUNT` |
| Structured | regex plus checksum for Luhn, My Number, and corporate number | `PHONE_NUMBER` `MY_NUMBER` `MY_NUMBER_CORPORATE` `CREDIT_CARD` `PASSPORT` `DRIVER_LICENSE` `HEALTH_INSURANCE` `RESIDENCE_CARD` `POSTAL_CODE` `EMAIL_ADDRESS` `IP_ADDRESS` `URL` |

Server and CLI load the same recognizer registry, so identical input yields identical entity sets.

## Self-host

```bash
docker build -t pleno-anonymize .
docker run -p 8080:8080 pleno-anonymize
```

`fly.toml` is included. `flyctl deploy --local-only` ships the same image to fly.io.

## License

[AGPL-3.0](LICENSE) · [Privacy Policy](docs/PRIVACY.md) · [DPIA](docs/DPIA.md)
