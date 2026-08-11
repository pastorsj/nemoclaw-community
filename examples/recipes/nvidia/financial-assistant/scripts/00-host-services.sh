#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$DIR/_lib.sh"

require_command docker
COMPOSE_FILE="$EXAMPLE_DIR/observability/phoenix-compose.yml"

case "${1:-up}" in
  up)
    docker compose -f "$COMPOSE_FILE" up -d
    for _ in $(seq 1 60); do
      curl -fsS http://127.0.0.1:6006/healthz >/dev/null 2>&1 && {
        echo "Phoenix ready at http://127.0.0.1:6006"
        exit 0
      }
      sleep 1
    done
    docker compose -f "$COMPOSE_FILE" logs --tail=80 phoenix >&2 || true
    echo "Phoenix did not become healthy" >&2
    exit 1
    ;;
  down)
    shift || true
    case "${1:-}" in
      "") docker compose -f "$COMPOSE_FILE" down ;;
      --volumes) docker compose -f "$COMPOSE_FILE" down --volumes ;;
      *) echo "Usage: $(basename "$0") [up|down [--volumes]]" >&2; exit 2 ;;
    esac
    ;;
  *) echo "Usage: $(basename "$0") [up|down [--volumes]]" >&2; exit 2 ;;
esac
