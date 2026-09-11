#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

# Keep bearer values in this shell, but out of every child process except the
# one native MCP lifecycle command that consumes the credential.
export -n NEMO_RETRIEVER_API_TOKEN 2>/dev/null || true

EXAMPLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="${QUERY_CLAW_RUNTIME_DIR:-$EXAMPLE_DIR/.runtime}"
NEMOCLAW_SANDBOX_NAME="${NEMOCLAW_SANDBOX_NAME:-query-claw}"
NEMOCLAW_CLI="${NEMOCLAW_CLI:-nemohermes}"
EXPECTED_NEMOCLAW_VERSION="v0.0.123"
EXPECTED_HERMES_VERSION="0.20.6"
EXPECTED_OPENSHELL_VERSION="0.0.106"
PROFILE_RECEIPT="$RUNTIME_DIR/query-claw-hermes-profile.json"
PROFILE_SCRIPT="$(<"$EXAMPLE_DIR/hermes/profile.py")"
SKILL_RECEIPT="$RUNTIME_DIR/query-claw-hermes-skills.json"
SKILL_SCRIPT="$(<"$EXAMPLE_DIR/hermes/skills.py")"
QUERY_CLAW_ENABLE_GSF="${QUERY_CLAW_ENABLE_GSF:-1}"
QUERY_CLAW_ENABLE_RETRIEVER="${QUERY_CLAW_ENABLE_RETRIEVER:-1}"
QUERY_CLAW_ENABLE_PREDICTION="${QUERY_CLAW_ENABLE_PREDICTION:-$QUERY_CLAW_ENABLE_GSF}"
QUERY_CLAW_GSF_MCP_URL="${QUERY_CLAW_GSF_MCP_URL:-}"
QUERY_CLAW_RETRIEVER_COLLECTIONS_JSON="${QUERY_CLAW_RETRIEVER_COLLECTIONS_JSON:-}"
QUERY_CLAW_RETRIEVER_PROVENANCE_JSON="${QUERY_CLAW_RETRIEVER_PROVENANCE_JSON:-}"
QUERY_CLAW_PREDICTION_CONTEXT_JSON=${QUERY_CLAW_PREDICTION_CONTEXT_JSON:-"{}"}
QUERY_CLAW_GSF_OAUTH_ORIGIN="${QUERY_CLAW_GSF_OAUTH_ORIGIN:-}"
QUERY_CLAW_GSF_OAUTH_SCRIPT="${QUERY_CLAW_GSF_OAUTH_SCRIPT:-}"
for enabled in "$QUERY_CLAW_ENABLE_GSF" "$QUERY_CLAW_ENABLE_RETRIEVER" \
  "$QUERY_CLAW_ENABLE_PREDICTION"; do
  [[ "$enabled" == 0 || "$enabled" == 1 ]] || {
    printf 'error: Query Claw source flags must be 0 or 1\n' >&2
    exit 1
  }
done
if (( QUERY_CLAW_ENABLE_PREDICTION && ! QUERY_CLAW_ENABLE_GSF )); then
  printf 'error: Query Claw prediction requires the GSF capability\n' >&2
  exit 1
fi
if [[ -z "$QUERY_CLAW_RETRIEVER_COLLECTIONS_JSON" ]] && \
  (( ! QUERY_CLAW_ENABLE_RETRIEVER )); then
  QUERY_CLAW_RETRIEVER_COLLECTIONS_JSON='{}'
fi

endpoint_vars=(QUERY_CLAW_MCP_URL NEMO_RETRIEVER_API_TOKEN)
provided=0
for endpoint_var in "${endpoint_vars[@]}"; do
  [[ -n "${!endpoint_var:-}" ]] && ((provided += 1))
