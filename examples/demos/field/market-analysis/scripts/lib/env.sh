# shellcheck shell=bash
# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# The configuration demo.sh works from: .env exactly as Compose reads it, plus derived values.
# .env is never sourced: bash and Compose parse it differently.

readonly DEFAULT_PROFILES=core,retrieval,analytics
readonly GENERATED_SECRETS="HERMES_API_SERVER_KEY HERMES_RECEIPT_API_KEY
  AUTO_ONTOLOGY_ADMIN_PASSWORD AUTO_ONTOLOGY_AUTH_SECRET"

# Every variable demo.sh reads. A shell value wins over .env, as in Compose.
readonly ENV_KEYS="COMPOSE_PROFILES UI_PORT UI_BIND_HOST DATA_PACK DATA_PACK_PROFILE DATA_CORPORA SEC_USER_AGENT
  DATA_SOURCE_DIR INFERENCE_BASE_URL INFERENCE_API_KEY CAPABLE_BASE_URL CAPABLE_API_KEY
  SWITCHYARD_ROUTES SWITCHYARD_CONFIRMATIONS
  AGENT_EFFICIENT_MODEL AGENT_CAPABLE_MODEL AGENT_JUDGE_MODEL AGENT_AUX_MODEL
  AUTO_ONTOLOGY_REASONING_MODEL AUTO_ONTOLOGY_NON_REASONING_MODEL
  RETRIEVER_BASE_URL RETRIEVER_API_KEY RETRIEVER_EMBED_MODEL KUMO_RELATIONAL_URL KUMO_API_KEY
  SPEECH_INPUT_ENABLED SPEECH_API_KEY
  $GENERATED_SECRETS OPENSHELL_SUPERVISOR_IMAGE OPENSHELL_SANDBOX_IMAGE"

# Read ENV_KEYS from Compose's own view of the environment (shell, versions.env, .env), then
# derive and export what Compose cannot compute itself:
#   COMPOSE_PROFILES    defaults to core,retrieval,analytics
#   DATA_DATABASE_NAME  the pack id in snake case (Compose has no ${VAR//-/_})
#   AUTO_ONTOLOGY_URL   the Auto Ontology web app under the ontology profile, for the API
#   AGENT_FEATURES      the optional tools baked into the agent image
#   KUMO_API_KEY        always exported, if empty: Compose needs every secret's variable set
#   RETRIEVER_API_KEY   INFERENCE_API_KEY when empty: one build.nvidia.com key serves both
#   SPEECH_API_KEY      RETRIEVER_API_KEY when empty and the retriever is build.nvidia.com: the
#                       ASR is on build.nvidia.com too, so no key goes to another host
#   DATA_SOURCE_DIR     $HOME/market-demo-data when empty: external datasets, outside the repository
#   CAPABLE_API_KEY     INFERENCE_API_KEY when empty and CAPABLE_BASE_URL is INFERENCE_BASE_URL (or empty),
#                       never for another host; always exported, if empty, because Compose needs every
#                       secret's variable set and .env.example leaves it commented out
# CAPABLE_BASE_URL also falls back to INFERENCE_BASE_URL, here for doctor and in Switchyard's
# entrypoint for the stack.
load_env() {
  local key
  COMPOSE_ENVIRONMENT=$(dc config --environment) ||
    die "$EXIT_CONFIG" "docker compose cannot read the configuration"
  for key in $ENV_KEYS; do
    printf -v "$key" '%s' "$(env_value "$key")"
  done
  COMPOSE_PROFILES=${COMPOSE_PROFILES:-$DEFAULT_PROFILES}
  UI_PORT=${UI_PORT:-3100}
  UI_BIND_HOST=${UI_BIND_HOST:-127.0.0.1}
  DATA_PACK=${DATA_PACK:-synthetic-market}
  DATA_DATABASE_NAME=${DATA_PACK//-/_}
  DATA_SOURCE_DIR=${DATA_SOURCE_DIR:-$HOME/market-demo-data}
  AUTO_ONTOLOGY_URL=
  if has_profile ontology; then
    AUTO_ONTOLOGY_URL=http://auto-ontology-frontend:3000
  fi
  AGENT_FEATURES=$(agent_features)
  RETRIEVER_API_KEY=${RETRIEVER_API_KEY:-$INFERENCE_API_KEY}
  if [ -z "$SPEECH_API_KEY" ] && [ "${RETRIEVER_BASE_URL:-https://$BUILD_NVIDIA_HOST/v1}" = "https://$BUILD_NVIDIA_HOST/v1" ]; then
    SPEECH_API_KEY=$RETRIEVER_API_KEY
  fi
  CAPABLE_BASE_URL=${CAPABLE_BASE_URL:-$INFERENCE_BASE_URL}
  if [ "$CAPABLE_BASE_URL" = "$INFERENCE_BASE_URL" ]; then
    CAPABLE_API_KEY=${CAPABLE_API_KEY:-$INFERENCE_API_KEY}
  fi
  export COMPOSE_PROFILES DATA_DATABASE_NAME DATA_SOURCE_DIR AUTO_ONTOLOGY_URL AGENT_FEATURES
  export RETRIEVER_API_KEY SPEECH_API_KEY CAPABLE_API_KEY KUMO_API_KEY
}

# env_value NAME: NAME as Compose sees it (shell, then .env), after load_env; empty when unset.
env_value() {
  printf '%s\n' "$COMPOSE_ENVIRONMENT" | sed -n "s/^$1=//p"
}

# The data pack's external datasets, one "id bytes" line each, from the `external:` section of its pack.yaml.
pack_external() {
  local pack=$ROOT/data/packs/$DATA_PACK/pack.yaml
  [ -f "$pack" ] || return 0
  awk '/^external:/ { on = 1; next }
    /^[^ #]/ { on = 0 }
    on && /^  [a-z0-9-]+:/ { id = $1; sub(/:$/, "", id) }
    on && /^    bytes:/ { print id, $2 }' "$pack"
}

# Commands that run the live stack need .env, and always include the core profile.
require_env() {
  [ -f "$ENV_FILE" ] || die "$EXIT_CONFIG" "no .env yet: run ./scripts/demo.sh init"
  if ! has_profile core; then
    COMPOSE_PROFILES=core,$COMPOSE_PROFILES
  fi
}

has_profile() {
  case ",$COMPOSE_PROFILES," in
    *",$1,"*) return 0 ;;
    *) return 1 ;;
  esac
}

