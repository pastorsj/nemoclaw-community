#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail
DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$DEPLOY_DIR/lib/common.sh"
load_deploy_env
dashboard_port="${NEMOCLAW_DASHBOARD_PORT:-18789}"
api_port="${NEMOCLAW_HERMES_API_PORT:-8642}"

compose ps
qualification_marker="$RUNTIME_DIR/kumo-qualified.sha256"
kumo_state=unqualified
if command -v sha256sum >/dev/null 2>&1 && [[ -f "$qualification_marker" ]] && \
  [[ "$(<"$qualification_marker")" == "$(kumo_qualification_fingerprint)" ]]; then
  kumo_state=qualified
fi
printf '\nPredictive upstream:\n'
printf '  Kumo prediction API  %s\n' "$kumo_state"
printf '\nLoopback user surfaces:\n'
printf '  NVIDIA Ontology UI  http://127.0.0.1:3000\n'
printf '  NVIDIA Ontology API http://127.0.0.1:3001\n'
printf '  Hermes dashboard    %s\n' "$(nemohermes "$NEMOCLAW_SANDBOX_NAME" dashboard-url --quiet 2>/dev/null || printf 'not ready')"
printf '  Hermes API           http://127.0.0.1:%s/v1\n' "$api_port"
if [[ -n "${CHAT_UI_URL:-}" ]]; then
  dashboard_host="$(chat_ui_host "$CHAT_UI_URL")"
  dashboard_code="$(curl --silent --output /dev/null --write-out '%{http_code}' \
    --max-time 5 --header "Host: $dashboard_host" \
    "http://127.0.0.1:$dashboard_port/" 2>/dev/null || true)"
  if [[ "$dashboard_code" == 200 ]]; then
    dashboard_state=accepted
  else
    dashboard_state="rejected (HTTP ${dashboard_code:-none})"
  fi
  printf '  External dashboard  %s (%s)\n' "$CHAT_UI_URL" "$dashboard_state"
fi
printf '\nPrivate agent-only MCP routes:\n'
printf '  https://%s:9443/mcp/\n' "$QUERY_CLAW_PRIVATE_HOST"
