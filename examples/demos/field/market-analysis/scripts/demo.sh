#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# The demo's lifecycle. Run ./scripts/demo.sh --help for the commands.
# Needs Docker Engine 28+, Compose 2.30+, bash (3.2 is fine) and curl.
# shellcheck source-path=SCRIPTDIR
set -Eeuo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=lib/common.sh
. "$ROOT/scripts/lib/common.sh"
# shellcheck source=lib/env.sh
. "$ROOT/scripts/lib/env.sh"
# shellcheck source=lib/doctor.sh
. "$ROOT/scripts/lib/doctor.sh"
# shellcheck source=lib/openshell.sh
. "$ROOT/scripts/lib/openshell.sh"

readonly COMPOSE_PROJECT=market-demo # the name in compose.yaml
readonly UP_TIMEOUT=3600 # the first data build downloads SEC EDGAR and embeds the corpus

usage() {
  cat <<'EOF'
Usage: ./scripts/demo.sh <command> [options]

Setup
  init                    create .env from .env.example and generate its internal secrets
  doctor [--keys]         check the host, .env and profiles; --keys also asks each endpoint
                          for its model list (authenticated, keys never printed)

Run
  up [--no-build]         build, prepare data, start the tools, Switchyard, Phoenix and the API,
                          then the OpenShell sandbox, the Hermes forwarder and the UI
  down [--volumes] [--prune]
                          delete the sandbox, then stop everything (--volumes: also delete data;
                          --prune: also remove this project's untagged images and Docker's unused
                          build cache, which is host-wide)
  restart SERVICE         agent: recreate the sandbox; switchyard: apply section 1 of .env
                          (routes, models, endpoints, keys) to Switchyard, the API, Auto Ontology
                          and retrieval; kumo: apply a new KUMO_RELATIONAL_URL or KUMO_API_KEY to
                          market analytics; anything else: docker compose restart SERVICE
  status                  services, the sandbox, Switchyard routes and URLs
  logs [agent|routing|SERVICE...] [-f]
                          agent: Hermes in the sandbox; routing: Switchyard's routing decisions
  check                   prove the sandbox boundary on the running stack

Data and recordings
  data fetch [DATASET...] [--verify-only]
                          fetch the pack's external datasets into DATA_SOURCE_DIR from each
                          DATA_SOURCE_<ID> in .env, and verify them file by file
  data prepare            build the active data pack (and the corpus and index with retrieval);
                          on a running stack, also restart the tools and, if needed, the sandbox
  data reindex            rebuild the retrieval index, e.g. after changing the embed model (with
                          analytics-gpu, also its CPU/GPU comparison)
  data generate [ARGS...]  write the synthetic-market text with NeMo Data Designer and Nemotron, with
                          uv on the host (ARGS go to demo-data-generate, e.g. --profile large)
  data validate|verify|list|clean [--all]
  record [ARGS...]        record sessions from the running stack into
                          data/packs/$DATA_PACK/recordings (ARGS go to `demo-api record`)
  replay                  serve the UI on the recorded sessions only: no .env, keys or GPU

Configuration comes from .env (see .env.example); a shell variable overrides it.
Profiles come from COMPOSE_PROFILES (default core,retrieval,analytics).
EOF
}

# up [--no-build]
cmd_up() {
  local build=true
  case ${1:-} in
    "") ;;
    --no-build) build=false ;;
    *) die "$EXIT_USAGE" "usage: demo.sh up [--no-build]" ;;
  esac
  load_env
  require_env
  doctor_for_up
  mkdir -p "$DATA_SOURCE_DIR" # before Compose, which would create it as root
  if has_profile ontology; then
    "$ROOT/tools/auto-ontology/prepare.sh"
  fi
  if $build; then
    log "building images (profiles $COMPOSE_PROFILES; agent features: ${AGENT_FEATURES:-none})"
    COMPOSE_PROFILES=$COMPOSE_PROFILES,build,tools dc build
  fi
  log "preparing data and starting the tools, Switchyard, Phoenix and the API"
  # shellcheck disable=SC2046 # one service per word
  dc up -d --wait --wait-timeout "$UP_TIMEOUT" $(backend_services)
  measure_retrieval_indexes
  openshell_up
  log "starting the UI"
  dc up -d --wait ui
  summary
}