done
if (( QUERY_CLAW_ENABLE_RETRIEVER && provided != ${#endpoint_vars[@]} )); then
  printf 'error: set QUERY_CLAW_MCP_URL and NEMO_RETRIEVER_API_TOKEN together\n' >&2
  exit 1
fi
if (( QUERY_CLAW_ENABLE_GSF )) && [[ -z "${QUERY_CLAW_GSF_MCP_URL:-}" ]]; then
  printf 'error: set QUERY_CLAW_GSF_MCP_URL to the official GSF MCP endpoint\n' >&2
  exit 1
fi
if { [[ -n "$QUERY_CLAW_GSF_OAUTH_ORIGIN" ]] &&
  [[ -z "$QUERY_CLAW_GSF_OAUTH_SCRIPT" ]]; } ||
  { [[ -z "$QUERY_CLAW_GSF_OAUTH_ORIGIN" ]] &&
    [[ -n "$QUERY_CLAW_GSF_OAUTH_SCRIPT" ]]; }; then
  printf 'error: set both QUERY_CLAW_GSF_OAUTH_ORIGIN and QUERY_CLAW_GSF_OAUTH_SCRIPT\n' >&2
  exit 1
fi
if (( QUERY_CLAW_ENABLE_RETRIEVER )) && \
  [[ -z "${QUERY_CLAW_RETRIEVER_COLLECTIONS_JSON:-}" ]]; then
  printf 'error: set QUERY_CLAW_RETRIEVER_COLLECTIONS_JSON from the active dataset manifest\n' >&2
  exit 1
fi
if (( QUERY_CLAW_ENABLE_RETRIEVER )) && \
  [[ -z "$QUERY_CLAW_RETRIEVER_PROVENANCE_JSON" ]]; then
  printf 'error: set QUERY_CLAW_RETRIEVER_PROVENANCE_JSON from Retriever qualification\n' >&2
  exit 1
fi

(umask 077 && mkdir -p "$RUNTIME_DIR")
chmod 700 "$RUNTIME_DIR"
command -v "$NEMOCLAW_CLI" >/dev/null || {
  printf 'error: %s is not installed; install NemoClaw %s first\n' \
    "$NEMOCLAW_CLI" "$EXPECTED_NEMOCLAW_VERSION" >&2
  exit 1
}
cli_version="$("$NEMOCLAW_CLI" --version 2>/dev/null)" || {
  printf 'error: could not determine the installed NemoClaw version\n' >&2
  exit 1
}
installed_nemoclaw_version="$(
  sed -nE 's/.*v?([0-9]+\.[0-9]+\.[0-9]+).*/v\1/p' <<<"$cli_version"
)"
if [[ "$installed_nemoclaw_version" != "$EXPECTED_NEMOCLAW_VERSION" ]]; then
  printf 'error: Query Claw requires NemoClaw %s; found %s\n' \
    "$EXPECTED_NEMOCLAW_VERSION" "$cli_version" >&2
  exit 1
fi
sandbox_status="$("$NEMOCLAW_CLI" "$NEMOCLAW_SANDBOX_NAME" status --json 2>/dev/null)" || {
  printf "error: sandbox '%s' does not exist; onboard it first\n" \
    "$NEMOCLAW_SANDBOX_NAME" >&2
  exit 1
}
gateway_port="${NEMOCLAW_GATEWAY_PORT:-8080}"
registry_path="$HOME/.nemoclaw/sandboxes.json"
if [[ "$gateway_port" != 8080 ]]; then
  registry_path="$HOME/.nemoclaw/gateways/$gateway_port/sandboxes.json"
fi
if ! python3 -c \
  'import json, pathlib, sys
status=json.load(sys.stdin)
registry=json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
entry=registry.get("sandboxes", {}).get(sys.argv[2], {})
versions=[str(entry.get(key, "")).removeprefix("v") for key in ("nemoclawVersion", "agentVersion")]
versions.append(str(status.get("openshellVersion", "")).removeprefix("v"))
expected=[value.removeprefix("v") for value in sys.argv[3:]]
raise SystemExit(0 if status.get("found") is True and status.get("agent")=="hermes" and entry.get("agent")=="hermes" and versions==expected else 1)' \
  "$registry_path" "$NEMOCLAW_SANDBOX_NAME" \
  "$EXPECTED_NEMOCLAW_VERSION" "$EXPECTED_HERMES_VERSION" \
  "$EXPECTED_OPENSHELL_VERSION" <<<"$sandbox_status" 2>/dev/null; then
  printf 'error: sandbox must use NemoClaw %s, Hermes %s, and OpenShell %s\n' \
    "$EXPECTED_NEMOCLAW_VERSION" "$EXPECTED_HERMES_VERSION" \
    "$EXPECTED_OPENSHELL_VERSION" >&2
  exit 1
fi

registration_name=retriever
registration_url="$QUERY_CLAW_MCP_URL"
registration_env=NEMO_RETRIEVER_API_TOKEN
registration_deny_tools=(
  answer
  get_document
  get_job
  health
  list_job_documents
  pipeline_config
)
registration_state=absent
managed_skill_names=(
  query-claw
  query-claw-structured
  query-claw-predictive
  retriever-mcp
)
desired_skill_names=(query-claw)
(( ! QUERY_CLAW_ENABLE_GSF )) || desired_skill_names+=(query-claw-structured)
(( ! QUERY_CLAW_ENABLE_PREDICTION )) || \
  desired_skill_names+=(query-claw-predictive)
(( ! QUERY_CLAW_ENABLE_RETRIEVER )) || desired_skill_names+=(retriever-mcp)

skill_is_desired() {
  local candidate="$1" name
  for name in "${desired_skill_names[@]}"; do
    [[ "$name" != "$candidate" ]] || return 0
  done
  return 1
}

skill_source_hash() {
  python3 "$EXAMPLE_DIR/hermes/skills.py" hash \
    "$EXAMPLE_DIR/skills/$1"
}

skill_live_hash() {
  "$NEMOCLAW_CLI" "$NEMOCLAW_SANDBOX_NAME" exec --no-tty -- \
    python3 -c "$SKILL_SCRIPT" state "$1"
}

skill_receipt_get() {
  python3 "$EXAMPLE_DIR/hermes/skills.py" receipt-get \
    "$SKILL_RECEIPT" "$1"
}

skill_receipt_set() {
  python3 "$EXAMPLE_DIR/hermes/skills.py" receipt-set \
    "$SKILL_RECEIPT" "$1" "$2"
}

skill_receipt_delete() {
  python3 "$EXAMPLE_DIR/hermes/skills.py" receipt-delete \
    "$SKILL_RECEIPT" "$1"
}

classify_registration() {
  local name="$1" url="$2" token_env="$3" status
  status="$("$NEMOCLAW_CLI" "$NEMOCLAW_SANDBOX_NAME" mcp status "$name" \
    --json --no-probe 2>/dev/null)" || return 2
  python3 -c \
    'import json, pathlib, sys
d=json.load(sys.stdin)
name,url,token_env,private_host,registry_path,sandbox,*expected_deny_tools=sys.argv[1:]
provider=d.get("provider") if isinstance(d.get("provider"),dict) else {}
policy=d.get("policy") if isinstance(d.get("policy"),dict) else {}
adapter=d.get("adapter") if isinstance(d.get("adapter"),dict) else {}
present=bool(d.get("url") or d.get("addState") or provider.get("registryPresent") or policy.get("registryPresent") or adapter.get("registered") is not None)
if not present:
    print("absent")
    raise SystemExit(0)
errors=[]
support=d.get("support") if isinstance(d.get("support"),dict) else {}
if d.get("server") not in (None,name): errors.append("server name differs")
if d.get("agent") != "hermes": errors.append("agent is not hermes")
if support.get("supported") is not True or support.get("mode") != "bridge" or support.get("adapter") != "hermes-config": errors.append("Hermes native bridge support differs")
if d.get("url") != url: errors.append("URL differs")
env=d.get("env") if isinstance(d.get("env"),dict) else {}
if env.get("names") != [token_env]: errors.append("credential environment differs")
target=d.get("trustedPrivateTarget")
if private_host:
    if not isinstance(target,dict) or target.get("host") != private_host: errors.append("trusted private host differs")
    elif not target.get("recordedPins"): errors.append("trusted private host has no recorded pins")
    elif target.get("state") != "match": errors.append("trusted private pins are " + str(target.get("state", "unknown")))
elif target is not None:
    errors.append("unexpected trusted private host")
state=d.get("addState")
if state not in (None,"prepared","preflighted"): errors.append("add transaction state differs")
try:
    registry=json.loads(pathlib.Path(registry_path).read_text(encoding="utf-8"))
    bridge=registry["sandboxes"][sandbox]["mcp"]["bridges"][name]
except (OSError, KeyError, TypeError, json.JSONDecodeError):
    errors.append("durable denied-tool intent is unavailable")
else:
    if bridge.get("denyTools", []) != expected_deny_tools: errors.append("denied tools differ")
    if "pendingDenyTools" in bridge: errors.append("denied-tool update is incomplete")
if "denyTools" in d and d["denyTools"] != expected_deny_tools:
    errors.append("reported denied tools differ")
if errors:
    print("conflict: " + "; ".join(errors))
elif state:
    print("resumable")
else:
    print("current")' \
    "$name" "$url" "$token_env" "${MCP_TRUSTED_PRIVATE_HOST:-}" \
    "$registry_path" "$NEMOCLAW_SANDBOX_NAME" \
    "${registration_deny_tools[@]}" <<<"$status"
}

if (( QUERY_CLAW_ENABLE_RETRIEVER )); then
  registration_state="$(classify_registration "$registration_name" \
    "$registration_url" "$registration_env")" || {
      printf "error: could not inspect native MCP registration '%s'\n" \
        "$registration_name" >&2
      exit 1
    }
  case "$registration_state" in
    absent|resumable|current) ;;
    conflict:*)
      printf "error: native MCP registration '%s' conflicts with Query Claw (%s); remove it explicitly before retrying\n" \
        "$registration_name" "${registration_state#conflict: }" >&2
      exit 1
      ;;
    *)
      printf "error: unrecognized native MCP state for '%s'\n" \
        "$registration_name" >&2
      exit 1
      ;;
  esac
