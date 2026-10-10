# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""How scripts/live_check.py reads curl results and treats a sandbox you name.

The texts below were captured from a real OpenShell sandbox. Only a refusal by
the OpenShell proxy counts as a deny; a request that fails for another reason
proves nothing and must be an error.

With ``--sandbox NAME`` the script replaces that sandbox's network policy while
it probes, so it must save the current policy first and put it back afterwards,
even when a probe or the apply fails.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

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


# --- --sandbox NAME: save the sandbox's policy, probe, then restore it ------------------

SANDBOX = "mine"

BASE_POLICY_YAML = """\
version: 1
filesystem_policy:
  include_workdir: true
  read_only:
  - /usr
network_policies:
  existing:
    endpoints:
    - host: pypi.org
      port: 443
"""

POLICY_GET_OUTPUT = (
    "Version:      3\nHash:         4f0c2a9d1e\nStatus:       Effective\nSource:       sandbox\nConfig rev:   7\n---\n"
    + BASE_POLICY_YAML
)

COMPILED = {
    "version": 1,
    "network_policies": {"github": {"endpoints": [{"host": "api.github.com", "port": 443}]}},
}


class FakeOpenShell:
    """Stands in for ``subprocess.run``; records each command and the policy file it was given."""

    def __init__(self, *, get=(0, POLICY_GET_OUTPUT, ""), apply_rc=0, restore_rc=0, probe_error=None):
        self.get = get
        self.set_rcs = [apply_rc, restore_rc]
        self.probe_error = probe_error
        self.calls = []
        self.policy_texts = []

    def __call__(self, args, **kwargs):
        args = list(args)
        self.calls.append(args)
        kind = self.kind(args)
        if kind == "get":
            return subprocess.CompletedProcess(args, *self.get)
        if kind == "set":
            self.policy_texts.append(self.policy_path(args).read_text())
            rc = self.set_rcs.pop(0)
            return subprocess.CompletedProcess(args, rc, "" if rc else "policy loaded\n", "boom\n" if rc else "")
        if kind == "exec":
            if self.probe_error is not None:
                raise self.probe_error
            return subprocess.CompletedProcess(args, 0, '{"message":"Not Found"}\n404', "")
        return subprocess.CompletedProcess(args, 0, "", "")

    @staticmethod
    def kind(args):
        """``get``/``set`` for ``openshell policy ...``; ``create``/``exec``/``delete`` for ``openshell sandbox ...``."""
        return args[2]

    @staticmethod
    def policy_path(call):
        return Path(call[call.index("--policy") + 1])

    def kinds(self):
        return [self.kind(call) for call in self.calls]


@pytest.fixture
def inputs(tmp_path):
    policy = tmp_path / "policy.yaml"
    policy.write_text(yaml.safe_dump(COMPILED, sort_keys=False))
    probes = tmp_path / "probes.yaml"
    probe = {"method": "GET", "url": "https://api.github.com/repos/NVIDIA/OpenShell", "expect": "allow"}
    probes.write_text(yaml.safe_dump({"probes": [probe]}))
    return ["--policy", str(policy), "--probes", str(probes)]


def run_main(live_check, monkeypatch, fake, argv):
    monkeypatch.setattr(live_check.subprocess, "run", fake)
    monkeypatch.setattr(sys, "argv", ["live_check.py", *argv])
    return live_check.main()


def assert_only_named_sandbox(fake):
    """Every command names the sandbox explicitly, none is global, and no sandbox is created or deleted."""
    for call in fake.calls:
        assert call[0] == "openshell"
        assert "--global" not in call
        assert fake.kind(call) in {"get", "set", "exec"}, call
        if fake.kind(call) == "exec":
            assert call[call.index("-n") + 1] == SANDBOX
        else:
            assert call[3] == SANDBOX