# What the sandbox calls, by profile, plus the Auto Ontology web app the API signs in to for the
# data viewer's ontology. The data and migration one-shots run as their dependencies.
backend_services() {
  local services="switchyard phoenix api" profile
  for profile in ${COMPOSE_PROFILES//,/ }; do
    case $profile in
      retrieval) services="$services retrieval" ;;
      analytics) services="$services market-analytics" ;;
      analytics-gpu) services="$services market-analytics-gpu" ;;
      ontology) services="$services auto-ontology-mcp auto-ontology-frontend" ;;
    esac
  done
  echo "$services"
}

summary() {
  local capable="" judge=""
  if uses_capable_model; then
    capable="capable $AGENT_CAPABLE_MODEL on $(endpoint_host "$CAPABLE_BASE_URL"), "
  fi
  if template_models | grep -qx AGENT_JUDGE_MODEL; then
    judge="judge $AGENT_JUDGE_MODEL, "
  fi
  cat >&2 <<EOF

The demo is up.
  UI        http://127.0.0.1:$UI_PORT
  Phoenix   http://127.0.0.1:6006   (on a remote host, use an SSH tunnel)
  Models    $SWITCHYARD_ROUTES on $(endpoint_host "$INFERENCE_BASE_URL"): efficient $AGENT_EFFICIENT_MODEL,
            ${capable}${judge}aux $AGENT_AUX_MODEL
  Data      $DATA_PACK
Next: ./scripts/demo.sh check
EOF
}

endpoint_host() {
  local host=${1#*://}
  echo "${host%%/*}"
}

# down [--volumes] [--prune]: the sandbox first, then Compose. Never --remove-orphans: the project
# is shared.
cmd_down() {
  local volumes=false prune=false arg
  for arg in "$@"; do
    case $arg in
      --volumes) volumes=true ;;
      --prune) prune=true ;;
      *) die "$EXIT_USAGE" "usage: demo.sh down [--volumes] [--prune]" ;;
    esac
  done
  load_env
  openshell_down
  log "stopping the stack"
  if $volumes; then
    dc --profile '*' down --volumes
    # Compose removes only the active pack's Auto Ontology database; the other packs' go too.
    docker volume ls -q --filter "label=com.docker.compose.project=$COMPOSE_PROJECT" \
      --filter label=com.docker.compose.volume=auto-ontology-db | xargs -r docker volume rm
  else
    dc --profile '*' down
  fi
  if $prune; then
    prune_builds
  fi
}

# What rebuilds leave behind. With Docker's classic image store, each rebuild leaves the previous
# image untagged; only those Compose labelled with this project are removed. With the containerd
# image store the old image goes away, but its layers stay in BuildKit's build cache, which grows by
# about 15 GB per rebuild of the RAPIDS image. The build cache is host-wide, not per project, so this
# also clears other projects' unused cache (never their images, containers or volumes).
prune_builds() {
  log "removing the untagged images of earlier $COMPOSE_PROJECT builds"
  docker image prune --force --filter "label=com.docker.compose.project=$COMPOSE_PROJECT"
  log "removing Docker's unused build cache (host-wide); the next build starts cold"
  docker builder prune --force
  docker system df
}

# restart SERVICE
cmd_restart() {
  [ $# -eq 1 ] || die "$EXIT_USAGE" "usage: demo.sh restart agent|switchyard|kumo|SERVICE"
  load_env
  require_env
  case $1 in
    agent) openshell_up --recreate ;;
    switchyard) restart_inference ;;
    kumo) restart_kumo ;;
    *) dc restart "$1" ;;
  esac
}

