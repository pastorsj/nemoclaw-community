# shellcheck shell=bash
# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# The Hermes sandbox on OpenShell, following infra/openshell/README.md: the gateway, provider
# profiles and providers, sandbox reconcile, the forwarder, teardown and the leaked-volume recipe.
# The openshell CLI and jq run in the openshell-cli container; the host needs only Docker and curl.
# shellcheck disable=SC2016 # single-quoted scripts expand in the container's shell

readonly SANDBOX=hermes
readonly AGENT_IMAGE=market-demo/hermes-sandbox:local
# The MCP servers' images: Hermes lists their tools once, when the sandbox starts.
readonly TOOL_IMAGES=(market-demo/retrieval:local market-demo/market-analytics:local market-demo/market-analytics:gpu
  market-demo/auto-ontology-mcp:local)
readonly NAMESPACE_LABEL=openshell.ai/sandbox-namespace=market-demo # gateway.toml sandbox_label
readonly SANDBOX_TIMEOUT=180
readonly STATE_DIR=$ROOT/.demo
readonly VOLUMES_STATE=$STATE_DIR/openshell-volumes

cli() {
  dc --progress quiet run --rm -T openshell-cli "$@"
}

# cli_sh SCRIPT [ARG...]: a POSIX shell script in the CLI container, which has openshell and jq.
cli_sh() {
  local script=$1
  shift
  dc --progress quiet run --rm -T --entrypoint sh openshell-cli -c "$script" sh "$@"
}

# Lifecycle state kept between runs (the leaked-volume records), ignored by git.
state_dir() {
  mkdir -p "$STATE_DIR" && echo '*' >"$STATE_DIR/.gitignore"
}

gateway_ready() {
  curl -fs -o /dev/null --max-time 3 http://127.0.0.1:18081/readyz
}

# "<fingerprint label> <phase>" of the sandbox, or nothing when there is none.
sandbox_state() {
  cli_sh 'openshell sandbox get "$1" -o json 2>/dev/null | jq -r "$2"' "$SANDBOX" \
    '(.labels["demo.fingerprint"] // "-") + " " + .phase'
}

