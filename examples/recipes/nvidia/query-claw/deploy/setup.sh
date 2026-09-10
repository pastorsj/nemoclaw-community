#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail
DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$DEPLOY_DIR/lib/common.sh"

stop_query_surface() {
  compose stop mcp-ingress query-claw-mcp
}

keep_query_surface_down_on_failure() {
  local status="$1"
  if ((status != 0)); then
    stop_query_surface >/dev/null 2>&1 || true
  fi
  return "$status"
}

main() {
  require_command curl
  require_command docker
  require_command python3
  initialize_deploy_env
  trap 'keep_query_surface_down_on_failure $?' EXIT
  stop_query_surface
  require_var KUMO_RFM_API_URL
  local compose_version component
  compose_version="$(docker compose version --short)"
  python3 - "$compose_version" <<'PY' || die "Docker Compose 2.24.4 or newer is required"
import re
import sys

parts = tuple(int(part) for part in re.findall(r"\d+", sys.argv[1])[:3])
raise SystemExit(0 if parts >= (2, 24, 4) else 1)
PY
  python3 "$EXAMPLE_DIR/scripts/generate_data.py" --validate
  python3 "$EXAMPLE_DIR/scripts/prepare_data_packs.py" \
    --datasets "$QUERY_CLAW_DATASETS" \
    --packs-root "$QUERY_CLAW_PACKS_ROOT"

  for component in gsf retriever kumo ingress hermes; do
    printf '\n==> Setting up %s\n' "$component"
    "$DEPLOY_DIR/setup-$component.sh"
  done

  printf '\nQuery Claw deployment completed.\n'
  "$DEPLOY_DIR/status.sh"
  trap - EXIT
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
