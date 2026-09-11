# syntax=docker/dockerfile:1.7
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv@sha256:eb2843a1e56fd9e30c7276ce1a52cba86e64c7b385f5e3279a0e08e02dd058fc /uv /usr/local/bin/uv

WORKDIR /app
COPY mcp/pyproject.toml mcp/uv.lock mcp/README.md ./
COPY mcp/gsf_mcp ./gsf_mcp
RUN uv sync --frozen --no-dev --no-editable \
 && useradd --system --uid 10001 --home-dir /app gsf-mcp \
 && chown -R gsf-mcp /app

USER gsf-mcp
EXPOSE 3003
ENTRYPOINT ["/app/.venv/bin/gsf-mcp"]
