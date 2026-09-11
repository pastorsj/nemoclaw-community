#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail
DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$DEPLOY_DIR/lib/common.sh"

stop_query_surface() {
  compose stop mcp-ingress gsf-mcp
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
  local compose_version component
  compose_version="$(docker compose version --short)"
  python3 - "$compose_version" <<'PY' || die "Docker Compose 2.24.4 or newer is required"
import re
import sys

parts = tuple(int(part) for part in re.findall(r"\d+", sys.argv[1])[:3])
raise SystemExit(0 if parts >= (2, 24, 4) else 1)
PY
  if [[ -n "$QUERY_CLAW_DATASET_REPOSITORY" ]]; then
    python3 "$EXAMPLE_DIR/scripts/activate_dataset.py" \
      --dataset "$QUERY_CLAW_DATASET_MANIFEST" \
      --repository-root "$QUERY_CLAW_DATASET_REPOSITORY" \
      --output "$QUERY_CLAW_DATA_DIR"
  else
    python3 "$EXAMPLE_DIR/scripts/generate_data.py" --validate
    python3 "$EXAMPLE_DIR/scripts/activate_dataset.py" \
      --generated "$RUNTIME_DIR/data" \
      --output "$QUERY_CLAW_DATA_DIR"
  fi
  # Refresh capabilities and connection values from the activation shared by
  # both source paths.
  export_runtime_env
  [[ "$QUERY_CLAW_HAS_PREDICTION" != 1 ]] || require_var KUMO_RFM_API_URL

  if [[ "$QUERY_CLAW_HAS_STRUCTURED" != 1 ]]; then
    compose stop gsf-mcp gsf-frontend ingestion-service gsf >/dev/null 2>&1 || true
  fi
  if [[ "$QUERY_CLAW_HAS_DOCUMENTS" != 1 ]]; then
    compose stop retriever >/dev/null 2>&1 || true
    rm -f "$RUNTIME_DIR/retriever-provenance.json"
  fi

  local -a components=()
  [[ "$QUERY_CLAW_HAS_STRUCTURED" != 1 ]] || components+=(gsf)
  [[ "$QUERY_CLAW_HAS_DOCUMENTS" != 1 ]] || components+=(retriever)
  components+=(ingress hermes)
  for component in "${components[@]}"; do
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