def test_named_sandbox_policy_is_saved_applied_probed_and_restored(live_check, monkeypatch, capsys, inputs):
    fake = FakeOpenShell()
    assert run_main(live_check, monkeypatch, fake, [*inputs, "--sandbox", SANDBOX]) == 0

    assert fake.kinds() == ["get", "set", "exec", "set"]
    get, apply, _, restore = fake.calls
    assert get == ["openshell", "policy", "get", SANDBOX, "--base"]
    assert apply == ["openshell", "policy", "set", SANDBOX, "--policy", str(fake.policy_path(apply)), "--wait"]
    saved = fake.policy_path(restore)
    assert restore == ["openshell", "policy", "set", SANDBOX, "--policy", str(saved), "--wait"]
    assert saved != fake.policy_path(apply)
    assert_only_named_sandbox(fake)

    applied = yaml.safe_load(fake.policy_texts[0])
    assert applied["network_policies"] == COMPILED["network_policies"]
    assert applied["filesystem_policy"] == yaml.safe_load(BASE_POLICY_YAML)["filesystem_policy"]
    assert fake.policy_texts[1] == BASE_POLICY_YAML
    assert not saved.exists()

    err = capsys.readouterr().err
    assert "warning" in err.lower() and SANDBOX in err and "replace" in err


@pytest.mark.parametrize("error", [RuntimeError("probe crashed"), KeyboardInterrupt()], ids=["error", "interrupt"])
def test_named_sandbox_policy_is_restored_when_a_probe_raises(live_check, monkeypatch, inputs, error):
    fake = FakeOpenShell(probe_error=error)
    with pytest.raises(type(error)):
        run_main(live_check, monkeypatch, fake, [*inputs, "--sandbox", SANDBOX])

    assert fake.kinds() == ["get", "set", "exec", "set"]
    restore = fake.calls[-1]
    assert restore == ["openshell", "policy", "set", SANDBOX, "--policy", str(fake.policy_path(restore)), "--wait"]
    assert fake.policy_texts[-1] == BASE_POLICY_YAML
    assert_only_named_sandbox(fake)


def test_named_sandbox_policy_is_restored_when_the_apply_fails(live_check, monkeypatch, inputs):
    fake = FakeOpenShell(apply_rc=1)
    with pytest.raises(SystemExit):
        run_main(live_check, monkeypatch, fake, [*inputs, "--sandbox", SANDBOX])

    assert fake.kinds() == ["get", "set", "set"]
    assert fake.calls[-1][-1] == "--wait"
    assert fake.policy_texts[-1] == BASE_POLICY_YAML
    assert_only_named_sandbox(fake)


@pytest.mark.parametrize(
    "get",
    [
        (1, "", "error: sandbox mine not found\n"),
        (0, "", ""),
        (0, "Version:      3\nHash:         4f0c2a9d1e\nStatus:       Effective\n", ""),
        (0, "Version:      3\n---\n- not\n- a mapping\n", ""),
        (0, "Version:      3\n---\nversion: [unclosed\n", ""),
    ],
    ids=["get-fails", "empty", "header-only", "not-a-mapping", "bad-yaml"],
)
def test_named_sandbox_is_not_changed_when_saving_its_policy_fails(live_check, monkeypatch, inputs, get):
    fake = FakeOpenShell(get=get)
    with pytest.raises(SystemExit):
        run_main(live_check, monkeypatch, fake, [*inputs, "--sandbox", SANDBOX])
    assert fake.kinds() == ["get"]


def test_failed_restore_is_loud_and_keeps_the_saved_policy(live_check, monkeypatch, capsys, inputs):
    fake = FakeOpenShell(restore_rc=1)
    assert run_main(live_check, monkeypatch, fake, [*inputs, "--sandbox", SANDBOX]) != 0

    assert fake.kinds() == ["get", "set", "exec", "set"]
    saved = fake.policy_path(fake.calls[-1])
    try:
        assert saved.read_text() == BASE_POLICY_YAML
        err = capsys.readouterr().err
        assert "ERROR" in err and str(saved) in err and SANDBOX in err
    finally:
        saved.unlink(missing_ok=True)


def test_created_sandbox_is_probed_and_deleted_without_a_restore(live_check, monkeypatch, inputs):
    fake = FakeOpenShell()
    assert run_main(live_check, monkeypatch, fake, inputs) == 0
    assert fake.kinds() == ["create", "get", "set", "exec", "delete"]
    assert all("--global" not in call for call in fake.calls)
