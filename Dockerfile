FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /workspace

# Workspace-aware build: copy every member's pyproject so uv can resolve the
# graph, then sync only the server subset.
COPY pyproject.toml uv.lock ./
COPY packages/training/pyproject.toml packages/training/pyproject.toml
# pleno-anonymize is a server dependency (provides the recognizer registry),
# so its source must be copied — uv builds workspace members as wheels and
# pyproject alone is not enough.
COPY packages/sdk/ packages/sdk/
COPY server/pyproject.toml server/pyproject.toml
# Server image excludes OSS baselines (ginza / ja-ginza / ja_core_news_trf)
# to keep the image small. `bench` lives in packages/training's
# `[project.optional-dependencies]`, so without `--extra bench` it is skipped.
RUN uv sync --frozen --no-dev --no-install-project --extra image --extra appi --package pleno-anonymize-server

COPY server/ server/
RUN uv sync --frozen --no-dev --extra image --extra appi --package pleno-anonymize-server

# spaCy / NER model wheels install AFTER the last uv sync. A prior layout
# placed them between syncs, and `uv sync --frozen` pruned wheels not in the
# lockfile, breaking production (caught by build-time smoke).
RUN uv pip install https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl
# Both NER wheels are tok2vec-based (no torch / spacy-transformers) so the
# image stays well under fly's 8GB rootfs limit. v0.2.1 EN replaced the
# transformer build that pushed image size to 15.7GB (#177 rollback).
RUN uv pip install \
    https://huggingface.co/0xhikae/pleno_anonymize_ja/resolve/main/pleno_anonymize_ja-0.2.0-py3-none-any.whl \
    https://huggingface.co/0xhikae/pleno_anonymize_en/resolve/main/pleno_anonymize_en-0.2.1-py3-none-any.whl

# Pre-download APPI ONNX model files into HF cache so the first
# /api/analyze?engine=appi request doesn't block on a network fetch.
# Only download the quantized ONNX model and tokenizer files (~170MB total).
RUN uv run --no-sync python -c "\
from huggingface_hub import hf_hub_download; \
repo = '0xhikae/ja-ner-appi-v1-onnx'; \
[hf_hub_download(repo, f) for f in ['model_quantized.onnx', 'config.json', 'tokenizer.json', 'tokenizer_config.json', 'special_tokens_map.json', 'spm.model']]; \
print('APPI ONNX model cached')"

# Pre-download OpenAI Privacy Filter quantized ONNX (~1.6GB) for the
# /api/analyze?engine=openai-privacy-filter path. The .onnx file is a small
# header that references weights in the sibling .onnx_data blob, so both must
# land in the same cache directory; hf_hub_download handles that automatically.
# Skipping this would force the first request to download 1.6GB inline, well
# past any reasonable HTTP timeout.
RUN uv run --no-sync python -c "\
from huggingface_hub import hf_hub_download; \
repo = 'openai/privacy-filter'; \
[hf_hub_download(repo, f) for f in ['onnx/model_quantized.onnx', 'onnx/model_quantized.onnx_data', 'config.json', 'tokenizer.json', 'tokenizer_config.json']]; \
print('OpenAI Privacy Filter ONNX cached')"

# Build-time smoke test surfaces model-load failures at image build instead of
# runtime. `--no-sync` is required: `uv run` defaults to re-syncing the
# workspace, which would clobber the wheels we just installed.
RUN uv run --no-sync python -c "import spacy; spacy.load('pleno_anonymize_ja'); spacy.load('pleno_anonymize_en'); print('models loadable')"

FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Runtime system libraries for POST /api/redact's image OCR path. All are
# required even after dropping face redaction, because presidio-image-redactor
# imports cv2 at package-import time and depends on opencv-python (the
# non-headless build):
# - tesseract-ocr (+ jpn): presidio's OCR backend; without it the image route
#   returns HTTP 500 in prod.
# - libgl1 / libglib2.0-0: shared libs opencv-python links against
#   (libGL.so.1, libglib-2.0.so.0). Missing either makes `import
#   presidio_image_redactor` fail with ImportError, breaking the OCR path.
#   libgl1 is the Debian bookworm/trixie name; fall back to libgl1-mesa-glx on
#   bases that predate the rename so the build does not break.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libglib2.0-0 \
        tesseract-ocr \
        tesseract-ocr-jpn \
    && (apt-get install -y --no-install-recommends libgl1 \
        || apt-get install -y --no-install-recommends libgl1-mesa-glx) \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
COPY --from=builder /workspace /workspace
# HF Hub cache for the APPI ONNX model (~164MB quantized). Only the
# huggingface subdir is copied — the uv download cache is NOT needed
# (the venv is already installed) and would waste hundreds of MB.
COPY --from=builder /root/.cache/huggingface /root/.cache/huggingface

EXPOSE 8080
# `--no-sync` mirrors the build-time smoke: the runtime image already has all
# wheels installed in /workspace/.venv; auto-sync would re-resolve and prune.
CMD ["uv", "run", "--no-sync", "uvicorn", "server.src.app:app", "--host", "0.0.0.0", "--port", "8080"]
