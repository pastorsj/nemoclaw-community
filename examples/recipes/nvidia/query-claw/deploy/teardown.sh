#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail
DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$DEPLOY_DIR/lib/common.sh"
load_deploy_env
HERMES_RUNTIME_DIR="${QUERY_CLAW_RUNTIME_DIR:-$RUNTIME_DIR}"

if command -v nemohermes >/dev/null 2>&1 && \
  nemohermes "$NEMOCLAW_SANDBOX_NAME" status --json >/dev/null 2>&1; then
  NEMOCLAW_SANDBOX_NAME="$NEMOCLAW_SANDBOX_NAME" \
    QUERY_CLAW_RUNTIME_DIR="$HERMES_RUNTIME_DIR" \
    bash "$EXAMPLE_DIR/scripts/teardown.sh"
elif [[ -f "$HERMES_RUNTIME_DIR/query-claw-hermes-profile.json" ]]; then
  die "nemohermes and sandbox $NEMOCLAW_SANDBOX_NAME are required to restore the Hermes profile"
fi
compose down --remove-orphans
rm -f "$RUNTIME_DIR/kumo-qualified.sha256"
printf 'Query Claw services stopped; persistent databases and generated data were retained.\n'
