#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$DIR/_lib.sh"

host_mode=""
case "${1:-}" in
  "") ;;
  --stop-host-services) host_mode=stop ;;
  --purge-host-services) host_mode=purge ;;
  -h|--help)
    echo "Usage: $(basename "$0") [--stop-host-services|--purge-host-services]"
    exit 0
    ;;
  *) echo "Unknown argument: $1" >&2; exit 2 ;;
esac

if ! load_env; then
  echo "Continuing teardown with environment/default resource names." >&2
  export NEMOCLAW_SANDBOX_NAME="${NEMOCLAW_SANDBOX_NAME:-financial-assistant}"
  export OPENSHELL_GATEWAY="${OPENSHELL_GATEWAY:-openshell}"
  export OPENSHELL_GATEWAY_ENDPOINT="${OPENSHELL_GATEWAY_ENDPOINT:-https://127.0.0.1:17670}"
fi
if command -v openshell >/dev/null 2>&1; then
  stop_api_forward
  openshell sandbox delete "$NEMOCLAW_SANDBOX_NAME" >/dev/null 2>&1 || true
fi
rm -f "$EXAMPLE_DIR/.Dockerfile.staged"

case "$host_mode" in
  stop) bash "$DIR/00-host-services.sh" down ;;
  purge) bash "$DIR/00-host-services.sh" down --volumes ;;
esac

echo "Financial Assistant sandbox removed."
echo "The gateway-level financial-assistant-inference route and provider remain configured."
if [[ -f "$STATE_DIR/inference-route-before.txt" ]]; then
  echo "Prior gateway route snapshot: $STATE_DIR/inference-route-before.txt"
fi
[[ -z "$host_mode" ]] && echo "Phoenix remains running; pass --stop-host-services to stop it."
