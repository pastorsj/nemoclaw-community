#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail
DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$DEPLOY_DIR/lib/common.sh"
initialize_deploy_env

arch="$(uname -m)"

if [[ "$arch" == aarch64 || "$arch" == arm64 ]]; then
  require_command git
  image="$NEMO_RETRIEVER_IMAGE"
  image_revision="$(docker image inspect --format \
    '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
    "$image" 2>/dev/null || true)"
  if [[ "$image_revision" != "$QUERY_CLAW_RETRIEVER_COMMIT" ]]; then
    if [[ ! -d "$NEMO_RETRIEVER_SOURCE_DIR/.git" ]]; then
      mkdir -p "$(dirname "$NEMO_RETRIEVER_SOURCE_DIR")"
      git clone --depth 1 --branch "$QUERY_CLAW_RETRIEVER_VERSION" \
        https://github.com/NVIDIA/NeMo-Retriever.git "$NEMO_RETRIEVER_SOURCE_DIR"
    fi
    actual="$(git -C "$NEMO_RETRIEVER_SOURCE_DIR" rev-parse HEAD)"
    [[ "$actual" == "$QUERY_CLAW_RETRIEVER_COMMIT" ]] || \
      die "NeMo Retriever $QUERY_CLAW_RETRIEVER_VERSION resolved to unexpected commit $actual"
    [[ -z "$(git -C "$NEMO_RETRIEVER_SOURCE_DIR" status --porcelain --untracked-files=all)" ]] || \
      die "NeMo Retriever source has local changes; use a clean $QUERY_CLAW_RETRIEVER_COMMIT checkout"

    patched="$RUNTIME_DIR/retriever-arm64.Dockerfile"
    python3 - "$NEMO_RETRIEVER_SOURCE_DIR/Dockerfile" "$patched" <<'PY'
from pathlib import Path
import sys

source, destination = map(Path, sys.argv[1:])
text = source.read_text(encoding="utf-8")
block = """RUN --mount=type=cache,target=/root/.cache/uv \\
    . /opt/retriever_runtime/bin/activate \\
    && wget https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/x86_64/cuda-keyring_1.1-1_all.deb \\
    && dpkg -i cuda-keyring_1.1-1_all.deb \\
    && apt update && apt-get --fix-broken install -y && apt-get -y install cuda-toolkit-13-0
"""
if text.count(block) != 1:
    raise SystemExit("pinned Retriever Dockerfile no longer has the expected x86 CUDA block")
destination.write_text(text.replace(block, ""), encoding="utf-8")
PY
    docker build \
      --file "$patched" \
      --target service \
      --build-arg DOWNLOAD_DEFAULT_TOKENIZER=True \
      --build-arg RETRIEVER_VERSION="$QUERY_CLAW_RETRIEVER_VERSION" \
      --build-arg RETRIEVER_RELEASE_TYPE=release \
      --label "org.opencontainers.image.revision=$QUERY_CLAW_RETRIEVER_COMMIT" \
      --tag "$image" \
      "$NEMO_RETRIEVER_SOURCE_DIR"
  fi
elif [[ "$arch" != x86_64 && "$arch" != amd64 ]]; then
  die "unsupported host architecture: $arch"
fi

data_fingerprint="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["fingerprint"])' "$QUERY_CLAW_ACTIVE_MANIFEST")"
fingerprint="$(python3 - "$data_fingerprint" "$NEMO_RETRIEVER_IMAGE" \
  "$NVIDIA_EMBED_INVOKE_URL" "$NVIDIA_EMBED_MODEL" \
  "$NVIDIA_EMBED_MODEL_PROVIDER_PREFIX" <<'PY'
import hashlib
import json
import sys

print(hashlib.sha256(json.dumps(sys.argv[1:], separators=(",", ":")).encode()).hexdigest())
PY
)"
marker="$RUNTIME_DIR/retriever.fingerprint"
needs_ingest=0
if [[ ! -f "$marker" ]] || [[ "$(<"$marker")" != "$fingerprint" ]] || \
  ! docker volume inspect query-claw_retriever_data >/dev/null 2>&1; then
  needs_ingest=1
  compose rm --stop --force retriever >/dev/null 2>&1 || true
  docker volume rm query-claw_retriever_data >/dev/null 2>&1 || true
fi

# Dataset activation publishes the active directory with an atomic rename.
# Recreate the container so Docker binds the newly published directory inode;
# a plain `up` can retain the now-unlinked directory from the prior dataset.
compose up -d --force-recreate retriever
wait_http http://127.0.0.1:7670/v1/health "NeMo Retriever" 120

if (( needs_ingest )); then
  collection_action=ingest
else
  collection_action=verify
fi
if ! compose exec -T retriever python \
  /opt/query-claw/retriever_collections.py "$collection_action"; then
  rm -f "$marker"
  die "NeMo Retriever active collection qualification failed"
fi
printf '%s\n' "$fingerprint" >"$marker"

image_digest=""
if [[ "$NEMO_RETRIEVER_IMAGE" == *@sha256:* ]]; then
  image_digest="${NEMO_RETRIEVER_IMAGE##*@}"
fi
image_revision="$(docker image inspect --format \
  '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
  "$NEMO_RETRIEVER_IMAGE" 2>/dev/null || true)"
if [[ ! "$image_revision" =~ ^[0-9a-f]{40}$ ]]; then
  image_revision=""
fi

receipt="$RUNTIME_DIR/retriever-provenance.json"
temporary="$(mktemp "$RUNTIME_DIR/.retriever-provenance.XXXXXX")"
provenance_args=(
  /opt/query-claw/retriever_collections.py provenance
  --expected-version "$QUERY_CLAW_RETRIEVER_VERSION"
)
if [[ -n "$image_digest" ]]; then
  provenance_args+=(--image-digest "$image_digest")
fi
if [[ -n "$image_revision" ]]; then
  provenance_args+=(--image-revision "$image_revision")
fi
if ! compose exec -T retriever python "${provenance_args[@]}" >"$temporary"; then
  rm -f "$temporary" "$receipt"
  die "NeMo Retriever runtime provenance capture failed"
fi
chmod 600 "$temporary"
mv -f "$temporary" "$receipt"

printf 'ready: NeMo Retriever %s with the active dataset collection\n' \
  "$QUERY_CLAW_RETRIEVER_VERSION"
printf 'ready: native NeMo Retriever MCP at /mcp\n'
