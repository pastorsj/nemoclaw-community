# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# The tool registry comes from the named build context `contracts`:
#   docker build --build-context contracts=contracts -t market-demo/api:local api
ARG PYTHON_IMAGE=python:3.12.14-slim-trixie@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f

FROM ghcr.io/astral-sh/uv:0.12.22@sha256:f513a91fc62fe7c17567eee97230dd198e43edb8a9fbecca843714a4358fe1bc AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /opt/demo-api
# Dependencies first, so source and README edits reuse the cached layer.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-install-project
COPY README.md ./
COPY src/ src/
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-editable

FROM ${PYTHON_IMAGE}
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --home-dir /nonexistent app \
    && install -d -o app -g app /var/lib/demo-api
COPY --from=build /opt/demo-api/.venv /opt/demo-api/.venv
COPY --from=contracts tool-registry.json /opt/demo-api/contracts/tool-registry.json
ENV PATH=/opt/demo-api/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp \
    TOOL_REGISTRY_FILE=/opt/demo-api/contracts/tool-registry.json
USER 10001:10001
# The job store (SQLite); mount the api-data volume here. A new volume inherits the app user as owner.
VOLUME /var/lib/demo-api
# Listens on :8000. No EXPOSE: a cached rebuild must give the same image ID.
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"]
# One worker: the job queue and the cancel signals live in the process. Open SSE streams hold
# uvicorn's graceful shutdown until this timeout, and only then does the runner fail live jobs
# and stop their Hermes runs; browsers resume from their cursor, so cut streams early.
CMD ["uvicorn", "demo_api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--timeout-graceful-shutdown", "3"]