# Apply section 1 of .env (the inference endpoint, its models and keys) to everything that reads it. The sandbox
# is kept. The API recreates only if its model ids changed, and Auto Ontology if its settings did; --no-deps
# leaves the one-shots alone. Compose never compares a secret's value, so the services holding the inference
# key as a secret are always recreated: Switchyard, and retrieval (its key defaults to the inference key).
# A new Kumo URL or key on a stack where Kumo stays on. Market analytics reads both once, at start, and Compose
# recreates it for neither a new key (a secret) nor a plain restart. Turning Kumo on or off also changes the
# agent image's tools, which takes `up`.
restart_kumo() {
  local service=market-analytics
  has_profile analytics-gpu && service=market-analytics-gpu
  check_kumo
  [ "$problems" -eq 0 ] || die "$EXIT_CONFIG" "fix the problems above, then run restart kumo again"
  kumo_enabled || die "$EXIT_CONFIG" "Kumo is off in .env: to turn it off on a running stack, run ./scripts/demo.sh up"
  log "recreating $service with the Kumo URL and key (if Kumo was off until now, run ./scripts/demo.sh up instead)"
  dc up -d --wait --no-deps --force-recreate "$service"
}

restart_inference() {
  dc up -d --wait --force-recreate switchyard
  dc up -d --wait --no-deps api
  if has_profile ontology; then
    dc up -d --wait --no-deps auto-ontology auto-ontology-ingestion
  fi
  if has_profile retrieval; then
    dc up -d --wait --no-deps --force-recreate retrieval
  fi
}

cmd_status() {
  [ $# -eq 0 ] || die "$EXIT_USAGE" "usage: demo.sh status"
  local routes endpoints
  load_env
  dc ps -a --format 'table {{.Service}}\t{{.State}}\t{{.Status}}'
  echo
  if gateway_ready; then
    endpoints='"sandbox: \(.phase)", (.endpoint_statuses[]
      | "  \(.host):\(.ports | map(tostring) | join(",")) \(.path) \(.last_result)")'
    # shellcheck disable=SC2016 # expanded by the container's shell
    cli_sh 'json=$(openshell sandbox get hermes -o json 2>/dev/null) && echo "$json" | jq -r "$1" ||
      echo "sandbox: none"' "$endpoints"
  else
    echo "sandbox: the OpenShell gateway is not running"
  fi
  if routes=$(curl -fsS --max-time 3 http://127.0.0.1:4000/v1/models 2>/dev/null); then
    echo "switchyard: $SWITCHYARD_ROUTES on $(endpoint_host "$INFERENCE_BASE_URL"), routes:" \
      "$(echo "$routes" | tr ',' '\n' | sed -n 's/.*"id": *"\([^"]*\)".*/\1/p' | tr '\n' ' ')"
  fi
  echo "data pack: $DATA_PACK; profiles: $COMPOSE_PROFILES; agent features: ${AGENT_FEATURES:-none}"
  echo "UI: http://127.0.0.1:$UI_PORT  Phoenix: http://127.0.0.1:6006"
}

# logs [agent|routing|SERVICE...] [-f]
cmd_logs() {
  load_env
  case ${1:-} in
    agent)
      shift
      if [ "${1:-}" = -f ]; then cli logs "$SANDBOX" --tail; else cli logs "$SANDBOX"; fi
      ;;
    routing)
      shift
      if [ "${1:-}" = -f ]; then
        dc exec switchyard tail -n 50 -f /var/lib/switchyard/routing.jsonl
      else
        dc exec switchyard tail -n 50 /var/lib/switchyard/routing.jsonl
      fi
      ;;
    *) dc logs "$@" ;;
  esac
}