fi

# Prove ownership for every same-name skill before changing any sandbox state.
# Every unowned collision requires explicit user resolution.
python3 "$EXAMPLE_DIR/hermes/skills.py" receipt-validate "$SKILL_RECEIPT"
for name in "${managed_skill_names[@]}"; do
  owned_hash="$(skill_receipt_get "$name")"
  live_hash="$(skill_live_hash "$name")" || {
    printf "error: could not inspect Hermes skill '%s'\n" "$name" >&2
    exit 1
  }
  if [[ "$owned_hash" == unowned ]]; then
    [[ "$live_hash" == absent ]] || {
      printf "error: Hermes skill '%s' already exists without a matching Query Claw ownership receipt; remove it explicitly before retrying\n" \
        "$name" >&2
      exit 1
    }
  elif [[ "$live_hash" != absent && "$live_hash" != "$owned_hash" ]]; then
    printf "error: Query Claw-owned Hermes skill '%s' changed after installation; refusing to replace it\n" \
      "$name" >&2
    exit 1
  fi
done

[[ ! -f "$PROFILE_RECEIPT" ]] || chmod 600 "$PROFILE_RECEIPT"

encode_stdin() {
  python3 -c 'import base64,sys; print(base64.b64encode(sys.stdin.buffer.read()).decode())'
}

