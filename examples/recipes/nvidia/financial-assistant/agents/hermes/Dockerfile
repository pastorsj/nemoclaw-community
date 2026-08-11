# syntax=docker/dockerfile:1.7
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# The NemoClaw v0.0.105 base carries the qualified OpenShell runtime and
# sandbox hardening. Its Hermes 0.19.0 payload is replaced by a checksum-pinned
# snapshot of the latest reviewed upstream Hermes main branch.
ARG BASE_IMAGE=ghcr.io/nvidia/nemoclaw/hermes-sandbox-base@sha256:57c091ab9b31c924eac0050e66c834c37df875154a254964302a31b119b50b96
ARG HERMES_COMMIT=a1bfbccc02d5bfdaef1568facfca2cc1456c59f0
ARG HERMES_TARBALL_SHA256=7b7c3a22d723fbf989cbe1629906dc4333997b2f91a3fe7f6ae65dc92e1d561b
ARG HERMES_SEMVER=0.20.0
# messaging supplies aiohttp for Hermes's API adapter; web retains its
# reviewed server pins. Unused provider, MCP, and bridge extras stay absent.
ARG HERMES_UV_EXTRAS="messaging web"

FROM ${BASE_IMAGE} AS hermes-builder
ARG HERMES_COMMIT
ARG HERMES_TARBALL_SHA256
ARG HERMES_SEMVER
ARG HERMES_UV_EXTRAS

# Hermes owns NeMo Relay as a normal ranged dependency and already carries the
# cryptography 50 security floor. The narrow lock patch moves only Relay from
# 0.7.1 to 0.7.2; there is no recipe-specific Relay extra, plugin, daemon, or
# finalization hook.
COPY certs/ /usr/local/share/ca-certificates/
COPY agents/hermes/nemo-relay/hermes-relay-lock.patch /tmp/hermes-relay-lock.patch
RUN --mount=type=secret,id=github_token,required=false \
    set -eu \
    && update-ca-certificates \
    && HERMES_URL="https://github.com/NousResearch/hermes-agent/archive/${HERMES_COMMIT}.tar.gz" \
    && if [ -s /run/secrets/github_token ]; then \
         curl -L --proto '=https' --tlsv1.2 -sSf \
           -H "Authorization: Bearer $(cat /run/secrets/github_token)" \
           "$HERMES_URL" -o /tmp/hermes.tar.gz; \
       else \
         curl -L --proto '=https' --tlsv1.2 -sSf \
           "$HERMES_URL" -o /tmp/hermes.tar.gz; \
       fi \
    && printf '%s  %s\n' "$HERMES_TARBALL_SHA256" /tmp/hermes.tar.gz \
       > /tmp/hermes.tar.gz.sha256 \
    && sha256sum -c /tmp/hermes.tar.gz.sha256 \
    && rm -rf /opt/hermes \
    && mkdir -p /opt/hermes \
    && tar -xzf /tmp/hermes.tar.gz -C /opt/hermes --strip-components=1 \
    && rm -rf /opt/hermes/tests \
    && git -C /opt/hermes apply --check --unidiff-zero /tmp/hermes-relay-lock.patch \
    && git -C /opt/hermes apply --unidiff-zero /tmp/hermes-relay-lock.patch \
    && test "$(sed -n 's/^version = "\(.*\)"/\1/p' /opt/hermes/pyproject.toml | head -1)" = "$HERMES_SEMVER" \
    && python3 -c 'import pathlib,tomllib; lock=tomllib.loads(pathlib.Path("/opt/hermes/uv.lock").read_text()); relay=next(p for p in lock["package"] if p["name"]=="nemo-relay"); assert relay["version"]=="0.7.2",relay' \
    && rm /tmp/hermes.tar.gz /tmp/hermes.tar.gz.sha256 \
       /tmp/hermes-relay-lock.patch

WORKDIR /opt/hermes
# hadolint ignore=SC2086
RUN set -eu \
    && set -- \
    && for extra in ${HERMES_UV_EXTRAS}; do set -- "$@" --extra "$extra"; done \
    && uv lock --check \
    && uv sync --python /usr/bin/python3.13 --frozen --no-dev "$@" --no-cache \
    && uv pip check --python /opt/hermes/.venv/bin/python \
    && /opt/hermes/.venv/bin/python -c \
       'from importlib.metadata import version; expected={"aiohttp":"3.14.3","cryptography":"50.0.0","hermes-agent":"0.20.0","nemo-relay":"0.7.2","pillow":"12.3.0","starlette":"1.3.1","tornado":"6.5.7"}; actual={name:version(name) for name in expected}; assert actual==expected,actual' \
    && npm ci --prefer-offline --no-audit --no-fund \
    && npm run build --workspace ui-tui \
    && npm run build --workspace web \
    && rm -rf node_modules ui-tui/node_modules web/node_modules \
    && npm ci --omit=dev --workspaces=false --prefer-offline --no-audit --no-fund \
    && rm -rf /root/.npm /root/.cache/electron /root/.cache/node-gyp \
    && test -s /opt/hermes/ui-tui/dist/entry.js \
    && test -s /opt/hermes/hermes_cli/web_dist/index.html \
    && test "$(readlink -f /opt/hermes/.venv/bin/python)" = /usr/bin/python3.13 \
    && chmod -R a+rX /opt/hermes

FROM ${BASE_IMAGE}

