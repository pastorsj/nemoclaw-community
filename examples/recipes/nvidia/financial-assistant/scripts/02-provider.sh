#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$DIR/_lib.sh"

load_env
require_runtime_config
require_command openshell
mkdir -p "$STATE_DIR"

# OpenShell 0.0.85 exposes this as a gateway-wide route and has no
# machine-readable restore operation. Preserve a human-readable snapshot once
# so an operator on a shared gateway can restore it deliberately.
if [[ ! -e "$STATE_DIR/inference-route-before.txt" ]]; then
  openshell inference get 2>&1 \
    | sed $'s/\x1b\\[[0-9;]*m//g' \
    >"$STATE_DIR/inference-route-before.txt" || true
fi

if ! openshell settings get --global 2>/dev/null \
  | grep -qE 'providers_v2_enabled[[:space:]]*=[[:space:]]*true'; then
  echo "OpenShell provider v2 support is not enabled. Run:" >&2
  echo "  openshell settings set --global --key providers_v2_enabled --value true --yes" >&2
  exit 1
fi

provider="financial-assistant-inference"
if openshell provider get "$provider" >/dev/null 2>&1 \
  && ! provider_type_matches "$provider" nvidia; then
  openshell provider delete "$provider" >/dev/null
fi

if openshell provider get "$provider" >/dev/null 2>&1; then
  env -i HOME="$HOME" PATH="$PATH" NVIDIA_API_KEY="$NVIDIA_API_KEY" \
    openshell provider update "$provider" \
      --credential NVIDIA_API_KEY --config "NVIDIA_BASE_URL=$NEMOCLAW_ENDPOINT_URL"
else
  env -i HOME="$HOME" PATH="$PATH" NVIDIA_API_KEY="$NVIDIA_API_KEY" \
    openshell provider create --name "$provider" --type nvidia \
      --credential NVIDIA_API_KEY --config "NVIDIA_BASE_URL=$NEMOCLAW_ENDPOINT_URL"
fi

openshell inference set --no-verify --provider "$provider" --model "$NEMOCLAW_MODEL"
echo "Inference route ready: $provider ($NEMOCLAW_MODEL)"
