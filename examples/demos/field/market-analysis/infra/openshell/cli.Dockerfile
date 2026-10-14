# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# OpenShell CLI from the sha256-pinned release tarball (OpenShell publishes no
# CLI image). It runs one-off `openshell` commands for scripts/demo.sh and the
# long-running Hermes forwarder, so the host needs only Docker.
FROM alpine:3.24.2@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6

ARG TARGETARCH
RUN apk add --no-cache curl jq
RUN --mount=type=bind,source=versions.env,target=/tmp/versions.env <<'EOF'
set -eu
. /tmp/versions.env
case "$TARGETARCH" in
  amd64) arch=x86_64;  sum=$OPENSHELL_CLI_SHA256_X86_64 ;;
  arm64) arch=aarch64; sum=$OPENSHELL_CLI_SHA256_AARCH64 ;;
  *) echo "unsupported architecture: $TARGETARCH" >&2; exit 1 ;;
esac
curl -fsSL --retry 3 -o /tmp/openshell.tgz \
  "https://github.com/NVIDIA/OpenShell/releases/download/v${OPENSHELL_VERSION}/openshell-${arch}-unknown-linux-musl.tar.gz"
echo "${sum}  /tmp/openshell.tgz" | sha256sum -c -
tar -xzf /tmp/openshell.tgz -C /usr/local/bin openshell
rm /tmp/openshell.tgz
test "$(openshell --version)" = "openshell ${OPENSHELL_VERSION}"
EOF

ENTRYPOINT ["openshell"]
