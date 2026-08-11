#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$DIR/_lib.sh"

load_env
require_command openshell

if openshell gateway info --gateway "$OPENSHELL_GATEWAY" >/dev/null 2>&1; then
  openshell gateway select "$OPENSHELL_GATEWAY" >/dev/null
else
  endpoint="$(default_gateway_endpoint)"
  [[ -n "$endpoint" ]] || {
    echo "No registered gateway named $OPENSHELL_GATEWAY and no endpoint configured" >&2
    exit 1
  }
  openshell gateway add "$endpoint" --local --name "$OPENSHELL_GATEWAY"
fi

openshell status >/dev/null || {
  echo "OpenShell gateway $OPENSHELL_GATEWAY is registered but unreachable" >&2
  exit 1
}
echo "OpenShell gateway $OPENSHELL_GATEWAY is healthy"
