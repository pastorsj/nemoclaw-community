#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail
DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$DEPLOY_DIR/lib/common.sh"
initialize_deploy_env

RETRIEVER_COMMIT=1992e3f09746b9fc150a266567c9737746781fdd
RETRIEVER_TAG=26.08.1
arch="$(uname -m)"

if [[ "$arch" == aarch64 || "$arch" == arm64 ]]; then
  require_command git
  image="query-claw-retriever:${RETRIEVER_TAG}-arm64"
  image_revision="$(docker image inspect --format \
    '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
    "$image" 2>/dev/null || true)"
  if [[ "$image_revision" != "$RETRIEVER_COMMIT" ]]; then
    if [[ ! -d "$NEMO_RETRIEVER_SOURCE_DIR/.git" ]]; then
      mkdir -p "$(dirname "$NEMO_RETRIEVER_SOURCE_DIR")"
      git clone --depth 1 --branch "$RETRIEVER_TAG" \
        https://github.com/NVIDIA/NeMo-Retriever.git "$NEMO_RETRIEVER_SOURCE_DIR"
    fi
    actual="$(git -C "$NEMO_RETRIEVER_SOURCE_DIR" rev-parse HEAD)"
    [[ "$actual" == "$RETRIEVER_COMMIT" ]] || \
      die "NeMo Retriever $RETRIEVER_TAG resolved to unexpected commit $actual"
    [[ -z "$(git -C "$NEMO_RETRIEVER_SOURCE_DIR" status --porcelain --untracked-files=all)" ]] || \
      die "NeMo Retriever source has local changes; use a clean $RETRIEVER_COMMIT checkout"

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
      --build-arg RETRIEVER_VERSION="$RETRIEVER_TAG" \
      --build-arg RETRIEVER_RELEASE_TYPE=release \
      --label "org.opencontainers.image.revision=$RETRIEVER_COMMIT" \
      --tag "$image" \
      "$NEMO_RETRIEVER_SOURCE_DIR"
  fi
  export NEMO_RETRIEVER_IMAGE="$image"
elif [[ "$arch" != x86_64 && "$arch" != amd64 ]]; then
  die "unsupported host architecture: $arch"
fi

data_fingerprint="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["fingerprint"])' "$RUNTIME_DIR/data/manifest.json")"
fingerprint="$(python3 - "$data_fingerprint" "$NEMO_RETRIEVER_IMAGE" \
  "$NVIDIA_EMBED_INVOKE_URL" "$NVIDIA_EMBED_MODEL" <<'PY'
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

compose up -d retriever
wait_http http://127.0.0.1:7670/v1/health "NeMo Retriever" 120

if (( needs_ingest )); then
  compose exec -T retriever retriever ingest service /query-claw-documents \
    --service-url http://127.0.0.1:7670 \
    --service-concurrency 2
fi

results="$(compose exec -T retriever retriever query service "Atlas Circuits" \
  --service-url http://127.0.0.1:7670 --top-k 10 --format hits \
  --max-text-chars 200)" || {
  rm -f "$marker"
  die "NeMo Retriever qualification query failed"
}
if ! python3 -c '
import json,sys
rows=json.load(sys.stdin)
sources={str(row.get("source", "")).rsplit("/", 1)[-1] for row in rows}
expected={f"sup-{index:03d}-notice.md" for index in range(1, 11)}
raise SystemExit(0 if len(rows) == 10 and sources == expected and "Atlas Circuits" in str(rows[0].get("text", "")) else 1)
' <<<"$results"; then
  rm -f "$marker"
  die "NeMo Retriever does not contain the ten expected Query Claw documents"
fi
printf '%s\n' "$fingerprint" >"$marker"

printf 'ready: NeMo Retriever %s with Query Claw documents\n' "$RETRIEVER_TAG"
