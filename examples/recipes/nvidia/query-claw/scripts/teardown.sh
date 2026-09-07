#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

EXAMPLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="${QUERY_CLAW_RUNTIME_DIR:-$EXAMPLE_DIR/.runtime}"
NEMOCLAW_SANDBOX_NAME="${NEMOCLAW_SANDBOX_NAME:-query-claw}"
NEMOCLAW_CLI="${NEMOCLAW_CLI:-nemohermes}"
PROFILE_RECEIPT="$RUNTIME_DIR/query-claw-hermes-profile.json"
PROFILE_SCRIPT="$(<"$EXAMPLE_DIR/scripts/hermes_profile.py")"
registration_names=(query-claw)
skill_names=(
  query-claw
  query-claw-structured
  query-claw-documents
  query-claw-predictive
  query-claw-reporting
)

export -n QUERY_CLAW_MCP_TOKEN 2>/dev/null || true

if ! command -v "$NEMOCLAW_CLI" >/dev/null; then
  if [[ -f "$PROFILE_RECEIPT" ]]; then
    printf 'error: %s is not installed; retained the Hermes profile receipt\n' \
      "$NEMOCLAW_CLI" >&2
    exit 1
  fi
  printf 'Nothing removed: %s is not installed.\n' "$NEMOCLAW_CLI"
  exit 0
fi
if ! "$NEMOCLAW_CLI" "$NEMOCLAW_SANDBOX_NAME" status --json >/dev/null 2>&1; then
  if [[ -f "$PROFILE_RECEIPT" ]]; then
    printf "error: sandbox '%s' is unavailable; retained its Hermes profile receipt\n" \
      "$NEMOCLAW_SANDBOX_NAME" >&2
    exit 1
  fi
  printf "Nothing removed: sandbox '%s' is not available.\n" "$NEMOCLAW_SANDBOX_NAME"
  exit 0
fi

encode_stdin() {
  python3 -c 'import base64,sys; print(base64.b64encode(sys.stdin.buffer.read()).decode())'
}

run_profile() {
  "$NEMOCLAW_CLI" "$NEMOCLAW_SANDBOX_NAME" exec --no-tty -- \
    python3 -c "$PROFILE_SCRIPT" "$@"
}

present_servers=()
for name in "${registration_names[@]}"; do
  status="$("$NEMOCLAW_CLI" "$NEMOCLAW_SANDBOX_NAME" mcp status "$name" \
    --json --no-probe)" || {
      printf "error: could not inspect native MCP registration '%s'\n" "$name" >&2
      exit 1
    }
  if python3 -c \
    'import json,sys
d=json.load(sys.stdin)
p=d.get("provider") if isinstance(d.get("provider"),dict) else {}
q=d.get("policy") if isinstance(d.get("policy"),dict) else {}
a=d.get("adapter") if isinstance(d.get("adapter"),dict) else {}
present=bool(d.get("url") or d.get("addState") or p.get("registryPresent") or q.get("registryPresent") or a.get("registered") is not None)
raise SystemExit(0 if present else 1)' <<<"$status"; then
    present_servers+=("$name")
  fi
done

encoded_receipt=""
if [[ -f "$PROFILE_RECEIPT" ]]; then
  chmod 600 "$PROFILE_RECEIPT"
  encoded_receipt="$(encode_stdin <"$PROFILE_RECEIPT")"
  run_profile restore-check "$encoded_receipt" >/dev/null
fi

removed_skills=()
for name in "${skill_names[@]}"; do
  set +e
  output="$("$NEMOCLAW_CLI" "$NEMOCLAW_SANDBOX_NAME" skill remove "$name" 2>&1)"
  result=$?
  set -e
  if (( result == 0 )); then
    removed_skills+=("$name")
  elif ! grep -Fq "Skill '$name' is not installed" <<<"$output"; then
    printf '%s\n' "$output" >&2
    exit "$result"
  fi
done

removed_servers=()
for name in "${present_servers[@]}"; do
  "$NEMOCLAW_CLI" "$NEMOCLAW_SANDBOX_NAME" mcp remove "$name"
  removed_servers+=("$name")
done

profile_restored=0
if [[ -n "$encoded_receipt" ]]; then
  restore_result="$(run_profile restore "$encoded_receipt")"
  if [[ "$restore_result" == restored || "$restore_result" == already-restored ]]; then
    profile_restored=1
    "$NEMOCLAW_CLI" "$NEMOCLAW_SANDBOX_NAME" gateway restart --quiet
  else
    printf 'error: Hermes profile helper returned an unexpected restore result\n' >&2
    exit 1
  fi
  rm -f "$PROFILE_RECEIPT"
fi

if (( ${#removed_servers[@]} )); then
  printf 'Removed native MCP registrations from %s: %s.\n' \
    "$NEMOCLAW_SANDBOX_NAME" "${removed_servers[*]}"
elif (( ${#removed_skills[@]} || profile_restored )); then
  printf 'Removed the remaining Query Claw Hermes configuration from %s.\n' \
    "$NEMOCLAW_SANDBOX_NAME"
else
  printf 'No Query Claw native MCP registrations or profile receipt remained.\n'
fi
printf 'The sandbox and generated local data were left in place.\n'
