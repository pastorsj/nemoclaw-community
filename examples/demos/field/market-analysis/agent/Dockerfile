# syntax=docker/dockerfile:1
# check=skip=SecretsUsedInArgOrEnv
# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# The Hermes agent as an OpenShell sandbox image. OpenShell ignores ENTRYPOINT
# and CMD, so the image's s6 init never runs: everything it would seed at boot
# is done here at build time. The main process is passed at sandbox creation:
#   openshell sandbox create --from <this image> ... -- /opt/hermes/.venv/bin/hermes gateway run
#
# Build context: agent/. Named context `contracts`: the repo's contracts/ dir.
FROM nousresearch/hermes-agent:v2026.9.24@sha256:fca358f12efd65bfaaca05884166f15c0e2788375ca30d77061ac1ebc96452b7

# Comma-separated optional features: retrieval, analytics, kumo, ontology.
ARG AGENT_FEATURES=retrieval,analytics

USER root
RUN --mount=type=bind,source=patches,target=/tmp/patches \
    cd /opt/hermes && git apply --exclude='tests/*' /tmp/patches/*.patch

COPY sandbox-policy.yaml /etc/openshell/policy.yaml
COPY profile/skills/ /opt/agent/skills/
COPY --from=contracts tool-registry.json /opt/agent/contracts/tool-registry.json

# Profile files are root-owned and read-only to the hermes user.
COPY profile/SOUL.md profile/relay-plugins.toml profile/distribution.yaml /opt/data/
COPY profile/plugins/ /opt/data/plugins/
RUN --mount=type=bind,source=render_config.py,target=/tmp/render_config.py \
    --mount=type=bind,source=profile/config.yaml,target=/tmp/config.yaml \
    /opt/hermes/.venv/bin/python /tmp/render_config.py --features "$AGENT_FEATURES" /tmp/config.yaml \
      > /opt/data/config.yaml

# Fails the build after a Hermes bump that breaks either assumption:
# - network policy matches binaries by real path, so every rule must name the
#   interpreter Hermes actually runs;
# - NeMo Relay below 0.9 exports plain HTTP to Phoenix on host.openshell.internal
#   (0.9 needs HTTPS or loopback, so an in-sandbox collector).
RUN <<'EOF' /opt/hermes/.venv/bin/python
import os, sys, yaml
from importlib.metadata import version
python = os.path.realpath(sys.executable)
policy = yaml.safe_load(open("/etc/openshell/policy.yaml"))
paths = {b["path"] for rule in policy["network_policies"].values() for b in rule["binaries"]}
assert paths == {python}, f"policy binaries {paths} != Hermes interpreter {python}"
relay = version("nemo-relay")
assert tuple(map(int, relay.split(".")[:2])) < (0, 9), f"nemo-relay {relay} needs a collector for Phoenix"
EOF

# The base image declares VOLUME /opt/data, so the workspace lives elsewhere.
RUN install -d -o 10000 -g 10000 /sandbox

# What the image's s6 stage2 hook does at boot, as the hermes user (10000):
# HERMES_HOME dirs, only the essential bundled skill, and config migration
# (a no-op while config.yaml is at the image's latest version, 46).
USER 10000:10000
RUN cd /opt/data \
 && mkdir -p backups cron sessions logs/gateways hooks memories skills skins plans workspace home \
      pairing platforms/pairing lazy-packages \
 && touch .no-bundled-skills \
 && /opt/hermes/.venv/bin/python /opt/hermes/tools/skills_sync.py \
 && /opt/hermes/.venv/bin/python /opt/hermes/scripts/docker_config_migrate.py

# Hermes must load SOUL.md rather than block it (see profile/distribution.yaml)
# and stage skill_manage writes (skills.write_approval in profile/config.yaml).
RUN <<'EOF' /opt/hermes/.venv/bin/python
from agent.prompt_builder import load_soul_md
from tools.write_approval import write_approval_enabled
soul = load_soul_md()
assert soul and not soul.startswith("[BLOCKED"), soul
assert write_approval_enabled("skills"), "skills.write_approval is off"
EOF

# No secrets here (hence the check=skip above): Switchyard ignores the client
# key, and the real receipt key arrives as an OpenShell placeholder.
ENV SWITCHYARD_CLIENT_API_KEY=not-a-secret \
    HERMES_STREAM_READ_TIMEOUT=900 \
    HERMES_STREAM_STALE_TIMEOUT=600 \
    HERMES_RECEIPT_API_URL=http://host.openshell.internal:8000 \
    HERMES_NEMO_RELAY_PLUGINS_TOML=/opt/data/relay-plugins.toml
WORKDIR /sandbox
