FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /workspace

# 依存関係のみ先にインストール（キャッシュ最適化）
COPY server/pyproject.toml uv.lock ./
RUN uv sync --no-dev --frozen --no-install-project

# spaCyモデル（英語）を事前ダウンロード
RUN uv pip install https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl

# 日本語NERモデルをコピー＆インストール
COPY packages/models/ja_ner_ja-0.1.0 packages/models/ja_ner_ja-0.1.0
RUN uv pip install packages/models/ja_ner_ja-0.1.0

# アプリケーションコードをコピー
COPY server/src/ src/

FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /workspace
COPY --from=builder /workspace /workspace
COPY --from=builder /root /root

EXPOSE 8080
CMD ["uv", "run", "uvicorn", "src.app:app", "--host", "0.0.0.0", "--port", "8080"]
