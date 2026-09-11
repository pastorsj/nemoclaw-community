#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail
DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$DEPLOY_DIR/lib/common.sh"
load_deploy_env
HERMES_RUNTIME_DIR="$RUNTIME_DIR"

if command -v nemohermes >/dev/null 2>&1 && \
  nemohermes "$NEMOCLAW_SANDBOX_NAME" status --json >/dev/null 2>&1; then
  NEMOCLAW_SANDBOX_NAME="$NEMOCLAW_SANDBOX_NAME" \
    MCP_TRUSTED_PRIVATE_HOST="$QUERY_CLAW_PRIVATE_HOST" \
    QUERY_CLAW_PRIVATE_IP="$QUERY_CLAW_PRIVATE_IP" \
    QUERY_CLAW_MCP_URL="https://$QUERY_CLAW_PRIVATE_HOST:9443/mcp/" \
    QUERY_CLAW_RUNTIME_DIR="$HERMES_RUNTIME_DIR" \
    bash "$EXAMPLE_DIR/hermes/teardown.sh"
elif [[ -f "$HERMES_RUNTIME_DIR/query-claw-hermes-profile.json" || \
  -f "$HERMES_RUNTIME_DIR/query-claw-gsf-oauth-policy.applied.yaml" ]]; then
  die "nemohermes and sandbox $NEMOCLAW_SANDBOX_NAME are required to restore the Hermes profile and policy"
fi
compose down --remove-orphans
printf 'Query Claw services stopped; persistent databases and generated data were retained.\n'
