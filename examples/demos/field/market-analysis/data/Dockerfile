# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# demo-data: builds the selected data pack into the demo-data volume at /data (see data/README.md), and fetches
# external datasets into /sources.
# Build from the repository root, so the image carries the tool contracts that packs are checked against:
#   docker build -f data/Dockerfile .
FROM ghcr.io/astral-sh/uv:0.12.22@sha256:f513a91fc62fe7c17567eee97230dd198e43edb8a9fbecca843714a4358fe1bc AS uv

FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    DATA_DIR=/data \
    DATA_CONTRACTS_DIR=/app/contracts
WORKDIR /app/data

# uv and its download cache are mounted for the build only, so neither ends up in the image. The `remote` extra
# is what `demo-data fetch` needs for https, s3, gs and hf sources.
COPY data/pyproject.toml data/uv.lock ./
RUN --mount=from=uv,source=/uv,target=/bin/uv --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra remote --no-install-project
COPY data/ ./
RUN --mount=from=uv,source=/uv,target=/bin/uv --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra remote
COPY tools/*/contract/*.json /app/contracts/

# /data belongs to the runtime user, so a fresh named volume mounted there does too.
RUN useradd --uid 1000 --user-group --no-create-home demo && install -d -o demo -g demo /data
USER demo
ENTRYPOINT ["demo-data"]
CMD ["prepare"]
