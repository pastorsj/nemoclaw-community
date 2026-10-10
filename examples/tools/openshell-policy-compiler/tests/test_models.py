# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import pytest

from policy_compiler.ir.access import ClientSet
from policy_compiler.ir.common import Privilege, method_privilege
from policy_compiler.ir.contract import Prohibition, RawEndpoint, Target


def test_target_requires_exactly_one_level():
    with pytest.raises(ValueError):
        Target()
    with pytest.raises(ValueError):
        Target(concept="source-code.read", capability="github.contents:read")
    target = Target(concept="source-code.read", provider="github", scope={"owner": "NVIDIA"})
    assert target.level == "concept"
    assert "on github" in target.describe()


def test_raw_target_describe():
    target = Target(raw=RawEndpoint(host="api.example.com", port=443, method="GET", path="/v1/{x}"))
    assert target.describe() == "https://api.example.com:443 GET /v1/{x}"


def test_prohibition_needs_subject():
    with pytest.raises(ValueError):
        Prohibition(id="p1", rationale="none")


def test_privilege_ordering_and_method_guess():
    assert Privilege.ADMIN.at_least(Privilege.WRITE)
    assert not Privilege.READ.at_least(Privilege.WRITE)
    assert Privilege.UNKNOWN.at_least(Privilege.ADMIN)
    assert method_privilege("get") is Privilege.READ
    assert method_privilege("DELETE") is Privilege.WRITE


def test_client_set_of_dedupes_and_handles_empty():
    assert ClientSet.of(["/usr/bin/git", "/usr/bin/git"]).paths == ("/usr/bin/git",)
    assert ClientSet.of([]).mode == "none"