# replay: the UI alone on the pack's recordings. Works without .env.
cmd_replay() {
  [ $# -eq 0 ] || die "$EXIT_USAGE" "usage: demo.sh replay"
  export COMPOSE_PROFILES=replay UI_MODE=replay PHOENIX_URL=
  load_env
  [ -f "$ROOT/data/packs/$DATA_PACK/recordings/index.json" ] ||
    die "$EXIT_CONFIG" "data pack $DATA_PACK has no recordings yet: record them on a running stack" \
      "(./scripts/demo.sh record), or replay another pack's, e.g. DATA_PACK=synthetic-market"
  dc up -d --build --wait ui
  log "replay: http://127.0.0.1:$UI_PORT (data pack $DATA_PACK)"
}

# record [ARGS...]: `demo-api record` against the running API, written to the pack's recordings.
cmd_record() {
  load_env
  require_env
  local out=data/packs/$DATA_PACK/recordings
  mkdir -p "$ROOT/$out"
  # As the host user, so the files are the user's; --no-deps leaves the running stack alone.
  dc run --rm --no-deps --user "$(id -u):$(id -g)" -v "$ROOT/$out:/out" --entrypoint demo-api api \
    record --out /out "$@"
  git -C "$ROOT" status --short -- "$out"
  log "review $out before committing: it holds questions, answers, evidence excerpts and model names"
}

# data fetch|prepare|reindex|validate|verify|list|clean [--all]
cmd_data() {
  local action=${1:-} service
  load_env
  mkdir -p "$DATA_SOURCE_DIR" # before Compose, which would create it as root
  case $action in
    # Build the data image first: a no-op when it is current, and after a code update never the old image.
    fetch | prepare | validate | verify | list | clean) COMPOSE_PROFILES=core dc build --quiet data ;;
  esac
  case $action in
    fetch)
      shift
      data_fetch "$@"
      ;;
    prepare)
      dc run --rm --no-deps data prepare --structured
      if has_profile retrieval; then
        dc run --rm --no-deps data-corpus
        reindex
      fi
      # Market analytics and retrieval keep the build they resolved at startup; restart them on the new one
      # (`up --wait` waits until they are healthy again).
      for service in market-analytics market-analytics-gpu retrieval; do
        if [ -n "$(dc ps -q --status running "$service" 2>/dev/null)" ]; then
          dc restart "$service" && dc up -d --wait --no-deps "$service"
        fi
      done
      # Hermes lists the tools once, when the sandbox starts: recreate the sandbox if the build changed them.
      if gateway_ready && [ -n "$(sandbox_state)" ]; then
        require_env
        openshell_up
      fi
      ;;
    reindex) reindex ;;
    generate)
      shift
      data_generate "$@"
      ;;
    validate | verify | list | clean)
      shift
      dc run --rm --no-deps data "$action" "$@"
      ;;
    *) die "$EXIT_USAGE" "usage: demo.sh data fetch|prepare|reindex|generate|validate|verify|list|clean [--all]" ;;
  esac
}