run_profile() {
  "$NEMOCLAW_CLI" "$NEMOCLAW_SANDBOX_NAME" exec --no-tty -- \
    env QUERY_CLAW_GSF_MCP_URL="$QUERY_CLAW_GSF_MCP_URL" \
    QUERY_CLAW_RETRIEVER_COLLECTIONS_JSON="$QUERY_CLAW_RETRIEVER_COLLECTIONS_JSON" \
    QUERY_CLAW_RETRIEVER_PROVENANCE_JSON="$QUERY_CLAW_RETRIEVER_PROVENANCE_JSON" \
    QUERY_CLAW_PREDICTION_CONTEXT_JSON="$QUERY_CLAW_PREDICTION_CONTEXT_JSON" \
    QUERY_CLAW_ENABLE_GSF="$QUERY_CLAW_ENABLE_GSF" \
    QUERY_CLAW_ENABLE_RETRIEVER="$QUERY_CLAW_ENABLE_RETRIEVER" \
    QUERY_CLAW_ENABLE_PREDICTION="$QUERY_CLAW_ENABLE_PREDICTION" \
    python3 -c "$PROFILE_SCRIPT" "$@"
}

gsf_oauth_is_current() {
  local output
  output="$("$NEMOCLAW_CLI" "$NEMOCLAW_SANDBOX_NAME" exec \
    --no-tty -- hermes mcp test gsf 2>&1)" || return 1
  grep -Fq 'Connected (' <<<"$output" &&
    grep -Eq 'Tools discovered: [1-9][0-9]*([^0-9]|$)' <<<"$output" &&
    grep -Eq '^[[:space:]]*ask_question([[:space:]]|$)' <<<"$output"
}

