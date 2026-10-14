# shellcheck shell=bash
# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# doctor: host, configuration and profile checks. Messages name variables, never their values.

readonly KNOWN_PROFILES="core retrieval analytics analytics-gpu ontology"
readonly BUILD_NVIDIA_HOST=integrate.api.nvidia.com

problems=0
problem() {
  printf 'problem: %s\n' "$*" >&2
  problems=$((problems + 1))
}

# doctor [--keys]
cmd_doctor() {
  local keys=false
  case ${1:-} in
    "") ;;
    --keys) keys=true ;;
    *) die "$EXIT_USAGE" "usage: demo.sh doctor [--keys]" ;;
  esac
  load_env
  check_host
  check_ports
  if [ -f "$ENV_FILE" ]; then
    check_config
    if $keys; then
      check_keys
    fi
  else
    problem "no .env yet: run ./scripts/demo.sh init"
  fi
  [ "$problems" -eq 0 ] || die "$EXIT_CONFIG" "$problems problem(s) found"
  log "doctor: no problems found"
}

# The checks `up` runs first: everything but ports and keys.
doctor_for_up() {
  check_host
  check_config
  [ "$problems" -eq 0 ] || die "$EXIT_CONFIG" "fix the problems above, then run up again"
}

# major.minor of a version string, as a number: 2.30.1 -> 2030, 6.8.0-85-generic -> 6008
version_number() {
  local major minor
  IFS=. read -r major minor _ <<<"${1#v}"
  major=${major%%[!0-9]*} minor=${minor%%[!0-9]*}
  echo $((${major:-0} * 1000 + ${minor:-0}))
}

check_host() {
  docker info >/dev/null 2>&1 || die "$EXIT_UNAVAILABLE" "Docker is not reachable"
  command -v curl >/dev/null || problem "curl is not installed"
  local engine compose kernel
  engine=$(docker version --format '{{.Server.Version}}')
  [ "$(version_number "$engine")" -ge 28000 ] || problem "Docker Engine $engine: 28 or later is required"
  compose=$(docker compose version --short)
  [ "$(version_number "$compose")" -ge 2030 ] || problem "Docker Compose $compose: 2.30 or later is required"
  # OpenShell's sandbox needs Landlock ABI 3 (Linux 6.2+) on the Docker host; it fails closed.
  kernel=$(docker info --format '{{.KernelVersion}}')
  [ "$(version_number "$kernel")" -ge 6002 ] || problem "Docker host kernel $kernel: OpenShell needs Linux 6.2 or later"
  if has_profile analytics-gpu; then
    docker info --format '{{json .Runtimes}}' | grep -q nvidia ||
      problem "the analytics-gpu profile needs the NVIDIA Container Toolkit"
  fi
  if has_profile retrieval && [ "$(docker info --format '{{.MemTotal}}')" -lt $((8 * 1024 * 1024 * 1024)) ]; then
    warn "Docker has less than 8 GiB of memory; Milvus needs at least 8 GiB"
  fi
}

# Host ports the active profiles publish on 127.0.0.1 must be free (skipped while the stack runs).
check_ports() {
  if [ -n "$(dc ps -q 2>/dev/null)" ]; then
    return 0
  fi
  local ports="$UI_PORT 4000 6006 8000 18080 18081" port
  if has_profile retrieval; then
    ports="$ports 8120"
  fi
  if has_profile analytics || has_profile analytics-gpu; then
    ports="$ports 3010"
  fi
  if has_profile ontology; then
    ports="$ports 3003"
  fi
  for port in $ports; do
    if (: </dev/tcp/127.0.0.1/"$port") 2>/dev/null; then
      problem "127.0.0.1:$port is already in use"
    fi
  done
}

