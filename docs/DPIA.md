# Data Protection Impact Assessment (DPIA)

Last updated: 2026-03-31

## 1. Processing Overview

| Item | Details |
|---|---|
| Service | pleno-anonymize |
| Purpose | PII detection and anonymization in text and images |
| Legal basis | Legitimate interest (GDPR Art. 6(1)(f)) / APPI compliance |
| Data subjects | Users submitting text via API; individuals referenced in the text |
| Data processor | pleno-anonymize operator (or each organization when self-hosted) |

## 2. Necessity and Proportionality

### Purpose
Mitigate the risk of sending personal information to LLMs. PII is detected via NER models and pattern matching, replaced with placeholders, then sent to LLM APIs.

### Alternatives Considered
| Alternative | Assessment |
|---|---|
| Manual masking | Impractical (does not scale, high miss rate) |
| LLM provider DLP | Limited Japanese language support |
| Regex only | Cannot handle context-dependent entities (names, addresses) |

pleno-anonymize achieves high detection rates for Japanese PII through a hybrid NER + pattern matching approach.

## 3. Risk Assessment

### 3.1 PII Leakage from Missed Detection

| Item | Assessment |
|---|---|
| Severity | High |
| Likelihood | Medium (low-score entities, unknown patterns) |
| Mitigation | Minimum recall thresholds, regular benchmarks, regression test suite |

### 3.2 Mapping Leakage

| Item | Assessment |
|---|---|
| Severity | High |
| Likelihood | Low (in-memory only, discarded on request completion) |
| Mitigation | No mapping persistence, no PII in logs |

### 3.3 Data Retention at LLM Providers

| Item | Assessment |
|---|---|
| Severity | Medium (masked data only) |
| Likelihood | Certain (depends on each provider's policy) |
| Mitigation | Only masked data is sent, users are informed |

### 3.4 Data Degradation from False Positives

| Item | Assessment |
|---|---|
| Severity | Low |
| Likelihood | Medium (e.g., 12-digit number patterns) |
| Mitigation | Confidence scores provided, Precision metric monitoring |

## 4. Technical and Organizational Measures

- [x] No persistence of input data (in-memory processing only)
- [x] Structured logs contain no PII
- [x] Input validation (text length limit: 100,000 characters)
- [x] HTTPS enforced (fly.io force_https)
- [x] CORS restrictions (allowed origins only)
- [x] Concurrency limits (hard_limit)
- [ ] Regular accuracy benchmarks (CI integration planned)
- [ ] External security audit (annually recommended)

## 5. Conclusion

pleno-anonymize is a PII protection service designed to minimize data retention (stateless, in-memory processing). Privacy risks are manageable. The primary residual risk is missed detection, addressed through continuous model improvement and benchmarking.
