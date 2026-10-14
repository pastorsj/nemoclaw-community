#!/bin/sh
# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Export the pinned Auto Ontology commit to .build/auto-ontology and apply our patches.
# Compose builds the ontology profile's images from that directory.
#
#   tools/auto-ontology/prepare.sh           # (re)create .build/auto-ontology
#   tools/auto-ontology/prepare.sh --check   # only check that every patch applies
set -eu

here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
source_repo="$root/vendor/auto-ontology"
out="$root/.build/auto-ontology"

if [ ! -e "$source_repo/.git" ]; then
  echo "vendor/auto-ontology is not checked out. From this example's directory, run:" >&2
  echo "  git submodule update --init --checkout vendor/auto-ontology" >&2
  exit 1
fi
# The pin is the commit the superproject records for the submodule (":./" resolves from this example's directory).
if ! pin=$(git -C "$root" rev-parse --verify --quiet ":./vendor/auto-ontology"); then
  echo "vendor/auto-ontology is not recorded as a submodule in this repository." >&2
  exit 1
fi

check=false
if [ "${1:-}" = "--check" ]; then
  check=true
  out=$(mktemp -d)
  trap 'rm -rf "$out"' EXIT
fi

rm -rf "$out"
mkdir -p "$out"
git -C "$source_repo" archive "$pin" | tar -x -C "$out"
# A repository of its own: inside this one, `git apply` would resolve paths against our root and skip them.
git -C "$out" init --quiet
for patch in "$here"/patches/backend/*.patch "$here"/patches/mcp/*.patch; do
  git -C "$out" apply "$patch"
  echo "applied ${patch#"$here"/}"
done
if [ "$check" = true ]; then
  echo "All patches apply to Auto Ontology ${pin}."
else
  echo "Patched Auto Ontology ${pin} is in .build/auto-ontology."
fi
