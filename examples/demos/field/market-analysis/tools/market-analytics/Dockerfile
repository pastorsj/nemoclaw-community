# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# CPU image by default. The GPU image adds the RAPIDS accelerators and needs an NVIDIA GPU (gpus: all) to run:
#   docker build --build-arg ANALYTICS_EXTRAS=gpu-cu12,kumo tools/market-analytics
ARG PYTHON_IMAGE=python:3.12.14-slim-trixie@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f

FROM ghcr.io/astral-sh/uv:0.12.22@sha256:f513a91fc62fe7c17567eee97230dd198e43edb8a9fbecca843714a4358fe1bc AS uv

FROM ${PYTHON_IMAGE} AS build
# Comma-separated extras from pyproject.toml: "kumo" (CPU) or "gpu-cu12,kumo" (GPU).
ARG ANALYTICS_EXTRAS=kumo
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /opt/market-analytics
# Dependencies first, so source and README edits reuse the cached layer (on the GPU image, a 15 GB install).
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project $(echo "$ANALYTICS_EXTRAS" | tr ',' '\n' | sed '/^$/d; s/^/--extra /')
# The service reads contract/ next to src/ (see data.py), so the image keeps the project layout.
COPY README.md ./
COPY contract/ contract/
COPY src/ src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev $(echo "$ANALYTICS_EXTRAS" | tr ',' '\n' | sed '/^$/d; s/^/--extra /')

FROM ${PYTHON_IMAGE}
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --home-dir /nonexistent app
COPY --from=build /opt/market-analytics /opt/market-analytics
ENV PATH=/opt/market-analytics/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp \
    DATA_ACTIVE_DIR=/data/active \
    # Only used when a GPU is attached: lets the NVIDIA runtime expose the CUDA driver.
    NVIDIA_DRIVER_CAPABILITIES=compute,utility
USER 10001:10001
# Listens on :3010. No EXPOSE: a cached rebuild must give the same image ID.
# /health is 200 only while the analytics worker process is up.
HEALTHCHECK --interval=10s --timeout=5s --start-period=60s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3010/health', timeout=4)"]
CMD ["market-analytics-server"]
