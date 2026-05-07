FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /workspace

# Workspace-aware build: copy every member's pyproject so uv can resolve the
# graph, then sync only the server subset.
COPY pyproject.toml uv.lock ./
COPY packages/training/pyproject.toml packages/training/pyproject.toml
# pleno-recognizers is a server dependency, so its source must be copied
# (uv builds workspace members as wheels — pyproject alone is not enough).
COPY packages/recognizers/ packages/recognizers/
COPY server/pyproject.toml server/pyproject.toml
# Server image excludes OSS baselines (ginza / ja-ginza / ja_core_news_trf)
# to keep the image small. `bench` lives in packages/training's
# `[project.optional-dependencies]`, so without `--extra bench` it is skipped.
RUN uv sync --frozen --no-dev --no-install-project --package pleno-anonymize-server

COPY server/ server/
RUN uv sync --frozen --no-dev --package pleno-anonymize-server

# spaCy / NER model wheels install AFTER the last uv sync. A prior layout
# placed them between syncs, and `uv sync --frozen` pruned wheels not in the
# lockfile, breaking production (caught by build-time smoke).
RUN uv pip install https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl
RUN uv pip install \
    https://huggingface.co/0xhikae/ja-ner-ja/resolve/main/ja_ner_ja-0.2.0-py3-none-any.whl \
    https://huggingface.co/0xhikae/en-ner-en/resolve/main/en_ner_en-0.1.0.tar.gz

# Build-time smoke test surfaces model-load failures at image build instead of
# runtime. `--no-sync` is required: `uv run` defaults to re-syncing the
# workspace, which would clobber the wheels we just installed.
RUN uv run --no-sync python -c "import spacy; spacy.load('ja_ner_ja'); spacy.load('en_ner_en'); print('models loadable')"

FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /workspace
COPY --from=builder /workspace /workspace
COPY --from=builder /root /root

EXPOSE 8080
# `--no-sync` mirrors the build-time smoke: the runtime image already has all
# wheels installed in /workspace/.venv; auto-sync would re-resolve and prune.
CMD ["uv", "run", "--no-sync", "uvicorn", "server.src.app:app", "--host", "0.0.0.0", "--port", "8080"]
