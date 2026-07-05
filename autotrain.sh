#!/usr/bin/env bash
# autotrain.sh - NER自律改善ループをClaude Codeで実行する
# プロジェクトルートに配置し、ルートから実行すること
#
# Usage:
#   ./autotrain.sh                    # モデル改善: en, 3イテレーション
#   ./autotrain.sh en 5               # モデル改善: 英語, 5イテレーション
#   ./autotrain.sh fr 3               # モデル改善: フランス語, 3イテレーション
#   ./autotrain.sh en 5 --resume      # 前回の続きから再開
#
# 前提:
#   - claude CLI がインストール済み
#   - .env に OPENAI_API_KEY が設定済み
#   - ja は ai4privacy/pii-masking-300k に日本語行が無いため評価不能 (exit 1)

set -euo pipefail

TARGET_LANG="${1:-en}"
MODE="improve"
MAX_ITER="3"
EXIT_CODE=0

if [ "$TARGET_LANG" = "ja" ]; then
  echo "error: ai4privacy/pii-masking-300k に日本語行は無く、このループでは ja を評価できません (.claude/skills/ner-improve/SKILL.md 参照)。en 等サポート言語を指定してください。" >&2
  exit 1
fi

# 引数パース
shift || true
while [[ $# -gt 0 ]]; do
  case "$1" in
    --resume)  MODE="resume"; shift ;;
    *)         MAX_ITER="$1"; shift ;;
  esac
done

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
TRAINING_DIR="$PROJECT_ROOT/packages/training"
LOG_DIR="$TRAINING_DIR/experiments"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
LOG_FILE="$LOG_DIR/autotrain_${TIMESTAMP}.log"

mkdir -p "$LOG_DIR"
touch "$LOG_DIR/log.jsonl"

print_banner() {
  cat <<'BANNER'
╔═══════════════════════════════════════════════╗
║  NER Autonomous Training Loop                 ║
║  Powered by Claude Code                       ║
╚═══════════════════════════════════════════════╝
BANNER
}

print_scores() {
  local label="$1"
  local latest
  latest=$(ls -t "$PROJECT_ROOT"/output/pii-300k-eval-*.json 2>/dev/null | head -1) || true
  if [ -z "$latest" ]; then
    echo "$label: no baseline eval yet"
    return
  fi
  echo "$label ($(basename "$latest")):"
  python3 -c "
import json
with open('$latest') as f:
    data = json.load(f)
for engine, s in data['results'].items():
    print(f'  {engine}: F1={s[\"f1\"]*100:.1f}%  P={s[\"precision\"]*100:.1f}%  R={s[\"recall\"]*100:.1f}%')
" 2>/dev/null || echo "  (scores not parseable)"
}

run_claude() {
  local prompt="$1"
  local step_label="$2"

  echo "=== $step_label ==="
  echo "$prompt" | tee -a "$LOG_FILE"
  echo "---"

  cd "$PROJECT_ROOT"
  claude --dangerously-skip-permissions \
    -p "$prompt" \
    --output-format text \
    2>&1 | tee -a "$LOG_FILE"

  return ${PIPESTATUS[0]}
}

# --- Main ---

print_banner
echo "Language:    $TARGET_LANG"
echo "Mode:        $MODE"
echo "Iterations:  $MAX_ITER"
echo "Log:         $LOG_FILE"
echo "Started:     $(date -Iseconds)"
echo "---"

print_scores "Before"
echo "---"

case "$MODE" in
  improve)
    run_claude "/ner-improve $TARGET_LANG $MAX_ITER" "Model Improvement" || EXIT_CODE=$?
    ;;

  resume)
    PREV_COUNT=$(wc -l < "$LOG_DIR/log.jsonl" | tr -d ' ')
    run_claude "/ner-improve $TARGET_LANG $MAX_ITER

前回の実験ログ ($PREV_COUNT 件) が packages/training/experiments/log.jsonl にあります。
前回の結果を踏まえて、まだ試していないアプローチから改善を続けてください。" \
      "Model Improvement (resume)" || EXIT_CODE=$?
    ;;
esac

echo ""
echo "---"
print_scores "After"
echo "---"
echo "Finished: $(date -Iseconds)"
echo "Exit code: $EXIT_CODE"
echo "Log saved: $LOG_FILE"

exit $EXIT_CODE
