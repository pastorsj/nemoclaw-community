#!/bin/sh
# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Render routes/$SWITCHYARD_ROUTES.toml.tmpl from the environment, validate it with --dry-run and
# serve it. Configuration mistakes exit 64 (EX_USAGE) before anything listens. Extra arguments
# are passed to switchyard-server.
#
# The capable model may use its own OpenAI-compatible endpoint: CAPABLE_BASE_URL defaults to
# INFERENCE_BASE_URL. CAPABLE_API_KEY defaults to INFERENCE_API_KEY only on that same endpoint, so
# the inference key never goes to another host.
set -eu

here=$(dirname "$0")
key_file=${INFERENCE_API_KEY_FILE:-/run/secrets/inference_api_key}
capable_key_file=${CAPABLE_API_KEY_FILE:-/run/secrets/capable_api_key}
state_dir=${SWITCHYARD_STATE_DIR:-/var/lib/switchyard}
config=${TMPDIR:-/tmp}/routes.toml

fail() {
  echo "switchyard: $*" >&2
  exit 64
}

require() {
  for name in "$@"; do
    [ -n "$(printenv "$name")" ] || fail "$name is empty; set it in .env (see .env.example)"
  done
}

# Compose delivers the keys as secret files, which keeps them out of `docker inspect`. Compose
# writes an empty capable_api_key file when CAPABLE_API_KEY is unset: empty means "the inference
# key" when the capable model is on the inference endpoint, and "no key" on any other endpoint.
if [ -e "$key_file" ]; then
  INFERENCE_API_KEY=$(cat "$key_file") || fail "cannot read $key_file"
fi
if [ -e "$capable_key_file" ]; then
  CAPABLE_API_KEY=$(cat "$capable_key_file") || fail "cannot read $capable_key_file"
fi
capable_endpoint="the inference endpoint"
if [ -n "${CAPABLE_BASE_URL:-}" ] && [ "$CAPABLE_BASE_URL" != "${INFERENCE_BASE_URL:-}" ]; then
  capable_endpoint=$CAPABLE_BASE_URL
fi
capable_key="the inference key"
if [ -n "${CAPABLE_API_KEY:-}" ] && [ "$CAPABLE_API_KEY" != "${INFERENCE_API_KEY:-}" ]; then
  capable_key="its own key"
fi
CAPABLE_BASE_URL=${CAPABLE_BASE_URL:-${INFERENCE_BASE_URL:-}}
if [ "$capable_endpoint" = "the inference endpoint" ]; then
  CAPABLE_API_KEY=${CAPABLE_API_KEY:-${INFERENCE_API_KEY:-}}
fi
CAPABLE_API_KEY=${CAPABLE_API_KEY:-}
export INFERENCE_API_KEY CAPABLE_BASE_URL CAPABLE_API_KEY

require SWITCHYARD_ROUTES
template=$here/routes/$SWITCHYARD_ROUTES.toml.tmpl
[ -f "$template" ] || fail "SWITCHYARD_ROUTES=$SWITCHYARD_ROUTES has no template in routes/"
required=$(sed -n 's/^# requires://p' "$template")
# shellcheck disable=SC2086 # the "# requires:" header is a space-separated list of names
require INFERENCE_API_KEY $required

# Switchyard keeps one target per model id: on one client it drops the second target (or rejects it
# when its settings differ), and within a route it rejects one id on two clients. So the efficient,
# capable and judge models a template uses must all differ, and the aux model (on the efficient
# model's client) must differ from the efficient model. The aux model may be the judge or the capable
# model: those sit on other clients and other routes.
models=""
for name in AGENT_EFFICIENT_MODEL AGENT_CAPABLE_MODEL AGENT_JUDGE_MODEL; do
  case " $required " in
    *" $name "*) models="$models $name" ;;
  esac
done
for a in $models; do
  for b in $models; do
    if [ "$a" != "$b" ] && [ "$(printenv "$a")" = "$(printenv "$b")" ]; then
      fail "$(echo "${models# }" | sed 's/ /, /g') must all differ"
    fi
  done
done
case " $required " in
  *" AGENT_AUX_MODEL "*)
    [ "$AGENT_AUX_MODEL" != "$AGENT_EFFICIENT_MODEL" ] || fail "AGENT_AUX_MODEL must differ from AGENT_EFFICIENT_MODEL"
    ;;
esac

case ${SWITCHYARD_CONFIRMATIONS:-1} in
  1 | 2) ;;
  *) fail "SWITCHYARD_CONFIRMATIONS must be 1 or 2" ;;
esac

# The escalation templates inline the judge prompt, minus its leading comment block.
JUDGE_PROMPT=$(sed '1,/^-->$/d' "$here/judge-prompt.md")
export JUDGE_PROMPT

# Only these names are substituted; any other "$" in a template stays as written.
# shellcheck disable=SC2016
envsubst '${INFERENCE_BASE_URL} ${CAPABLE_BASE_URL} ${AGENT_EFFICIENT_MODEL} ${AGENT_CAPABLE_MODEL}
  ${AGENT_JUDGE_MODEL} ${AGENT_AUX_MODEL} ${SWITCHYARD_CONFIRMATIONS} ${JUDGE_PROMPT}' <"$template" >"$config"

case " $required " in
  *" AGENT_CAPABLE_MODEL "*)
    [ -n "$CAPABLE_API_KEY" ] ||
      fail "CAPABLE_BASE_URL is another endpoint than INFERENCE_BASE_URL: set CAPABLE_API_KEY to its key"
    capable=" capable ${AGENT_CAPABLE_MODEL:-} on $capable_endpoint with $capable_key,"
    ;;
  *) capable="" ;;
esac
judge=""
case " $required " in
  *" AGENT_JUDGE_MODEL "*) judge=" judge ${AGENT_JUDGE_MODEL:-}," ;;
esac
echo "switchyard: $SWITCHYARD_ROUTES on $INFERENCE_BASE_URL (efficient ${AGENT_EFFICIENT_MODEL:-},${capable}${judge}" \
  "aux ${AGENT_AUX_MODEL:-})" >&2
switchyard-server --config "$config" --dry-run
exec switchyard-server --config "$config" --host 0.0.0.0 --port 4000 \
  --routing-log-file "$state_dir/routing.jsonl" "$@"
