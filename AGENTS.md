pleno-anonymizeは、個人情報を匿名化するためのサービスです
適宜pushしてバージョン管理すること
use runpod for training (use runpod mcp: `mcp__runpod__*` — `create-pod`, `start-pod`, `get-pod`, `delete-pod`, etc.)
do not train on local machine

<!-- agentops:dreaming:start -->
# Project memory (managed by agentops dreaming — do not edit between markers)

## ai4privacy-license-constraint ([[ai4privacy-license-constraint]])
ai4privacy/pii-masking-300k は非商用・派生物公開に書面許諾が必要 — 訓練には使えず評価専用

## en-model-030-release-pending ([[en-model-030-release-pending]])
EN NER 0.3.0 (pii-300k F1 0.5812, +79%) は PR #288 — HF wheel アップロード完了まで未マージ

## face-redaction-feature ([[face-redaction-feature]])
Face redaction via OpenCV YuNet merged in PR #195; README banner uses presidio OCR redaction

## readme-redact-banner ([[readme-redact-banner]])
README before/after presidio OCR redaction banner + reproducible generator script
<!-- agentops:dreaming:end -->