complete_gsf_oauth_if_requested() {
  (( QUERY_CLAW_ENABLE_GSF )) || return 0
  [[ -n "$QUERY_CLAW_GSF_OAUTH_SCRIPT" ]] || return 0
  if gsf_oauth_is_current; then
    printf 'configured: existing GSF OAuth login remains valid\n'
    return 0
  fi
  # The deployment wrapper supplies exactly two credential lines on stdin.
  # They never enter argv or the environment and are forwarded only to the
  # isolated, fixed OAuth helper inside the sandbox.
  "$NEMOCLAW_CLI" "$NEMOCLAW_SANDBOX_NAME" exec \
    --no-tty --stdin -- /opt/hermes/.venv/bin/python3 -I \
    -c "$QUERY_CLAW_GSF_OAUTH_SCRIPT" \
    --gsf-origin "$QUERY_CLAW_GSF_OAUTH_ORIGIN"
  gsf_oauth_is_current || {
    printf 'error: official GSF OAuth did not connect to ask_question\n' >&2
    return 1
  }
}

write_profile_receipt() {
  local contents="$1" temporary
  temporary="$(mktemp "$RUNTIME_DIR/.query-claw-hermes-profile.XXXXXX")"
  chmod 600 "$temporary"
  printf '%s\n' "$contents" >"$temporary"
  mv -f "$temporary" "$PROFILE_RECEIPT"
}

