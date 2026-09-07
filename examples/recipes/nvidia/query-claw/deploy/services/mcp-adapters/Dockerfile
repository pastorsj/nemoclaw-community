# syntax=docker/dockerfile:1.7
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

ARG GSF_IMAGE=gsf:latest
FROM ${GSF_IMAGE}

USER root
COPY --from=ghcr.io/astral-sh/uv@sha256:eb2843a1e56fd9e30c7276ce1a52cba86e64c7b385f5e3279a0e08e02dd058fc /uv /usr/local/bin/uv
RUN uv pip install --python /opt/venv/bin/python --no-cache 'fastmcp==3.2.4'
RUN install -d -m 0555 /opt/query-claw
COPY --chmod=0444 app.py /opt/query-claw/app.py
USER gsf
