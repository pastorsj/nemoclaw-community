# shellcheck shell=bash
# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Logging, exit codes and the Compose wrapper shared by scripts/demo.sh. Needs $ROOT.
# shellcheck disable=SC2034 # the exit codes are used by the files that source this one

readonly EXIT_USAGE=2
readonly EXIT_CONFIG=64      # .env or profile rules
readonly EXIT_UNAVAILABLE=69 # a host requirement or a service is missing

ENV_FILE=$ROOT/.env
VERSIONS_FILE=$ROOT/infra/openshell/versions.env

log() { printf '==> %s\n' "$*" >&2; }
warn() { printf 'warning: %s\n' "$*" >&2; }

# die CODE MESSAGE
die() {
  local code=$1
  shift
  printf 'error: %s\n' "$*" >&2
  exit "$code"
}

# Local images need no provenance attestations, and with them every build of an unchanged image gets a
# new ID, so `up` would recreate every container and the sandbox.
export BUILDX_NO_DEFAULT_ATTESTATIONS=1

# docker compose for this repository: the OpenShell pins, plus .env when it exists.
# (Passing --env-file turns off Compose's own .env lookup, so .env is passed explicitly.)
dc() {
  local env_files=(--env-file "$VERSIONS_FILE")
  if [ -f "$ENV_FILE" ]; then
    env_files+=(--env-file "$ENV_FILE")
  fi
  docker compose -f "$ROOT/compose.yaml" "${env_files[@]}" "$@"
}

# wait_for SECONDS COMMAND...: retry COMMAND every 2 s until it succeeds or time runs out.
wait_for() {
  local deadline=$(($(date +%s) + $1))
  shift
  until "$@" >/dev/null 2>&1; do
    [ "$(date +%s)" -lt "$deadline" ] || return 1
    sleep 2
  done
}

sha256() {
  if command -v sha256sum >/dev/null; then sha256sum; else shasum -a 256; fi | cut -d' ' -f1
}

# 64 hex characters from the kernel's CSPRNG.
random_hex() {
  od -An -tx1 -N32 /dev/urandom | tr -d ' \n'
}