created_servers=()
installed_skill_names=()
installed_skill_hashes=()
installed_skill_prior_hashes=()
profile_changed=0
profile_receipt_created=0
rollback() {
  local status=$? name encoded restore_result live_hash prior_hash index
  trap - EXIT
  set +e
  if (( status != 0 )); then
    printf '\nSetup failed; restoring Query Claw-owned skills, the Hermes profile, and new MCP registrations.\n' >&2
    for ((index = ${#installed_skill_names[@]} - 1; index >= 0; index -= 1)); do
      name="${installed_skill_names[index]}"
      live_hash="$(skill_live_hash "$name")" || {
        printf "warning: could not verify newly installed Hermes skill '%s'; retained its ownership receipt\n" \
          "$name" >&2
        status=1
        continue
      }
      if [[ "$live_hash" == "${installed_skill_hashes[index]}" ]]; then
        if ! "$NEMOCLAW_CLI" "$NEMOCLAW_SANDBOX_NAME" skill remove "$name"; then
          printf "warning: could not remove newly installed Hermes skill '%s'; retained its ownership receipt\n" \
            "$name" >&2
          status=1
          continue
        fi
      elif [[ "$live_hash" != absent ]]; then
        printf "warning: newly installed Hermes skill '%s' drifted; retained it and its ownership receipt\n" \
          "$name" >&2
        status=1
        continue
      fi
      prior_hash="${installed_skill_prior_hashes[index]}"
      if [[ "$prior_hash" == unowned ]]; then
        skill_receipt_delete "$name" || status=1
      else
        skill_receipt_set "$name" "$prior_hash" || status=1
      fi
    done
    if (( profile_receipt_created )); then
      if (( profile_changed )); then
        encoded="$(encode_stdin <"$PROFILE_RECEIPT")"
        restore_result="$(run_profile restore "$encoded")"
        if [[ "$restore_result" == restored ]]; then
          "$NEMOCLAW_CLI" "$NEMOCLAW_SANDBOX_NAME" gateway restart --quiet || status=1
          rm -f "$PROFILE_RECEIPT"
        else
          printf 'warning: could not restore the previous Hermes API profile; retained its receipt\n' >&2
          status=1
        fi
      else
        rm -f "$PROFILE_RECEIPT"
      fi
    fi
    for name in "${created_servers[@]}"; do
      if ! "$NEMOCLAW_CLI" "$NEMOCLAW_SANDBOX_NAME" mcp remove "$name" --force; then
        printf "warning: could not remove native MCP registration '%s'; retry hermes/teardown.sh\n" \
          "$name" >&2
        status=1
      fi
    done
  fi
  exit "$status"
}
trap rollback EXIT

if (( ! QUERY_CLAW_ENABLE_RETRIEVER )); then
  :
elif [[ "$registration_state" == current ]]; then
  (
    export NEMO_RETRIEVER_API_TOKEN
    "$NEMOCLAW_CLI" "$NEMOCLAW_SANDBOX_NAME" mcp restart "$registration_name"
  )
else
  [[ "$registration_state" != absent ]] || created_servers+=("$registration_name")
  add_args=(
    "$registration_name" --url "$registration_url" --env "$registration_env"
  )
  for denied_tool in "${registration_deny_tools[@]}"; do
    add_args+=(--deny-tool "$denied_tool")
  done
  if [[ -n "${MCP_TRUSTED_PRIVATE_HOST:-}" ]]; then
    add_args+=(--trusted-private-host "$MCP_TRUSTED_PRIVATE_HOST")
  fi
  add_args+=(--no-probe)
  (
    export NEMO_RETRIEVER_API_TOKEN
    "$NEMOCLAW_CLI" "$NEMOCLAW_SANDBOX_NAME" mcp add "${add_args[@]}"
  )
fi

if [[ -f "$PROFILE_RECEIPT" ]]; then
  encoded_receipt="$(encode_stdin <"$PROFILE_RECEIPT")"
  prepared_receipt="$(run_profile prepare "$encoded_receipt")"
  write_profile_receipt "$prepared_receipt"
else
  snapshot="$(run_profile snapshot)"
  write_profile_receipt "$snapshot"
  profile_receipt_created=1
fi
encoded_receipt="$(encode_stdin <"$PROFILE_RECEIPT")"
profile_result="$(run_profile apply "$encoded_receipt")"
if [[ "$profile_result" == changed ]]; then
  profile_changed=1
elif [[ "$profile_result" != unchanged ]]; then
  printf 'error: Hermes profile helper returned an unexpected result\n' >&2
  exit 1
fi
run_profile verify "$encoded_receipt" >/dev/null

# Keep OAuth inside this transaction. If authentication or exact tool
# discovery fails, the trap below restores the profile, skills, and native MCP
# registrations instead of leaving a partially configured agent.
complete_gsf_oauth_if_requested

# Reconcile only skills proved absent or Query-Claw-owned in the complete
# preflight above. Install enabled skills first; then remove exact-owned skills
# disabled by the active dataset. A new chat picks up the resulting inventory.
for name in "${desired_skill_names[@]}"; do
  source_hash="$(skill_source_hash "$name")"
  owned_hash="$(skill_receipt_get "$name")"
  live_hash="$(skill_live_hash "$name")"
  [[ "$live_hash" != "$source_hash" ]] || continue
  if [[ "$live_hash" != absent ]]; then
    [[ "$owned_hash" != unowned && "$live_hash" == "$owned_hash" ]] || {
      printf "error: Hermes skill '%s' changed after preflight; refusing to replace it\n" \
        "$name" >&2
      exit 1
    }
    "$NEMOCLAW_CLI" "$NEMOCLAW_SANDBOX_NAME" skill remove "$name"
  fi
  "$NEMOCLAW_CLI" "$NEMOCLAW_SANDBOX_NAME" skill install \
    "$EXAMPLE_DIR/skills/$name"
  installed_skill_names+=("$name")
  installed_skill_hashes+=("$source_hash")
  installed_skill_prior_hashes+=("$owned_hash")
  skill_receipt_set "$name" "$source_hash"
  [[ "$(skill_live_hash "$name")" == "$source_hash" ]] || {
    printf "error: Hermes skill '%s' does not match the installed recipe bytes\n" \
      "$name" >&2
    exit 1
  }
done

for name in "${managed_skill_names[@]}"; do
  skill_is_desired "$name" && continue
  owned_hash="$(skill_receipt_get "$name")"
  [[ "$owned_hash" != unowned ]] || continue
  live_hash="$(skill_live_hash "$name")"
  if [[ "$live_hash" == "$owned_hash" ]]; then
    "$NEMOCLAW_CLI" "$NEMOCLAW_SANDBOX_NAME" skill remove "$name"
  elif [[ "$live_hash" != absent ]]; then
    printf "error: disabled Query Claw skill '%s' changed after preflight; refusing to remove it\n" \
      "$name" >&2
    exit 1
  fi
  skill_receipt_delete "$name"
done

# Dataset activation intentionally recreates GSF and its private ingress. Even
# when the profile bytes are unchanged, replace the resident gateway's stale
# MCP transport only after the endpoint, OAuth session, and skills are ready.
if (( profile_changed || QUERY_CLAW_ENABLE_GSF )); then
  "$NEMOCLAW_CLI" "$NEMOCLAW_SANDBOX_NAME" gateway restart --quiet
fi

# NemoClaw seals Hermes' complete config as part of managed MCP lifecycle
# state. The Query Claw profile intentionally updates that same config after
# mcp add/restart, so validate the native adapter only after the gateway
# restart has adopted the final profile snapshot.
NEMOCLAW_CLI="$NEMOCLAW_CLI" \
  python3 "$EXAMPLE_DIR/scripts/verify.py" --live \
    --sandbox "$NEMOCLAW_SANDBOX_NAME"

trap - EXIT

if (( QUERY_CLAW_ENABLE_GSF )) && [[ -z "$QUERY_CLAW_GSF_OAUTH_SCRIPT" ]]; then
  printf '\nQuery Claw is installed in %s. Authenticate the official GSF MCP once with:\n' \
    "$NEMOCLAW_SANDBOX_NAME"
  printf '  %s %s exec -- hermes mcp login gsf\n' \
    "$NEMOCLAW_CLI" "$NEMOCLAW_SANDBOX_NAME"
elif (( ! QUERY_CLAW_ENABLE_GSF )); then
  printf '\nQuery Claw is installed in %s; this activation does not expose GSF.\n' \
    "$NEMOCLAW_SANDBOX_NAME"
fi
printf 'Then start a new chat to load the authenticated tools and updated skills.\n'