check_config() {
  [ -n "$(find "$ENV_FILE" -perm 600)" ] || warn ".env holds keys; restrict it with: chmod 600 .env"
  # The UI has no sign-in and proxies /api/v1 to the agent: only a link that signs users in may reach it.
  [ "$UI_BIND_HOST" = 127.0.0.1 ] ||
    warn "UI_BIND_HOST is not 127.0.0.1: the UI, and the agent behind it, is open to every network that reaches port $UI_PORT; keep it only behind a link that requires sign-in, such as a Brev link with sign-in set in the Brev console"
  check_profiles
  check_kumo
  check_inference
  if has_profile retrieval || has_profile ontology; then
    check_retriever
  fi
  check_speech
  # The default corpora (DATA_CORPORA empty) include the SEC EDGAR filings (sec_filings).
  case ,${DATA_CORPORA:-sec_filings}, in
    *,sec_filings,*)
      if has_profile retrieval && [ -z "$SEC_USER_AGENT" ]; then
        problem "SEC_USER_AGENT is empty: SEC EDGAR needs it for the filings corpus (or leave it out of DATA_CORPORA)"
      fi
      ;;
  esac
  check_external_data
  # Hermes refuses an API server key under 16 characters; init generates 64.
  local key value
  for key in $GENERATED_SECRETS; do
    value=${!key}
    [ "${#value}" -ge 16 ] || problem "$key must be at least 16 characters: run ./scripts/demo.sh init"
  done
  [ "$HERMES_API_SERVER_KEY" != "$HERMES_RECEIPT_API_KEY" ] ||
    problem "HERMES_API_SERVER_KEY and HERMES_RECEIPT_API_KEY must differ"
  if has_profile ontology; then
    for key in AUTO_ONTOLOGY_REASONING_MODEL AUTO_ONTOLOGY_NON_REASONING_MODEL; do
      [ -n "${!key}" ] || problem "$key is empty (.env section 1)"
    done
  fi
}

# A pack with external data (docs/data-platform.md) needs it fetched, room for it, and SEC company data.
check_external_data() {
  local pack=$ROOT/data/packs/$DATA_PACK/pack.yaml id bytes needed=0 free dir=$DATA_SOURCE_DIR
  [ -f "$pack" ] || {
    problem "DATA_PACK=$DATA_PACK has no data/packs/$DATA_PACK/pack.yaml"
    return 0
  }
  if grep -q '^  companies: sec' "$pack" && [ -z "$SEC_USER_AGENT" ]; then
    problem "SEC_USER_AGENT is empty: $DATA_PACK looks up its companies at SEC"
  fi
  while read -r id bytes; do
    if [ ! -f "$DATA_SOURCE_DIR/$id/.verified.json" ]; then
      problem "external dataset $id is not in $DATA_SOURCE_DIR yet: set" \
        "DATA_SOURCE_$(printf '%s' "$id" | tr 'a-z-' 'A-Z_') in .env and run ./scripts/demo.sh data fetch"
      needed=$((needed + bytes))
    fi
  done < <(pack_external)
  if [ "$needed" -gt 0 ]; then
    until [ -d "$dir" ]; do dir=$(dirname "$dir"); done
    free=$(df -Pk "$dir" | awk 'NR == 2 { print $4 }') # KiB
    [ "$free" -gt $(((needed + needed / 10) / 1024)) ] ||
      problem "$DATA_SOURCE_DIR has $((free / 1048576)) GiB free; the external data needs" \
        "$((needed / 1073741824 + 1)) GiB plus 10%"
  fi
}

check_profiles() {
  local profile
  for profile in ${COMPOSE_PROFILES//,/ }; do
    case " $KNOWN_PROFILES " in
      *" $profile "*) ;;
      *)
        if [ "$profile" = kumo ]; then
          problem "the kumo profile is gone: remove it from COMPOSE_PROFILES, and set KUMO_RELATIONAL_URL and" \
            "KUMO_API_KEY to a Kumo service (.env section 3; docs/kumo-service.md)"
        else
          problem "unknown profile '$profile' in COMPOSE_PROFILES (known: $KNOWN_PROFILES)"
        fi
        ;;
    esac
  done
  if has_profile analytics && has_profile analytics-gpu; then
    problem "use analytics or analytics-gpu, not both (they share a port and a DNS name)"
  fi
  if has_profile ontology && [ ! -e "$ROOT/vendor/auto-ontology/.git" ]; then
    problem "the ontology profile needs the Auto Ontology submodule:" \
      "git submodule update --init --checkout vendor/auto-ontology"
  fi
}

