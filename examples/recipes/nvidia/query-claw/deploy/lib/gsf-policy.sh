#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# Shared ownership checks for Query Claw's custom GSF OAuth policy. Callers
# provide NEMOCLAW_CLI, NEMOCLAW_SANDBOX_NAME, PROFILE_SCRIPT,
# GSF_POLICY_FILE, and GSF_POLICY_MARKER.

gsf_policy_encode_file() {
  python3 -c 'import base64,sys; print(base64.b64encode(sys.stdin.buffer.read()).decode())' \
    <"$1"
}

gsf_policy_run_helper() {
  "$NEMOCLAW_CLI" "$NEMOCLAW_SANDBOX_NAME" exec --no-tty -- \
    python3 -c "$PROFILE_SCRIPT" "$@"
}

gsf_policy_digest() {
  local encoded
  encoded="$(gsf_policy_encode_file "$1")" || return
  gsf_policy_run_helper policy-digest "$encoded"
}

gsf_policy_state() {
  local expected="${1:-$GSF_POLICY_MARKER}" current current_encoded expected_encoded
  local -a classify_args
  current="$(
    "$NEMOCLAW_CLI" "$NEMOCLAW_SANDBOX_NAME" policy get 2>/dev/null
  )" || return
  current_encoded="$(printf '%s\n' "$current" | \
    python3 -c 'import base64,sys; print(base64.b64encode(sys.stdin.buffer.read()).decode())'
  )" || return
  expected_encoded="$(gsf_policy_encode_file "$expected")" || return
  classify_args=(policy-classify "$current_encoded" "$expected_encoded")
  [[ -z "${QUERY_CLAW_PRIVATE_IP:-}" ]] || \
    classify_args+=("$QUERY_CLAW_PRIVATE_IP")
  gsf_policy_run_helper "${classify_args[@]}"
}

gsf_policy_definitions_match() {
  local left right
  left="$(gsf_policy_digest "$1")" || return
  right="$(gsf_policy_digest "$2")" || return
  [[ "$left" == "$right" ]]
}

# Remove the policy only when its complete namespaced live definition still
# matches the private receipt. An already-absent policy is a safe no-op.
gsf_policy_remove_exact() {
  local state
  [[ -s "$GSF_POLICY_MARKER" ]] || {
    printf 'error: Query Claw GSF policy receipt is missing or empty\n' >&2
    return 1
  }
  state="$(gsf_policy_state "$GSF_POLICY_MARKER")" || {
    printf 'error: could not inspect the live Query Claw GSF policy\n' >&2
    return 1
  }
  case "$state" in
    absent)
      return 0
      ;;
    match)
      "$NEMOCLAW_CLI" "$NEMOCLAW_SANDBOX_NAME" policy remove \
        query-claw-gsf-oauth --yes || return
      [[ "$(gsf_policy_state "$GSF_POLICY_MARKER")" == absent ]] || {
        printf 'error: Query Claw GSF policy remains after removal\n' >&2
        return 1
      }
      ;;
    drift)
      printf 'error: Query Claw GSF policy drifted; refusing to remove operator state\n' >&2
      return 1
      ;;
    *)
      printf 'error: unrecognized Query Claw GSF policy state: %s\n' "$state" >&2
      return 1
      ;;
  esac
}
