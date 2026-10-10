# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""``load_policy_file`` reads saved ``openshell policy get`` output as well as plain policy files.

``openshell policy get NAME --base`` prints ``Key: value`` lines, a line that is
exactly ``---``, then the policy YAML (``sandbox_policy_get_effective_to_writer``
in ``crates/openshell-cli/src/run.rs``). Only such a header is removed; anything
else before a ``---`` is left for the YAML parser, which rejects a stream of
more than one document.
"""

import pytest
import yaml

from policy_compiler.backends.openshell import load_policy_file

POLICY_YAML = """\
version: 1
filesystem_policy:
  include_workdir: true
  read_only:
  - /usr
network_policies:
  github:
    endpoints:
    - host: api.github.com
      port: 443
"""

POLICY = yaml.safe_load(POLICY_YAML)

EFFECTIVE_HEADER = """\
Version:      3
Hash:         4f0c2a9d1e
Status:       Effective
Source:       sandbox
Config rev:   7
"""

REVISION_HEADER = """\
Version:      2
Hash:         sha256:4f0c2a9d1e
Status:       Loaded
Active:       2
Created:      1760000000000 ms
Loaded:       1760000001000 ms
"""


def _write(tmp_path, text, name="policy.yaml"):
    path = tmp_path / name
    path.write_bytes(text.encode("utf-8"))
    return path


@pytest.mark.parametrize(
    "header",
    [
        "Version: 3\nHash: abc\nStatus: active\n",
        EFFECTIVE_HEADER,
        REVISION_HEADER,
        "Version: 3\n\nHash: abc\n\nStatus: active\n\n",
        "\nVersion: 3\nError:        failed: bad key: x\n",
    ],
    ids=["minimal", "effective", "revision", "blank-lines", "colons-in-value"],
)
def test_policy_get_output_is_accepted(tmp_path, header):
    path = _write(tmp_path, header + "---\n" + POLICY_YAML, name="current.txt")
    assert load_policy_file(path) == POLICY


def test_policy_get_output_with_crlf_line_endings(tmp_path):
    text = (EFFECTIVE_HEADER + "---\n" + POLICY_YAML).replace("\n", "\r\n")
    assert load_policy_file(_write(tmp_path, text)) == POLICY


def test_policy_get_body_that_starts_with_a_document_marker(tmp_path):
    path = _write(tmp_path, EFFECTIVE_HEADER + "---\n---\n" + POLICY_YAML)
    assert load_policy_file(path) == POLICY


@pytest.mark.parametrize(
    "text",
    [
        POLICY_YAML,
        "---\n" + POLICY_YAML,
        "# a comment\n---\n" + POLICY_YAML,
        "\n---\n" + POLICY_YAML,
    ],
    ids=["plain", "document-start", "comment-then-document-start", "blank-then-document-start"],
)
def test_plain_yaml_still_loads(tmp_path, text):
    assert load_policy_file(_write(tmp_path, text)) == POLICY


@pytest.mark.parametrize(
    "body",
    ["- just\n- a list\n", "", "just a string\n"],
    ids=["list", "empty", "scalar"],
)
def test_policy_get_output_without_a_mapping_is_rejected(tmp_path, body):
    with pytest.raises(ValueError, match="expected a mapping"):
        load_policy_file(_write(tmp_path, EFFECTIVE_HEADER + "---\n" + body))


@pytest.mark.parametrize(
    "prefix",
    [
        # Lowercase keys are policy YAML, not a ``policy get`` header.
        "version: 1\n",
        # An indented line is YAML structure, not a header line.
        "Version: 3\n  nested: x\n",
        # A comment is not a header line.
        "# saved from openshell\nVersion: 3\n",
        # A key without the colon-and-space shape.
        "Version:3\n",
        # A list item.
        "Version: 3\n- item\n",
    ],
    ids=["lowercase-key", "indented", "comment", "no-space", "list-item"],
)
def test_only_a_strict_header_is_stripped(tmp_path, prefix):
    """Text before ``---`` that is not purely a header is not dropped; the YAML parser sees it."""
    with pytest.raises(yaml.YAMLError):
        load_policy_file(_write(tmp_path, prefix + "---\n" + POLICY_YAML))


def test_separator_must_be_exactly_three_dashes(tmp_path):
    with pytest.raises(yaml.YAMLError):
        load_policy_file(_write(tmp_path, "Version: 3\n--- \n" + POLICY_YAML))
    with pytest.raises(yaml.YAMLError):
        load_policy_file(_write(tmp_path, "Version: 3\n----\n" + POLICY_YAML))


def test_json_is_not_header_stripped(tmp_path):
    path = _write(tmp_path, "Version: 3\n---\n{}\n", name="policy.json")
    with pytest.raises(ValueError):
        load_policy_file(path)