# Kumo prediction (predict_asset_outcomes) is configured only by the URL and key of a Kumo service
# (docs/kumo-service.md): both turn it on, neither leaves it off, and one without the other is a problem.
check_kumo() {
  if [ -z "$KUMO_RELATIONAL_URL" ] && [ -z "$KUMO_API_KEY" ]; then
    log "Kumo prediction is off: to turn it on, set KUMO_RELATIONAL_URL and KUMO_API_KEY to a Kumo service" \
      "(.env section 3; docs/kumo-service.md)"
    return 0
  fi
  if ! kumo_enabled; then
    local empty=KUMO_API_KEY
    [ -n "$KUMO_RELATIONAL_URL" ] || empty=KUMO_RELATIONAL_URL
    problem "$empty is empty: set KUMO_RELATIONAL_URL and KUMO_API_KEY together, or neither (.env section 3;" \
      "docs/kumo-service.md)"
    return 0
  fi
  # The Kumo client refuses to send a key over plain HTTP; the service is reached through an HTTPS link.
  case $KUMO_RELATIONAL_URL in
    https://?*) ;;
    *) problem "KUMO_RELATIONAL_URL must be an https:// URL: the Kumo client never sends KUMO_API_KEY over plain HTTP" ;;
  esac
  if ! has_profile analytics && ! has_profile analytics-gpu; then
    problem "Kumo (KUMO_RELATIONAL_URL and KUMO_API_KEY) needs the analytics or analytics-gpu profile"
  fi
}

