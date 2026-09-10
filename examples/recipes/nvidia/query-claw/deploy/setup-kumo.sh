#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail
DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$DEPLOY_DIR/lib/common.sh"
initialize_deploy_env
require_var KUMO_RFM_API_URL
require_command sha256sum
qualification_marker="$RUNTIME_DIR/kumo-qualified.sha256"
prediction_receipt="$QUERY_CLAW_DATA_DIR/prediction-qualified.json"
rm -f "$qualification_marker"
rm -f "$prediction_receipt"

python3 "$DEPLOY_DIR/qualify_prediction.py" \
  --manifest "$QUERY_CLAW_ACTIVE_MANIFEST" \
  --receipt "$prediction_receipt"

compose build query-claw-mcp
compose up -d --no-deps --force-recreate --remove-orphans query-claw-mcp

container_id="$(compose ps -q query-claw-mcp)"
for _ in {1..60}; do
  [[ "$(docker inspect -f '{{.State.Health.Status}}' "$container_id" 2>/dev/null)" == healthy ]] && break
  sleep 2
done
[[ "$(docker inspect -f '{{.State.Health.Status}}' "$container_id" 2>/dev/null)" == healthy ]] || \
  die "query-claw-mcp did not become healthy"

(umask 077 && kumo_qualification_fingerprint >"$qualification_marker")

printf 'ready: bounded Query Claw MCP facade and Ontology-routed Kumo prediction\n'