USER root
RUN rm -rf /opt/hermes
COPY --from=hermes-builder /opt/hermes /opt/hermes
RUN ln -sfn /opt/hermes/.venv/bin/hermes /usr/local/bin/hermes \
    && ln -sfn /opt/hermes/.venv/bin/hermes-agent /usr/local/bin/hermes-agent \
    && ln -sfn /opt/hermes/.venv/bin/hermes-acp /usr/local/bin/hermes-acp \
    && /usr/local/bin/hermes --version \
    && /opt/hermes/.venv/bin/python -c \
       'from importlib.metadata import version; expected={"cryptography":"50.0.0","hermes-agent":"0.20.0","nemo-relay":"0.7.2"}; actual={name:version(name) for name in expected}; assert actual==expected,actual'

COPY certs/ /usr/local/share/ca-certificates/
RUN update-ca-certificates \
    && mkdir -p /usr/local/lib/nemoclaw/bin

COPY agents/hermes/nemo-relay/hermes-cli-shim /usr/local/lib/nemoclaw/bin/hermes
RUN chmod 755 /usr/local/lib/nemoclaw/bin/hermes

# Render an immutable native Relay component configuration. Hermes configures
# its per-session ATIF subscriber at runtime; Phoenix carries no credential.
ARG PHOENIX_COLLECTOR_ENDPOINT=http://host.openshell.internal:6006/v1/traces
ARG PHOENIX_PROJECT_NAME=financial-assistant
COPY agents/hermes/nemo-relay/plugins.toml.in /tmp/nemo-relay-plugins.toml.in
RUN set -eu \
    && mkdir -p /etc/nemo-relay/config /etc/nemo-relay/xdg /etc/nemo-relay/runtime \
    && if [ -n "${PHOENIX_COLLECTOR_ENDPOINT}" ]; then \
         PHOENIX_ENABLED=true; PHOENIX_ENDPOINT="${PHOENIX_COLLECTOR_ENDPOINT%/}"; \
       else \
         PHOENIX_ENABLED=false; PHOENIX_ENDPOINT="http://127.0.0.1:4318/v1/traces"; \
       fi \
    && awk \
         -v phoenix_enabled="$PHOENIX_ENABLED" \
         -v phoenix_endpoint="$PHOENIX_ENDPOINT" \
         -v phoenix_project="$PHOENIX_PROJECT_NAME" \
         '{ gsub(/@@PHOENIX_ENABLED@@/, phoenix_enabled); gsub(/@@PHOENIX_ENDPOINT@@/, phoenix_endpoint); gsub(/@@PHOENIX_PROJECT_NAME@@/, phoenix_project); print }' \
         /tmp/nemo-relay-plugins.toml.in > /etc/nemo-relay/config/plugins.toml \
    && rm /tmp/nemo-relay-plugins.toml.in \
    && /opt/hermes/.venv/bin/python -c \
       'import tomllib; from nemo_relay import plugin; cfg=tomllib.load(open("/etc/nemo-relay/config/plugins.toml","rb")); report=plugin.validate(cfg); assert not report["diagnostics"],report' \
    && chown -R root:root /etc/nemo-relay \
    && chmod 555 /etc/nemo-relay /etc/nemo-relay/config \
       /etc/nemo-relay/xdg /etc/nemo-relay/runtime \
    && chmod 444 /etc/nemo-relay/config/plugins.toml

ENV HERMES_DISABLE_LAZY_INSTALLS=1 \
    HERMES_NEMO_RELAY_PLUGINS_TOML=/etc/nemo-relay/config/plugins.toml \
    HERMES_TUI_DIR=/opt/hermes/ui-tui \
    HERMES_HOME=/sandbox/.hermes-data \
    XDG_CONFIG_HOME=/etc/nemo-relay/xdg

COPY agents/hermes/generate-config.ts /opt/nemoclaw-generate-config.ts
COPY scripts/lib/sandbox-init.sh /usr/local/lib/nemoclaw/sandbox-init.sh
COPY agents/hermes/start.sh /usr/local/bin/nemoclaw-start
RUN chmod 644 /usr/local/lib/nemoclaw/sandbox-init.sh \
    && chmod 755 /usr/local/bin/nemoclaw-start \
    && chmod 444 /opt/nemoclaw-generate-config.ts

COPY --chown=sandbox:sandbox agents/hermes/SOUL.md /sandbox/.hermes-data/SOUL.md
COPY --chown=sandbox:sandbox skills/ /sandbox/.hermes-data/skills/

ARG NEMOCLAW_MODEL=nvidia/nemotron-3-ultra-550b-a55b
ARG NEMOCLAW_INFERENCE_BASE_URL=https://inference.local/v1
ARG NEMOCLAW_BUILD_ID=development
ENV NEMOCLAW_MODEL=${NEMOCLAW_MODEL} \
    NEMOCLAW_INFERENCE_BASE_URL=${NEMOCLAW_INFERENCE_BASE_URL} \
    NEMOCLAW_BUILD_ID=${NEMOCLAW_BUILD_ID}

WORKDIR /sandbox
USER sandbox
RUN node --experimental-strip-types /opt/nemoclaw-generate-config.ts

USER root
RUN chown root:root /sandbox/.hermes \
    && find /sandbox/.hermes -mindepth 1 -exec chown -h root:root {} + \
    && chmod 755 /sandbox/.hermes \
    && chmod 444 /sandbox/.hermes/config.yaml /sandbox/.hermes/.env \
    && sha256sum /sandbox/.hermes/config.yaml /sandbox/.hermes/.env \
       > /sandbox/.hermes/.config-hash \
    && printf '%s\n' "$NEMOCLAW_BUILD_ID" > /etc/nemoclaw-financial-assistant-build-id \
    && chmod 444 /sandbox/.hermes/.config-hash \
       /etc/nemoclaw-financial-assistant-build-id \
    && chown root:root /sandbox/.hermes/.config-hash \
       /etc/nemoclaw-financial-assistant-build-id

ENTRYPOINT ["/usr/local/bin/nemoclaw-start"]
CMD ["/bin/bash"]
