#!/bin/bash
# Setup Japanese NER model for pleno-anonymize

set -e

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MODEL_PATH="$PROJECT_ROOT/packages/models/ja_ner_ja-0.1.0"
VENV_PATH="$PROJECT_ROOT/.venv"

# Activate venv if it exists
if [ -f "$VENV_PATH/bin/activate" ]; then
    source "$VENV_PATH/bin/activate"
fi

echo "Installing ja_ner_ja model..."
pip install -e "$MODEL_PATH"

echo "Model installation complete!"
