# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""How scripts/live_check.py reads curl results from inside a sandbox.

The texts below were captured from a real OpenShell sandbox. Only a refusal by
the OpenShell proxy counts as a deny; a request that fails for another reason
proves nothing and must be an error.
"""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "live_check.py"


@pytest.fixture(scope="module")
def live_check():
    spec = importlib.util.spec_from_file_location("live_check", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


POLICY_DENIED_BODY = (
    '{"binary":"/usr/bin/curl","detail":"DELETE /repos/NVIDIA/OpenShell not permitted by policy",'
    '"error":"policy_denied","host":"api.github.com","layer":"l7"}'
)


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "outcome"),
    [
        (56, "\n000", "curl: (56) CONNECT tunnel failed, response 403\n", "deny"),
        (0, POLICY_DENIED_BODY + "\n403", "", "deny"),
        (
            0,
            "HTTP/1.1 200 Connection Established\r\n\r\nHTTP/1.1 403 Forbidden\r\nX-OpenShell-Policy: github_rest\r\n\r\n\n403",
            "",
            "deny",
        ),
        (0, '{"message":"Not Found"}\n404', "", "allow"),
        (0, '{"message":"Requires authentication"}\n401', "", "allow"),
        (0, '{"message":"Forbidden by upstream"}\n403', "", "allow"),
        (6, "\n000", "curl: (6) Could not resolve host: api.github.com\n", "error"),
        (28, "\n000", "curl: (28) Operation timed out after 30001 milliseconds with 0 bytes received\n", "error"),
        (56, "\n000", "curl: (56) CONNECT tunnel failed, response 502\n", "error"),
        (1, "", "error: sandbox opc-missing not found\n", "error"),
        (0, "", "", "error"),
    ],
)
def test_classify(live_check, returncode, stdout, stderr, outcome):
    assert live_check.classify(returncode, stdout, stderr)[0] == outcome


def test_head_uses_dash_capital_i(live_check):
    args = live_check.curl_args({"method": "HEAD", "url": "https://api.github.com/repos/NVIDIA/OpenShell"})
    assert "-I" in args
    assert "-X" not in args
    assert live_check.curl_args({"method": "GET", "url": "https://x"})[-3:] == ["-X", "GET", "https://x"]


def test_applied_policy_is_not_written_next_to_the_input():
    text = SCRIPT.read_text()
    assert 'with_name("applied-policy.yaml")' not in text
    assert "tempfile.TemporaryDirectory" in text
