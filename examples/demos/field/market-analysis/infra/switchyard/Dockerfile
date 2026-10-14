# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# Switchyard publishes no container image or release binary, so build the crates.io release
# with its own lockfile.
FROM rust:1.98.1-slim-trixie@sha256:4cd829461bd5c4d511c32e269da9cb8929223b666519d8004e35fc8d1d771ab7 AS build
ARG SWITCHYARD_VERSION=0.3.0
RUN cargo install --locked --root /out "switchyard-server@${SWITCHYARD_VERSION}"

FROM debian:13.7-slim@sha256:a99cfc517144bc59b1978475ec53b46ecabec7e43635402ee5b77cc54cd1b20a
# gettext-base provides envsubst for the entrypoint; curl serves the health check. Package
# versions float within the pinned Debian release so security updates apply.
# hadolint ignore=DL3008
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gettext-base \
 && rm -rf /var/lib/apt/lists/*
# The routing log lives here (the switchyard-data volume). The server exits at startup if it
# cannot write it, and a fresh named volume copies this ownership.
RUN install -d -o 1000 -g 1000 -m 0700 /var/lib/switchyard
COPY --from=build /out/bin/switchyard-server /usr/local/bin/
COPY entrypoint.sh judge-prompt.md /opt/switchyard/
COPY routes/ /opt/switchyard/routes/

ENV HOME=/tmp
USER 1000:1000
# Listens on :4000. No EXPOSE: a cached rebuild must give the same image ID.
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --start-interval=1s \
  CMD ["curl", "-fsS", "-o", "/dev/null", "http://127.0.0.1:4000/health"]
ENTRYPOINT ["/opt/switchyard/entrypoint.sh"]
