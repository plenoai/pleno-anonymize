#!/bin/bash
# Setup Japanese NER model for pleno-anonymize

set -e

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MODEL_PATH="$PROJECT_ROOT/packages/models/pleno_anonymize_ja-0.2.0"
VENV_PATH="$PROJECT_ROOT/.venv"

# Activate venv if it exists
if [ -f "$VENV_PATH/bin/activate" ]; then
    source "$VENV_PATH/bin/activate"
fi

echo "Installing pleno_anonymize_ja model..."
pip install -e "$MODEL_PATH"

echo "Model installation complete!"
