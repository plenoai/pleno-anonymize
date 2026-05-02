FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /workspace

# 依存関係のみ先にインストール（キャッシュ最適化）
# Workspace-aware build: 全 member の pyproject.toml + lock を先にコピー
# `recognizers_ja.py` 物理移動 (U1) に伴い packages/training も image に必要
COPY pyproject.toml uv.lock ./
COPY packages/training/pyproject.toml packages/training/pyproject.toml
COPY server/pyproject.toml server/pyproject.toml
# server image は OSS baselines (ginza/ja-ginza/ja_core_news_trf) を含めない
# image size 膨張を構造的に抑制 (plan U1 Deployment image impact)。
# `bench` は packages/training の `[project.optional-dependencies]` に定義しており、
# `--extra bench` を渡さない限り install されない (default exclude)。
RUN uv sync --frozen --no-dev --no-install-project

# spaCyモデル（英語ベース）を事前ダウンロード
RUN uv pip install https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl

# NERモデルをHugging Faceからインストール
RUN uv pip install \
    https://huggingface.co/0xhikae/ja-ner-ja/resolve/main/ja_ner_ja-0.2.0-py3-none-any.whl \
    https://huggingface.co/0xhikae/en-ner-en/resolve/main/en_ner_en-0.1.0.tar.gz

# アプリケーションコードをコピー（workspace member 単位）
COPY packages/training/ packages/training/
COPY server/ server/
RUN uv sync --frozen --no-dev

# Build-time smoke test: catches model-load failures at image build (not runtime).
# Background: PR #40 fixed a 4-week-latent regression where the runtime warmup
# thread died silently because of a wrong spacy.load() argument, leaving the
# server up but unable to serve. This RUN line forces the failure mode visible
# at build time so a bad image is never pushed.
RUN uv run python -c "import spacy; spacy.load('ja_ner_ja'); spacy.load('en_ner_en'); print('models loadable')"

FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /workspace
COPY --from=builder /workspace /workspace
COPY --from=builder /root /root

EXPOSE 8080
CMD ["uv", "run", "uvicorn", "server.src.app:app", "--host", "0.0.0.0", "--port", "8080"]
