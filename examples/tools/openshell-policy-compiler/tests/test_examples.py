# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The committed example outputs match what the compiler produces today.

After an intended behavior change, run ``scripts/regenerate_examples.sh`` and
review the diff of ``examples/*/expected*/``.
"""

import json
from pathlib import Path

import pytest
import yaml

from policy_compiler.cli import main

TOOL = Path(__file__).resolve().parents[1]

CASES = {
    "github-repo-explainer/expected": [
        "compile",
        "examples/github-repo-explainer/agent.yaml",
        "--client",
        "/usr/bin/python3",
        "--forbid",
        "github:write",
    ],
    "internal-orders-api/expected": [
        "compile",
        "examples/internal-orders-api/agent.yaml",
        "--catalog",
        "examples/internal-orders-api/catalog",
        "--client",
        "/usr/bin/python3",
        "--forbid",
        "acme:admin",
        "--use-case",
        "Look up order status and search orders for customers",
        "--strict-use-case",
    ],
    "use-case-only/expected": [
        "from-use-case",
        (TOOL / "examples/use-case-only/use-case.txt").read_text().strip(),
        "--name",
        "understand-openshell",
        "--client",
        "/usr/bin/python3",
        "--client",
        "/usr/bin/git",
    ],
    "use-case-only/expected-ambiguous": [
        "from-use-case",
        "I need access to code",
        "--name",
        "ambiguous-code-access",
        "--client",
        "/usr/bin/python3",
    ],
}


@pytest.mark.parametrize("expected", sorted(CASES))
def test_example_outputs_match(expected, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(TOOL)
    out = tmp_path / "out"
    assert main([*CASES[expected], "--spdx-header", "--out", str(out)]) == 0
    capsys.readouterr()
    committed = TOOL / "examples" / expected
    for name in ("policy.yaml", "boundary.yaml", "contract.yaml"):
        assert yaml.safe_load((out / name).read_text()) == yaml.safe_load((committed / name).read_text()), name
    assert json.loads((out / "report.json").read_text()) == json.loads((committed / "report.json").read_text())
