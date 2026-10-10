#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Regenerate the committed example outputs under examples/*/expected/. The test
# suite compiles the same inputs and compares against these files, so run this
# after an intended change in compiler behavior and review the diff.

set -euo pipefail

tool_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${tool_dir}"
run() { uv run policy-compiler "$@" --spdx-header >/dev/null; }

run compile examples/github-repo-explainer/agent.yaml \
  --client /usr/bin/python3 --forbid github:write \
  --out examples/github-repo-explainer/expected

run compile examples/internal-orders-api/agent.yaml \
  --catalog examples/internal-orders-api/catalog \
  --client /usr/bin/python3 --forbid acme:admin \
  --use-case "Look up order status and search orders for customers" --strict-use-case \
  --out examples/internal-orders-api/expected

run from-use-case "$(cat examples/use-case-only/use-case.txt)" \
  --name understand-openshell --client /usr/bin/python3 --client /usr/bin/git \
  --out examples/use-case-only/expected

run from-use-case "I need access to code" \
  --name ambiguous-code-access --client /usr/bin/python3 \
  --out examples/use-case-only/expected-ambiguous

# Paths in generated reports are relative to the tool directory; nothing else is machine specific.
echo "Regenerated examples/*/expected*/."
