#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

# Keep bearer values in this shell, but out of every child process except the
# one native MCP lifecycle command that consumes the credential.
export -n QUERY_CLAW_MCP_TOKEN 2>/dev/null || true

EXAMPLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="${QUERY_CLAW_RUNTIME_DIR:-$EXAMPLE_DIR/.runtime}"
NEMOCLAW_SANDBOX_NAME="${NEMOCLAW_SANDBOX_NAME:-query-claw}"
NEMOCLAW_CLI="${NEMOCLAW_CLI:-nemohermes}"
EXPECTED_NEMOCLAW_VERSION="v0.0.120"
EXPECTED_HERMES_VERSION="0.20.6"
EXPECTED_OPENSHELL_VERSION="0.0.106"
PROFILE_RECEIPT="$RUNTIME_DIR/query-claw-hermes-profile.json"
PROFILE_SCRIPT="$(<"$EXAMPLE_DIR/scripts/hermes_profile.py")"

endpoint_vars=(QUERY_CLAW_MCP_URL QUERY_CLAW_MCP_TOKEN)
provided=0
for endpoint_var in "${endpoint_vars[@]}"; do
  [[ -n "${!endpoint_var:-}" ]] && ((provided += 1))
done
if (( provided != ${#endpoint_vars[@]} )); then
  printf 'error: set QUERY_CLAW_MCP_URL and QUERY_CLAW_MCP_TOKEN together\n' >&2
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

python3 "$EXAMPLE_DIR/scripts/generate_data.py" --validate

registration_name=query-claw
registration_url="$QUERY_CLAW_MCP_URL"
registration_env=QUERY_CLAW_MCP_TOKEN
skill_names=(
  query-claw
  query-claw-structured
  query-claw-documents
  query-claw-predictive
  query-claw-reporting
)

classify_registration() {
  local name="$1" url="$2" token_env="$3" status
  status="$("$NEMOCLAW_CLI" "$NEMOCLAW_SANDBOX_NAME" mcp status "$name" \
    --json --no-probe 2>/dev/null)" || return 2
  python3 -c \
    'import json, sys
d=json.load(sys.stdin)
name,url,token_env,private_host=sys.argv[1:]
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
if errors:
    print("conflict: " + "; ".join(errors))
elif state:
    print("resumable")
else:
    print("current")' \
    "$name" "$url" "$token_env" "${MCP_TRUSTED_PRIVATE_HOST:-}" <<<"$status"
}

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

[[ ! -f "$PROFILE_RECEIPT" ]] || chmod 600 "$PROFILE_RECEIPT"

encode_stdin() {
  python3 -c 'import base64,sys; print(base64.b64encode(sys.stdin.buffer.read()).decode())'
}

run_profile() {
  "$NEMOCLAW_CLI" "$NEMOCLAW_SANDBOX_NAME" exec --no-tty -- \
    python3 -c "$PROFILE_SCRIPT" "$@"
}

write_profile_receipt() {
  local contents="$1" temporary
  temporary="$(mktemp "$RUNTIME_DIR/.query-claw-hermes-profile.XXXXXX")"
  chmod 600 "$temporary"
  printf '%s\n' "$contents" >"$temporary"
  mv -f "$temporary" "$PROFILE_RECEIPT"
}

created_servers=()
profile_changed=0
profile_receipt_created=0
rollback() {
  local status=$? name encoded restore_result
  trap - EXIT
  set +e
  if (( status != 0 )); then
    printf '\nSetup failed; restoring the Hermes profile and new MCP registrations.\n' >&2
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
        printf "warning: could not remove native MCP registration '%s'; retry scripts/teardown.sh\n" \
          "$name" >&2
        status=1
      fi
    done
  fi
  exit "$status"
}
trap rollback EXIT

# Hermes loads native skills when the next chat session starts. Reinstalling a
# same-name Hermes skill is the supported update path and requires no restart.
for name in "${skill_names[@]}"; do
  "$NEMOCLAW_CLI" "$NEMOCLAW_SANDBOX_NAME" skill install \
    "$EXAMPLE_DIR/skills/$name"
done

if [[ "$registration_state" == current ]]; then
  (
    export QUERY_CLAW_MCP_TOKEN
    "$NEMOCLAW_CLI" "$NEMOCLAW_SANDBOX_NAME" mcp restart "$registration_name"
  )
else
  [[ "$registration_state" != absent ]] || created_servers+=("$registration_name")
  add_args=(
    "$registration_name" --url "$registration_url" --env "$registration_env"
  )
  if [[ -n "${MCP_TRUSTED_PRIVATE_HOST:-}" ]]; then
    add_args+=(--trusted-private-host "$MCP_TRUSTED_PRIVATE_HOST")
  fi
  add_args+=(--no-probe)
  (
    export QUERY_CLAW_MCP_TOKEN
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
  "$NEMOCLAW_CLI" "$NEMOCLAW_SANDBOX_NAME" gateway restart --quiet
elif [[ "$profile_result" != unchanged ]]; then
  printf 'error: Hermes profile helper returned an unexpected result\n' >&2
  exit 1
fi
run_profile verify "$encoded_receipt" >/dev/null

NEMOCLAW_CLI="$NEMOCLAW_CLI" \
  python3 "$EXAMPLE_DIR/scripts/verify.py" --live \
    --sandbox "$NEMOCLAW_SANDBOX_NAME"

trap - EXIT

printf '\nQuery Claw is installed and verified in %s. Start a new chat to load updated skills.\n' \
  "$NEMOCLAW_SANDBOX_NAME"
