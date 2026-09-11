#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail
DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$DEPLOY_DIR/lib/common.sh"

print_active_dataset() {
  local manifest="$1"
  python3 -c '
import json,sys
document=json.load(open(sys.argv[1], encoding="utf-8"))
print("  " + document["id"] + " (" + document["industry"]["title"] + ")")
' "$manifest"
}

main() {
  local dashboard_port api_port dashboard_host dashboard_code dashboard_state
  load_deploy_env
  dashboard_port="${NEMOCLAW_DASHBOARD_PORT:-18789}"
  api_port="${NEMOCLAW_HERMES_API_PORT:-8642}"

  compose ps
  if [[ "$QUERY_CLAW_HAS_PREDICTION" == 1 ]]; then
    printf '\nPredictive upstream:\n'
    printf '  GSF automatic Kumo routing configured\n'
  fi
  if [[ -f "$QUERY_CLAW_ACTIVE_MANIFEST" ]]; then
    printf '\nActive dataset:\n'
    print_active_dataset "$QUERY_CLAW_ACTIVE_MANIFEST"
  fi
  printf '\nLoopback user surfaces:\n'
  if [[ "$QUERY_CLAW_HAS_STRUCTURED" == 1 ]]; then
    printf '  NVIDIA GSF UI       http://127.0.0.1:3000\n'
    printf '  NVIDIA GSF API      http://127.0.0.1:3001\n'
  fi
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
  [[ "$QUERY_CLAW_HAS_DOCUMENTS" != 1 ]] || \
    printf '  NeMo Retriever  https://%s:9443/mcp\n' "$QUERY_CLAW_PRIVATE_HOST"
  [[ "$QUERY_CLAW_HAS_STRUCTURED" != 1 ]] || \
    printf '  Official GSF    https://%s:9444/mcp (OAuth)\n' "$QUERY_CLAW_PRIVATE_HOST"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
