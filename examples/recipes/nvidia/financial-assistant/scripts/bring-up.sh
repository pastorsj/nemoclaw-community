#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Phase 1/4: local Phoenix"
bash "$DIR/00-host-services.sh" up
echo "Phase 2/4: OpenShell gateway"
bash "$DIR/01-gateway.sh"
echo "Phase 3/4: inference provider"
bash "$DIR/02-provider.sh"
echo "Phase 4/4: financial-assistant sandbox"
bash "$DIR/03-sandbox.sh" "$@"