check_inference() {
  local key models=""
  for key in INFERENCE_BASE_URL INFERENCE_API_KEY SWITCHYARD_ROUTES; do
    [ -n "${!key}" ] || problem "$key is empty (.env section 1)"
  done
  [ -z "$SWITCHYARD_ROUTES" ] || [ -f "$(route_template)" ] ||
    problem "SWITCHYARD_ROUTES=$SWITCHYARD_ROUTES has no template in infra/switchyard/routes/"
  case ${SWITCHYARD_CONFIRMATIONS:-1} in
    1 | 2) ;;
    *) problem "SWITCHYARD_CONFIRMATIONS must be 1 or 2" ;;
  esac
  # Only the models the template uses are required. As in Switchyard's entrypoint, the efficient,
  # capable and judge models must all differ, and the aux model must differ from the efficient one.
  for key in $(template_models); do
    [ -n "${!key}" ] || problem "$key is empty (.env section 1; $SWITCHYARD_ROUTES uses it)"
    [ "$key" = AGENT_AUX_MODEL ] || models="$models ${!key}"
  done
  if [ -n "$(echo "$models" | tr ' ' '\n' | sed '/^$/d' | sort | uniq -d)" ]; then
    problem "$SWITCHYARD_ROUTES needs different models in:" \
      "$(template_models | grep -vx AGENT_AUX_MODEL | tr '\n' ' ' | sed 's/ $//')"
  fi
  if template_models | grep -qx AGENT_AUX_MODEL && [ -n "$AGENT_AUX_MODEL" ] &&
    [ "$AGENT_AUX_MODEL" = "$AGENT_EFFICIENT_MODEL" ]; then
    problem "AGENT_AUX_MODEL must differ from AGENT_EFFICIENT_MODEL"
  fi
  # build.nvidia.com takes nvapi- keys and publisher/model ids, and serves no GPT-6 Sol.
  if on_build_nvidia "$INFERENCE_BASE_URL"; then
    case $INFERENCE_API_KEY in
      nvapi-*) ;;
      *) problem "INFERENCE_API_KEY must be an nvapi- key for build.nvidia.com" ;;
    esac
    for key in $(template_models); do
      on_capable_endpoint "$key" && ! on_build_nvidia "$CAPABLE_BASE_URL" && continue
      case ${!key} in
        */*/* | /* | */) problem "$key is not a build.nvidia.com model id (publisher/model)" ;;
        */*) ;;
        *) problem "$key is not a build.nvidia.com model id (publisher/model)" ;;
      esac
    done
  fi
  # The inference key goes to the inference endpoint only (env.sh, Switchyard's entrypoint).
  if uses_capable_model && [ -z "$CAPABLE_API_KEY" ] && [ "$CAPABLE_BASE_URL" != "$INFERENCE_BASE_URL" ]; then
    problem "CAPABLE_BASE_URL is another endpoint than INFERENCE_BASE_URL: set CAPABLE_API_KEY to its key" \
      "(.env section 1)"
  fi
  if on_build_nvidia "$CAPABLE_BASE_URL" && uses_capable_model; then
    case $CAPABLE_API_KEY in
      "" | nvapi-*) ;;
      *) problem "CAPABLE_API_KEY (or INFERENCE_API_KEY) must be an nvapi- key for build.nvidia.com" ;;
    esac
    case $SWITCHYARD_ROUTES in
      *-gpt) problem "SWITCHYARD_ROUTES=$SWITCHYARD_ROUTES needs a GPT model: set CAPABLE_BASE_URL and" \
        "CAPABLE_API_KEY to an OpenAI-compatible endpoint that serves it (build.nvidia.com does not)" ;;
      *-claude) problem "SWITCHYARD_ROUTES=$SWITCHYARD_ROUTES needs a Claude model: set CAPABLE_BASE_URL and" \
        "CAPABLE_API_KEY to an endpoint that serves it over the Anthropic Messages API (build.nvidia.com does not)" ;;
    esac
  fi
}

route_template() {
  echo "$ROOT/infra/switchyard/routes/$SWITCHYARD_ROUTES.toml.tmpl"
}

# The AGENT_*_MODEL settings the selected template substitutes (its "# requires:" header).
template_models() {
  [ -n "$SWITCHYARD_ROUTES" ] && [ -f "$(route_template)" ] || return 0
  sed -n 's/^# requires://p' "$(route_template)" | tr ' ' '\n' | grep -x 'AGENT_[A-Z]*_MODEL'
}

uses_capable_model() {
  template_models | grep -qx AGENT_CAPABLE_MODEL
}

# The AGENT_*_MODEL settings whose target's llm client reads CAPABLE_BASE_URL: the capable model, and
# the judge in the frontier escalation templates.
capable_endpoint_models() {
  [ -n "$SWITCHYARD_ROUTES" ] && [ -f "$(route_template)" ] || return 0
  awk '/^\[llm_clients\./ { kind = "client"; name = $1; gsub(/^\[llm_clients\.|\].*$/, "", name); next }
    /^\[targets\./ { kind = "target"; name = $1; gsub(/^\[targets\.|\].*$/, "", name); next }
    /^\[/ { kind = ""; next }
    kind == "client" && /^base_url *= *"\$\{CAPABLE_BASE_URL\}"/ { capable[name] = 1 }
    kind == "target" && /^id *= *"\$\{AGENT_[A-Z]*_MODEL\}"/ { model = $0; gsub(/^[^{]*\{|\}.*$/, "", model); id[name] = model }
    kind == "target" && /^llm_client *= / { client = $0; gsub(/^[^"]*"|".*$/, "", client); uses[name] = client }
    END { for (t in id) if (uses[t] in capable) print id[t] }' "$(route_template)" | sort -u
}

on_capable_endpoint() {
  capable_endpoint_models | grep -qx "$1"
}

on_build_nvidia() {
  case $1 in
    *"$BUILD_NVIDIA_HOST"*) return 0 ;;
    *) return 1 ;;
  esac
}

check_retriever() {
  [ -n "$RETRIEVER_API_KEY" ] || problem "RETRIEVER_API_KEY and INFERENCE_API_KEY are empty (.env sections 1 and 2)"
  if on_build_nvidia "${RETRIEVER_BASE_URL:-$BUILD_NVIDIA_HOST}"; then
    case $RETRIEVER_API_KEY in
      "" | nvapi-*) ;;
      *) problem "RETRIEVER_API_KEY (or INFERENCE_API_KEY) must be an nvapi- key for build.nvidia.com" ;;
    esac
  fi
}

# Voice input (.env section 6) needs an nvapi- key for Nemotron ASR on build.nvidia.com.
check_speech() {
  case $SPEECH_INPUT_ENABLED in
    [Tt]rue | 1 | [Yy]es | [Oo]n) ;;
    *) return 0 ;;
  esac
  case $SPEECH_API_KEY in
    nvapi-*) ;;
    "") problem "SPEECH_INPUT_ENABLED is on but SPEECH_API_KEY is empty, and the retriever is not build.nvidia.com" ;;
    *) problem "SPEECH_API_KEY must be an nvapi- key for build.nvidia.com" ;;
  esac
}

# Ask each endpoint for its model list with the configured key and look for every configured id
# the template uses. A listed id can still be refused: some gateways list models outside a key's
# access group. The hosted rerankers are not listed at all; the first ingest checks the reranker.
check_keys() {
  local models="" capable_models="" key
  for key in $(template_models); do
    if on_capable_endpoint "$key"; then
      capable_models="$capable_models ${!key}"
    else
      models="$models ${!key}"
    fi
  done
  if has_profile ontology; then
    models="$models $AUTO_ONTOLOGY_REASONING_MODEL $AUTO_ONTOLOGY_NON_REASONING_MODEL"
  fi
  # shellcheck disable=SC2086 # model ids have no spaces
  models_listed inference "$INFERENCE_BASE_URL" "$INFERENCE_API_KEY" $models
  if uses_capable_model; then
    # shellcheck disable=SC2086 # model ids have no spaces
    case $SWITCHYARD_ROUTES in
      # The Anthropic Messages API lists models for an x-api-key, as Switchyard authenticates there.
      *-claude) MODELS_AUTH=anthropic models_listed capable "$CAPABLE_BASE_URL" "$CAPABLE_API_KEY" $capable_models ;;
      *) models_listed capable "$CAPABLE_BASE_URL" "$CAPABLE_API_KEY" $capable_models ;;
    esac
  fi
  if has_profile retrieval || has_profile ontology; then
    models_listed retriever "${RETRIEVER_BASE_URL:-https://$BUILD_NVIDIA_HOST/v1}" "$RETRIEVER_API_KEY" \
      "${RETRIEVER_EMBED_MODEL:-nvidia/nemotron-3-embed-1b}"
  fi
  # The Kumo service's proxy checks the key on every route but /healthz, the NIM's /v1/models included.
  if kumo_enabled; then
    MODELS_AUTH=kumo models_listed kumo "${KUMO_RELATIONAL_URL%/}/v1" "$KUMO_API_KEY" kumo-relational
  fi
}

# [MODELS_AUTH=anthropic|kumo] models_listed NAME BASE_URL KEY MODEL...
models_listed() {
  local name=$1 url=$2 key=$3 listed model header='header = "Authorization: Bearer %s"\n'
  shift 3
  [ -n "$url" ] && [ -n "$key" ] || return 0 # already reported as empty
  case ${MODELS_AUTH:-} in
    anthropic) header='header = "x-api-key: %s"\nheader = "anthropic-version: 2023-06-01"\n' ;;
    kumo) header='header = "X-API-Key: %s"\n' ;;
  esac
  # curl reads the header from stdin, so the key never appears in a process listing.
  # shellcheck disable=SC2059 # the format is one of the two constant strings above
  if ! listed=$(printf "$header" "$key" |
    curl -fsS --max-time 30 --config - "$url/models" | tr -d ' \t\n'); then
    problem "$name: GET $url/models failed with the configured key"
    return 0
  fi
  for model in "$@"; do
    case $listed in
      *"\"id\":\"$model\""*) log "$name: $model is listed" ;;
      *) problem "$name: $model is not listed by $url/models" ;;
    esac
  done
}
