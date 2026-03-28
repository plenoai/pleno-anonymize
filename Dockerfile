FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# 依存関係のみ先にインストール（キャッシュ最適化）
COPY app/pyproject.toml uv.lock ./
RUN uv sync --no-dev --frozen --no-install-project

# spaCyモデルを事前ダウンロード（uv pip で直接インストール）
RUN uv pip install https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl

COPY app/app.py app/config.cfg ./

FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app
COPY --from=builder /app /app
COPY --from=builder /root /root

EXPOSE 8080
CMD ["uv", "run", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
