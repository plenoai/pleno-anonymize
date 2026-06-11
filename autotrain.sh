#!/usr/bin/env bash
# autotrain.sh - NER自律改善ループをClaude Codeで実行する
# プロジェクトルートに配置し、ルートから実行すること
#
# Usage:
#   ./autotrain.sh                    # モデル改善: ja, 3イテレーション
#   ./autotrain.sh ja 5               # モデル改善: 日本語, 5イテレーション
#   ./autotrain.sh en 3               # モデル改善: 英語, 3イテレーション
#   ./autotrain.sh ja 5 --resume      # 前回の続きから再開
#   ./autotrain.sh ja --evolve        # ベンチマーク進化
#   ./autotrain.sh ja 5 --full        # モデル改善 → ベンチマーク進化 → モデル改善
#
# 前提:
#   - claude CLI がインストール済み
#   - .env に OPENAI_API_KEY が設定済み

set -euo pipefail

TARGET_LANG="${1:-ja}"
MODE="improve"
MAX_ITER="3"
EXIT_CODE=0

# 引数パース
shift || true
while [[ $# -gt 0 ]]; do
  case "$1" in
    --evolve)  MODE="evolve"; shift ;;
    --full)    MODE="full"; shift ;;
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
  local latest_scores
  latest_scores=$(find "$TRAINING_DIR/data/benchmark" -name "scores.json" -path "*/$TARGET_LANG/*" 2>/dev/null \
    | sort -V | tail -1) || true
  if [ -n "$latest_scores" ] && [ -f "$latest_scores" ]; then
    local version
    version=$(echo "$latest_scores" | grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+') || true
    echo "$label (benchmark $version):"
    python3 -c "
import json
with open('$latest_scores') as f:
    scores = json.load(f)
for model, s in scores.items():
    if model.startswith('_'): continue
    print(f'  {model}: F1={s[\"ents_f\"]*100:.1f}%  P={s[\"ents_p\"]*100:.1f}%  R={s[\"ents_r\"]*100:.1f}%')
    for label, v in s.get('ents_per_type', {}).items():
        print(f'    {label}: F1={v[\"f\"]*100:.1f}%  P={v[\"p\"]*100:.1f}%  R={v[\"r\"]*100:.1f}%')
" 2>/dev/null || echo "  (scores not parseable)"
  fi
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

  evolve)
    run_claude "/benchmark-evolve $TARGET_LANG" "Benchmark Evolution" || EXIT_CODE=$?
    ;;

  full)
    echo "=== Phase 1/3: Model Improvement ==="
    run_claude "/ner-improve $TARGET_LANG $MAX_ITER" "Model Improvement" || true
    echo ""
    print_scores "After improvement"
    echo ""

    echo "=== Phase 2/3: Benchmark Evolution ==="
    run_claude "/benchmark-evolve $TARGET_LANG" "Benchmark Evolution" || true
    echo ""

    echo "=== Phase 3/3: Model Improvement (vs new benchmark) ==="
    PREV_COUNT=$(wc -l < "$LOG_DIR/log.jsonl" | tr -d ' ')
    run_claude "/ner-improve $TARGET_LANG $MAX_ITER

ベンチマークが進化しました。新しいベンチマークに対して改善を続けてください。
実験ログ ($PREV_COUNT 件) が packages/training/experiments/log.jsonl にあります。" \
      "Model Improvement (vs evolved benchmark)" || true
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