# data fetch [DATASET...] [--verify-only]: the pack's external datasets into DATA_SOURCE_DIR/<id>, each verified
# file by file against the manifest the pack pins (docs/data-platform.md). DATA_SOURCE_<ID> in .env says where
# dataset <id> comes from:
#   a URL (https, s3, gs, hf)      fetched in the data image, which gets only that scheme's credentials
#   a directory or host:/path      copied with rsync here on the host, which has the files and the SSH keys
#   nothing                        what is already in place is verified
data_fetch() {
  local arg id variable source verify_only=false datasets=() options
  for arg in "$@"; do
    case $arg in
      --verify-only) verify_only=true ;;
      -*) die "$EXIT_USAGE" "usage: demo.sh data fetch [DATASET...] [--verify-only]" ;;
      *) datasets+=("$arg") ;;
    esac
  done
  if [ ${#datasets[@]} -eq 0 ]; then
    # shellcheck disable=SC2207 # dataset ids have no spaces
    datasets=($(pack_external | cut -d' ' -f1))
  fi
  if [ ${#datasets[@]} -eq 0 ]; then
    log "data pack $DATA_PACK has no external datasets"
    return 0
  fi
  for id in "${datasets[@]}"; do
    variable=DATA_SOURCE_$(printf '%s' "$id" | tr 'a-z-' 'A-Z_')
    source=$(env_value "$variable")
    source=${source#file://}
    # As the host user, so the files are the user's; HOME for the libraries' caches.
    options=(--rm --no-deps --user "$(id -u):$(id -g)" -e HOME=/tmp)
    if $verify_only || [ -z "$source" ]; then
      log "$id: verifying $DATA_SOURCE_DIR/$id"
      dc run "${options[@]}" data-fetch fetch "$id" --verify-only
    elif [[ $source == *://* ]]; then
      log "$id: fetching from ${source%%\?*}" # a pre-signed URL's query is a credential
      export "$variable=$source"
      options+=(-e "$variable")
      fetch_credentials "$source"
      dc run "${options[@]}" data-fetch fetch "$id"
    else
      log "$id: copying $source into $DATA_SOURCE_DIR/$id with rsync"
      mkdir -p "$DATA_SOURCE_DIR/$id"
      rsync -a --partial --exclude '.*' "${source%/}/" "$DATA_SOURCE_DIR/$id/"
      dc run "${options[@]}" data-fetch fetch "$id" --verify-only
    fi
    # The services read it as their own users, whatever modes it arrived with (rsync -a keeps the source's).
    chmod -R a+rX "$DATA_SOURCE_DIR/$id"
  done
}

# fetch_credentials URL: add to the caller's `options` the credentials URL's scheme reads, for those .env sets.
# Values are exported and passed as -e NAME, so they never appear on a command line.
fetch_credentials() {
  local names="" name value
  case $1 in
    http://* | https://*) names=DATA_SOURCE_HTTP_TOKEN ;;
    s3://*) names="AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_REGION AWS_ENDPOINT_URL" ;;
    hf://*) names=HF_TOKEN ;;
    gs://* | gcs://*)
      value=$(env_value GOOGLE_APPLICATION_CREDENTIALS) # a host path to a service-account file
      if [ -n "$value" ]; then
        options+=(-v "$value:/run/gcs.json:ro" -e GOOGLE_APPLICATION_CREDENTIALS=/run/gcs.json)
      fi
      ;;
  esac
  for name in $names; do
    value=$(env_value "$name")
    if [ -n "$value" ]; then
      export "$name=$value"
      options+=(-e "$name")
    fi
  done
}

# data generate [ARGS...]: the synthetic-market pack's text (data/generate/README.md). The keys are passed in
# the environment of this one command only. An empty DATA_DESIGNER_API_KEY falls back to INFERENCE_API_KEY only
# when DATA_DESIGNER_BASE_URL is INFERENCE_BASE_URL, or for an nvapi- key on build.nvidia.com; demo-data-generate
# decides, and stops otherwise. Paths in ARGS are relative to the current directory.
data_generate() {
  command -v uv >/dev/null || die "$EXIT_CONFIG" "data generate runs with uv on the host: install uv"
  DATA_DESIGNER_API_KEY=$(env_value DATA_DESIGNER_API_KEY) \
    INFERENCE_API_KEY=$INFERENCE_API_KEY INFERENCE_BASE_URL=$INFERENCE_BASE_URL \
    DATA_DESIGNER_BASE_URL=$(env_value DATA_DESIGNER_BASE_URL) \
    DATA_DESIGNER_MODEL=$(env_value DATA_DESIGNER_MODEL) \
    DATA_DESIGNER_PARALLEL=$(env_value DATA_DESIGNER_PARALLEL) \
    SEC_USER_AGENT=$SEC_USER_AGENT \
    uv run --project "$ROOT/data/generate" --locked demo-data-generate "$@"
}

reindex() {
  dc build --quiet retrieval # retrieval-index's image, as the data image above
  dc up -d --wait milvus
  dc run --rm --no-deps retrieval-index
  measure_retrieval_indexes
}

# analytics-gpu with retrieval: the Benchmark tab's Milvus row, the build's CPU index against its copy in the GPU
# Milvus, measured once per build. Answers search the CPU index only, so a GPU Milvus that does not start (its GPU
# memory taken, say) leaves the comparison out with a warning, and is stopped so it holds no GPU memory.
measure_retrieval_indexes() {
  has_profile retrieval && has_profile analytics-gpu || return 0
  log "measuring the Milvus CPU index against its GPU copy (the Benchmark tab), once per build"
  if ! retrieval_benchmark; then
    warn "no Milvus CPU/GPU comparison for this build; answers are unaffected" \
      "(./scripts/demo.sh logs retrieval-benchmark milvus-gpu)"
    dc stop milvus-gpu >/dev/null 2>&1 || true
  fi
}

# The retrieval-benchmark one-shot, once the GPU Milvus is healthy.
retrieval_benchmark() {
  dc up -d --wait milvus-gpu && dc run --rm --no-deps retrieval-benchmark
}

main() {
  local command=${1:-}
  case $command in
    "" | -h | --help | help) usage ;;
    init | doctor | up | down | restart | status | logs | replay | record | data | check)
      shift
      "cmd_$command" "$@"
      ;;
    *)
      usage >&2
      exit "$EXIT_USAGE"
      ;;
  esac
}

main "$@"
