#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$DIR/_lib.sh"

load_env
[[ "$(sandbox_phase)" == "Ready" ]] || {
  echo "Sandbox $NEMOCLAW_SANDBOX_NAME is not Ready" >&2
  exit 1
}

output_dir="$EXAMPLE_DIR/.traces"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir -p "$output_dir"
timestamp="$(date -u +%Y-%m-%dT%H-%M-%SZ)"
archive="$output_dir/atif-$timestamp.tar.gz"

openshell sandbox download "$NEMOCLAW_SANDBOX_NAME" \
  /sandbox/.hermes-data/atif "$work/" >/dev/null
source_dir="$work"
[[ -d "$work/atif" ]] && source_dir="$work/atif"
COPYFILE_DISABLE=1 tar czf "$archive" -C "$source_dir" .
count="$(find "$source_dir" -type f -name '*.json' | wc -l | tr -d ' ')"
echo "Wrote $archive ($count ATIF file(s))" >&2
[[ "$count" != 0 ]] || echo "No completed Hermes session has produced ATIF yet." >&2
echo "$archive"
