#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail
DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$DEPLOY_DIR/lib/common.sh"
initialize_deploy_env

# Recreate so an idempotent rerun also applies Caddyfile changes.
compose up -d --no-deps --force-recreate mcp-ingress
CA_FILE="$RUNTIME_DIR/query-claw-mcp-root.crt"
for _ in {1..30}; do
  if compose cp mcp-ingress:/data/caddy/pki/authorities/local/root.crt "$CA_FILE" \
    >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
[[ -s "$CA_FILE" ]] || die "Caddy root certificate was not created"
chmod 600 "$CA_FILE"

for _ in {1..60}; do
  if curl --fail --silent --cacert "$CA_FILE" \
    "https://$QUERY_CLAW_PRIVATE_HOST:9443/query-claw/health" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
curl --fail --silent --show-error --cacert "$CA_FILE" \
  "https://$QUERY_CLAW_PRIVATE_HOST:9443/query-claw/health" >/dev/null || \
  die "private MCP TLS ingress did not become ready"

printf 'ready: private MCP TLS ingress at https://%s:9443\n' "$QUERY_CLAW_PRIVATE_HOST"
