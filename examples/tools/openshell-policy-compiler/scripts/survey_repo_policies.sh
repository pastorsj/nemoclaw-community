#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Explain every OpenShell policy file in this repository and mine them into a
# catalog. Writes a breadth table, a host usage index, and a mined catalog under
# out/survey/. Run from the tool directory.

set -euo pipefail

tool_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo_examples="$(cd "${tool_dir}/../.." && pwd)"
out_dir="${tool_dir}/out/survey"
mkdir -p "${out_dir}"

files=()
while IFS= read -r file; do
  files+=("${file}")
done < <(
  cd "${repo_examples}/.." &&
    find examples -type d \( -name node_modules -o -name .venv -o -name openshell-policy-compiler \) -prune -o \
      -type f \( -name 'policy*.yaml' -o -path '*/policies/*.yaml' -o -path '*/policies/*.json' \) -print | sort
)
echo "Found ${#files[@]} policy files."

cd "${repo_examples}/.."
uv run --project "${tool_dir}" policy-compiler explain "${files[@]}" >"${out_dir}/breadth.md"
uv run --project "${tool_dir}" policy-compiler import openshell-policies "${files[@]}" \
  --out "${out_dir}/mined-catalog.yaml" --usage "${out_dir}/usage.json" 2>"${out_dir}/hosts.md"
echo "Wrote ${out_dir}/breadth.md, hosts.md, usage.json, and mined-catalog.yaml."
