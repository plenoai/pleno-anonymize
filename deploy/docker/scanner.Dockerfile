# Multi-arch container image for `pleno-pii-scanner` CLI + every connector.
#
# Build:  docker buildx build -f deploy/docker/scanner.Dockerfile -t pleno/pii-scanner:dev --platform linux/amd64,linux/arm64 .
# Run:    docker run --rm -v $PWD:/scan pleno/pii-scanner:dev scan run dir --config-json '{"root":"/scan"}'
#
# This image is independent of the FastAPI server image (./Dockerfile).
# Server is for the cloud anonymization endpoint; this is for the
# enterprise scanner CLI that operators run inside their network or
# in a CronJob next to their data sources.

FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /workspace

# Workspace-aware build: copy every member's pyproject.toml so `uv
# sync` can resolve the cross-member graph, plus the pii-scanner
# source tree (CLI entry point) and the recognizers source (linked
# workspace dep).
COPY pyproject.toml uv.lock ./
COPY packages/recognizers/ packages/recognizers/
COPY packages/pii-scanner/ packages/pii-scanner/
# Connector wheels — every package under packages/pii-scanner-* gets
# bundled. Listed explicitly (not glob) so adding a new connector is a
# visible Dockerfile change reviewed in the same PR.
COPY packages/pii-scanner-aws/ packages/pii-scanner-aws/
COPY packages/pii-scanner-azure-devops/ packages/pii-scanner-azure-devops/
COPY packages/pii-scanner-bitbucket/ packages/pii-scanner-bitbucket/
COPY packages/pii-scanner-gcs/ packages/pii-scanner-gcs/
COPY packages/pii-scanner-github/ packages/pii-scanner-github/
COPY packages/pii-scanner-gitlab/ packages/pii-scanner-gitlab/
COPY packages/pii-scanner-notion/ packages/pii-scanner-notion/
COPY packages/pii-scanner-oci/ packages/pii-scanner-oci/
COPY packages/pii-scanner-postgres/ packages/pii-scanner-postgres/
COPY packages/pii-scanner-slack/ packages/pii-scanner-slack/

# Install the scanner core + every connector. `--package` constrains
# the workspace install to the scanner subgraph, but its dependency
# resolution pulls in every workspace member that another member
# depends on. Since connectors all depend on pleno-pii-scanner (not
# the other way around), we install each connector explicitly.
RUN uv sync --frozen --no-dev --no-install-project --package pleno-pii-scanner
RUN uv sync --frozen --no-dev --package pleno-pii-scanner

# Connectors register themselves via entry-points; install them after
# the project sync so their wheels are not pruned. Same prune semantics
# as the server image (see /docs/solutions/2026-05-03-uv-sync-wheel-install-ordering.md).
RUN uv pip install \
    ./packages/pii-scanner-aws \
    ./packages/pii-scanner-azure-devops \
    ./packages/pii-scanner-bitbucket \
    ./packages/pii-scanner-gcs \
    ./packages/pii-scanner-github \
    ./packages/pii-scanner-gitlab \
    ./packages/pii-scanner-notion \
    ./packages/pii-scanner-oci \
    ./packages/pii-scanner-postgres \
    ./packages/pii-scanner-slack

# spaCy model wheels last (must come after every uv sync — see solutions
# doc 2026-05-03-uv-sync-wheel-install-ordering.md).
RUN uv pip install \
    https://huggingface.co/0xhikae/ja-ner-ja/resolve/main/ja_ner_ja-0.2.0-py3-none-any.whl

# Build-time smoke: assert the CLI can enumerate every registered
# connector. This catches entry-point misregistrations at build time
# instead of at first scan.
RUN uv run --no-sync pleno-pii-scanner connectors list


FROM python:3.12-slim

# `git` is needed by the GitHub/GitLab/Bitbucket/Azure DevOps
# connectors which shell out to `git clone --depth=1`. Slim is fine —
# we only need the porcelain.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /workspace
COPY --from=builder /workspace /workspace
COPY --from=builder /root /root

# Non-root by default. Operators who need root for some bind-mount
# scenario can override with `--user 0`.
RUN useradd --create-home --uid 1000 scanner && \
    chown -R scanner:scanner /workspace
USER scanner

ENTRYPOINT ["uv", "run", "--no-sync", "pleno-pii-scanner"]
CMD ["--help"]
