FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /workspace

# 依存関係のみ先にインストール（キャッシュ最適化）
# Workspace-aware build: 全 member の pyproject.toml を先にコピー。
# server は #74 で `pleno-ner-training` 依存を切ったため training package を
# install する必要は無いが、uv はワークスペース全体を resolve するので
# member の pyproject.toml は必要 (実体コードは sync 後にも不要)。
COPY pyproject.toml uv.lock ./
COPY packages/training/pyproject.toml packages/training/pyproject.toml
COPY packages/pii-scanner/pyproject.toml packages/pii-scanner/pyproject.toml
# pleno-recognizers は server の dependency なので source ごと copy が必要
# (uv は workspace member を wheel build するため pyproject だけでは足りない)。
COPY packages/recognizers/ packages/recognizers/
COPY server/pyproject.toml server/pyproject.toml
# server image は OSS baselines (ginza/ja-ginza/ja_core_news_trf) を含めない
# image size 膨張を構造的に抑制 (plan U1 Deployment image impact).
# `bench` は packages/training の `[project.optional-dependencies]` に定義しており、
# `--extra bench` を渡さない限り install されない (default exclude).
#
# `--package pleno-anonymize-server` で workspace の install を server に絞る。
# 以前は default の `uv sync` がすべての member を editable install しようと
# したため、pii-scanner の README/src が image に無いと
# `hatchling.build.build_editable` が `OSError: Readme file does not exist` で
# 落ちていた (deploy 失敗の原因)。`--package` で graph を server サブセットに
# 限定すれば pii-scanner / training は触られない。
RUN uv sync --frozen --no-dev --no-install-project --package pleno-anonymize-server

# アプリケーションコードをコピー（server のみ）。
# #74 で `recognizers_ja.py` を server/src 配下へ移動したため training の
# ソースは server image には不要。
COPY server/ server/
RUN uv sync --frozen --no-dev --package pleno-anonymize-server

# spaCy / NER モデル wheel install は最後の uv sync の **後ろ** に置く必要がある。
# 過去に sync の間に挟んでいた時期があり、`uv sync --frozen` が lockfile に存在しない
# 既インストール wheel を prune して production を壊した (build-time smoke で発覚)。
# 以降の RUN では sync をかけないこと。
RUN uv pip install https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl
RUN uv pip install \
    https://huggingface.co/0xhikae/ja-ner-ja/resolve/main/ja_ner_ja-0.2.0-py3-none-any.whl \
    https://huggingface.co/0xhikae/en-ner-en/resolve/main/en_ner_en-0.1.0.tar.gz

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