# Whatever the sandbox depends on. A change to any of it recreates the sandbox. Both Hermes keys
# are in it: the server key is set at creation, and a new receipt key moves the provider revision.
# Hermes lists each MCP server's tools once, when it starts, so the tool images, the data pack and
# its profile are in it too: they shape the tool schemas (the universes, the relationship graph, the
# minute bars). After a DATA_PACK or DATA_PACK_PROFILE switch the model would otherwise still see
# the previous build's tools.
sandbox_fingerprint() {
  {
    docker image inspect -f '{{.Id}}' "$AGENT_IMAGE"
    docker image inspect -f '{{.Id}}' "${TOOL_IMAGES[@]}" 2>/dev/null || true # absent profiles
    cat "$ROOT/data/packs/$DATA_PACK/pack.yaml"
    echo "${DATA_PACK_PROFILE:-}"
    cat "$ROOT"/infra/openshell/providers/*.yaml "$ROOT/infra/openshell/gateway.toml" "$VERSIONS_FILE"
    echo "$AGENT_FEATURES"
    printf '%s' "$HERMES_API_SERVER_KEY" | sha256
    printf '%s' "$HERMES_RECEIPT_API_KEY" | sha256
  } | sha256 | cut -c1-16
}

# openshell_up [--recreate]: gateway, profiles, providers, then a Ready sandbox and the forwarder.
# Everything the sandbox calls must already be up: Hermes parks an MCP server it cannot reach.
openshell_up() {
  local recreate=false want have phase image
  [ "${1:-}" != --recreate ] || recreate=true
  state_dir

  # Pre-pull the runtime images, or the gateway pulls them while creating the sandbox.
  for image in "$OPENSHELL_SUPERVISOR_IMAGE" "$OPENSHELL_SANDBOX_IMAGE"; do
    docker image inspect "$image" >/dev/null 2>&1 || docker pull -q "$image" >/dev/null
  done
  log "starting the OpenShell gateway"
  dc up -d openshell # openshell-certs and openshell-preflight run first and must exit 0
  wait_for 60 gateway_ready ||
    die "$EXIT_UNAVAILABLE" "the gateway is not ready: docker compose logs openshell-preflight openshell"

  want=$(sandbox_fingerprint)
  read -r have phase <<<"$(sandbox_state)"
  if [ "$have" != "$want" ]; then
    recreate=true
  fi
  openshell_providers "$recreate"
  if ! $recreate && [ "$phase" = Ready ]; then
    log "sandbox $SANDBOX is Ready and current"
  else
    dc stop hermes-gateway
    if [ -n "$phase" ]; then
      sandbox_delete
    fi
    sandbox_create "$want"
  fi
  log "starting the Hermes forwarder"
  dc up -d --wait hermes-gateway
}

# Import missing provider profiles and create missing providers. `profile import` is create-only,
# so on a recreate existing profiles are updated in place and the receipt key is re-applied.
openshell_providers() {
  local update=$1 file id
  for file in "$ROOT"/infra/openshell/providers/*.yaml; do
    id=$(basename "$file" .yaml)
    if ! cli profile describe "$id" >/dev/null 2>&1; then
      log "importing provider profile $id"
      cli profile lint -f "/providers/$id.yaml" >/dev/null # lint also rejects an existing id
      cli profile import -f "/providers/$id.yaml" >/dev/null
    elif $update; then
      log "updating provider profile $id"
      cli_sh 'v=$(openshell profile export "$1" -o json | jq -r .resource_version) &&
        { echo "resource_version: $v"; cat "/providers/$1.yaml"; } >/tmp/profile.yaml &&
        openshell profile update "$1" --file /tmp/profile.yaml' "$id" >/dev/null
    fi
  done
  cli provider get switchyard >/dev/null 2>&1 || cli provider create --name switchyard --type switchyard >/dev/null
  # --credential names an environment variable, so the key stays off every command line.
  if ! cli provider get receipts >/dev/null 2>&1; then
    HERMES_RECEIPT_API_KEY=$HERMES_RECEIPT_API_KEY dc --progress quiet run --rm -T -e HERMES_RECEIPT_API_KEY \
      openshell-cli provider create --name receipts --type receipts --credential HERMES_RECEIPT_API_KEY >/dev/null
  elif $update; then
    HERMES_RECEIPT_API_KEY=$HERMES_RECEIPT_API_KEY dc --progress quiet run --rm -T -e HERMES_RECEIPT_API_KEY \
      openshell-cli provider update receipts --credential HERMES_RECEIPT_API_KEY >/dev/null
  fi
}

sandbox_create() {
  log "creating sandbox $SANDBOX (fingerprint $1)"
  dangling_volumes >"$VOLUMES_STATE.before"
  # API_SERVER_KEY cannot be a provider placeholder: Hermes checks it on inbound requests.
  HERMES_API_SERVER_KEY=$HERMES_API_SERVER_KEY dc --progress quiet run --rm -T -e HERMES_API_SERVER_KEY \
    --entrypoint sh openshell-cli -c 'exec openshell sandbox create --name hermes \
      --from market-demo/hermes-sandbox:local --provider switchyard --provider receipts --no-auto-providers \
      --label "demo.fingerprint=$1" --env "API_SERVER_KEY=$HERMES_API_SERVER_KEY" \
      --no-credential-warnings --detach --no-tty -- /opt/hermes/.venv/bin/hermes gateway run' sh "$1" >/dev/null
  if ! cli_sh 'for _ in $(seq "$1"); do
      [ "$(openshell sandbox get hermes -o json 2>/dev/null | jq -r .phase)" = Ready ] && exit 0
      sleep 2
    done
    exit 1' $((SANDBOX_TIMEOUT / 2)); then
    cli logs "$SANDBOX" --since 5m >&2 || true
    die "$EXIT_UNAVAILABLE" "sandbox $SANDBOX is not Ready after ${SANDBOX_TIMEOUT}s"
  fi
  # Leaked volumes, step 1: once Ready, the identity resolver's volume is the new dangling one.
  comm -13 "$VOLUMES_STATE.before" <(dangling_volumes) >>"$VOLUMES_STATE"
  rm -f "$VOLUMES_STATE.before"
}

sandbox_delete() {
  log "deleting sandbox $SANDBOX"
  record_workload_volume
  cli sandbox delete "$SANDBOX" >/dev/null
  cli_sh 'for _ in $(seq 60); do openshell sandbox get hermes >/dev/null 2>&1 || exit 0; sleep 2; done; exit 1' ||
    die "$EXIT_UNAVAILABLE" "sandbox $SANDBOX still exists after 120 s"
  wait_for 60 no_sandbox_containers || warn "sandbox containers are still being removed"
  remove_leaked_volumes
}

# Teardown: delete the sandbox through the gateway, then remove anything this namespace left
# behind (when the gateway was already gone). Other OpenShell namespaces are never touched.
openshell_down() {
  state_dir
  if gateway_ready && [ -n "$(sandbox_state)" ]; then
    dc stop hermes-gateway
    sandbox_delete
  fi
  local ids
  ids=$(docker ps -aq --filter "label=$NAMESPACE_LABEL")
  if [ -n "$ids" ]; then
    warn "removing sandbox containers the gateway left behind"
    record_workload_volume
    # shellcheck disable=SC2086 # one id per word
    docker rm -f $ids >/dev/null
  fi
  ids=$(docker volume ls -q --filter "label=$NAMESPACE_LABEL")
  # shellcheck disable=SC2086
  [ -z "$ids" ] || docker volume rm $ids >/dev/null
  remove_leaked_volumes
}

no_sandbox_containers() {
  [ -z "$(docker ps -aq --filter "label=$NAMESPACE_LABEL")" ]
}

# OpenShell 0.1.2 removes sandbox containers without their anonymous /opt/data volumes. Record
# their names while they are still known, and remove only those (infra/openshell/README.md,
# "Leaked volumes"). Never sweep dangling volumes: the Docker host is shared.
dangling_volumes() {
  docker volume ls -q -f dangling=true | sort
}

# Step 2, just before a delete: the workload's /opt/data volume, from its labelled container.
record_workload_volume() {
  docker ps -aq --filter "label=$NAMESPACE_LABEL" --filter "label=openshell.ai/sandbox-name=$SANDBOX" \
    --filter label=openshell.ai/isolation-role=sandbox | while read -r container; do
    docker inspect -f '{{range .Mounts}}{{if eq .Destination "/opt/data"}}{{.Name}}{{end}}{{end}}' "$container"
  done >>"$VOLUMES_STATE"
}

# Step 3, once the containers are gone: remove each recorded volume that holds this profile.
remove_leaked_volumes() {
  [ -f "$VOLUMES_STATE" ] || return 0
  local volume
  while read -r volume; do
    [ -n "$volume" ] || continue
    docker volume inspect "$volume" >/dev/null 2>&1 </dev/null || continue # `docker run -v` would create it
    if docker run --rm --pull never --network none -v "$volume:/v:ro" --entrypoint grep "$AGENT_IMAGE" \
      -qsx 'name: market-analysis-agent' /v/distribution.yaml </dev/null; then
      docker volume rm "$volume" >/dev/null </dev/null
    fi
  done <"$VOLUMES_STATE"
  rm -f "$VOLUMES_STATE" "$VOLUMES_STATE.before"
}

# check: prove the sandbox boundary on a running stack. Runs as Hermes' interpreter, the only
# binary the policy allows, so every result reflects what the agent can and cannot reach.
cmd_check() {
  [ $# -eq 0 ] || die "$EXIT_USAGE" "usage: demo.sh check"
  load_env
  gateway_ready || die "$EXIT_UNAVAILABLE" "the OpenShell gateway is not running: ./scripts/demo.sh up"
  local phase
  read -r _ phase <<<"$(sandbox_state)"
  [ "$phase" = Ready ] || die 1 "sandbox $SANDBOX is ${phase:-missing}, not Ready"
  log "sandbox $SANDBOX is Ready"
  cli sandbox exec --name "$SANDBOX" --no-tty -- /usr/bin/python3.13 - <<'EOF'
import os
import urllib.error
import urllib.request

HOST = "http://host.openshell.internal"
failures = 0


def request(url, method="GET", body=b"{}", headers=None):
    """(status, body) of one request; (None, reason) when the connection itself is refused."""
    headers = {"content-type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=body if method == "POST" else None, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, response.read(2000).decode(errors="replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read(2000).decode(errors="replace")
    except OSError as error:
        return None, str(error)


def check(ok, claim):
    global failures
    failures += not ok
    print(("PASS  " if ok else "FAIL  ") + claim)


def denied(url, method="GET", body=b"{}", headers=None):
    status, body = request(url, method, body, headers)
    return status is None or (status == 403 and "policy_denied" in body)


# A well-formed MCP request, so a denial is the path rule's and not the protocol check's
MCP = {"MCP-Protocol-Version": "2025-11-25", "accept": "application/json, text/event-stream"}
TOOLS_LIST = b'{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}'


key = os.environ.get("HERMES_RECEIPT_API_KEY", "")
check(key.startswith("openshell:resolve:env:"), "the receipt key in the sandbox is an OpenShell placeholder")
check(denied("https://example.com/"), "egress to a host outside the policy is blocked")
check(request(f"{HOST}:4000/v1/models")[0] == 200, "Switchyard GET /v1/models is allowed")
check(denied(f"{HOST}:4000/v1/responses", "POST"), "Switchyard POST /v1/responses is denied by policy")
check(denied(f"{HOST}:8000/v1/pack"), "the job API's public routes are denied by policy")
check(
    denied(f"{HOST}:3010/benchmark", "POST", TOOLS_LIST, MCP),
    "market analytics' POST /benchmark (the API's) is denied by policy",
)
for method, route in (("GET", "execution-scope"), ("POST", "tool-receipts"), ("POST", "llm-calls")):
    url = f"{HOST}:8000/internal/hermes/jobs/check/{route}"
    check(not denied(url, method), f"the plugin's {method} .../{route} reaches the job API")
raise SystemExit(1 if failures else 0)
EOF
}