# The agent image's features (agent/README.md): retrieval, analytics (also for analytics-gpu),
# kumo (KUMO_RELATIONAL_URL and KUMO_API_KEY both set: the Kumo service, docs/kumo-service.md) and
# ontology, in that order.
agent_features() {
  local features=""
  if has_profile retrieval; then
    features=retrieval
  fi
  if has_profile analytics || has_profile analytics-gpu; then
    features=${features:+$features,}analytics
  fi
  if kumo_enabled; then
    features=${features:+$features,}kumo
  fi
  if has_profile ontology; then
    features=${features:+$features,}ontology
  fi
  echo "$features"
}

# Kumo prediction is on when the service's URL and key are both set; doctor rejects one without the other.
kumo_enabled() {
  [ -n "$KUMO_RELATIONAL_URL" ] && [ -n "$KUMO_API_KEY" ]
}

# init: create .env from .env.example (mode 600, never overwritten) and fill every empty
# generated secret with 64 random hex characters.
cmd_init() {
  [ $# -eq 0 ] || die "$EXIT_USAGE" "usage: demo.sh init"
  if [ -f "$ENV_FILE" ]; then
    log ".env exists; filling only its empty generated secrets"
  else
    (umask 077 && cp "$ROOT/.env.example" "$ENV_FILE")
    log "created .env (mode 600)"
  fi
  local key script="" tmp
  for key in $GENERATED_SECRETS; do
    script="$script s/^$key=\$/$key=$(random_hex)/;"
  done
  tmp=$(mktemp "$ENV_FILE.XXXXXX") # mode 600, next to .env
  sed "$script" "$ENV_FILE" >"$tmp" && mv "$tmp" "$ENV_FILE"
  cat >&2 <<'EOF'
Now edit .env:
  INFERENCE_API_KEY   the key for INFERENCE_BASE_URL (section 1): an nvapi- key for build.nvidia.com
  RETRIEVER_API_KEY   only for a different retriever key (section 2); empty uses INFERENCE_API_KEY
  COMPOSE_PROFILES    what runs (section 3)
  SEC_USER_AGENT      your name and email, for the SEC EDGAR corpus (section 4)
Then run: ./scripts/demo.sh doctor --keys && ./scripts/demo.sh up
EOF
}
