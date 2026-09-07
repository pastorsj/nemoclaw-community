# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import base64
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch


EXAMPLE_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH: list[Path] = [Path("config.yaml")]
WRITES: list[dict] = []
PLUGIN_TOOLSETS = {
    "query-claw",
    "unrelated-plugin",
}


def fake_yaml_load(stream):
    raw = stream.read()
    if raw == "model: test\n":
        return {"model": "test"}
    if raw == "- item\n":
        return ["item"]
    if not raw:
        return None
    raise ValueError("malformed YAML")


def fake_platform_tools(config, platform):
    configured = set(config.get("platform_toolsets", {}).get(platform, []))
    disabled = set(config.get("agent", {}).get("disabled_toolsets", []))
    known = set(config.get("known_plugin_toolsets", {}).get(platform, []))
    configured.update(PLUGIN_TOOLSETS - known)
    return configured - disabled


def load_profile_module():
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.__path__ = []
    config = types.ModuleType("hermes_cli.config")
    config.atomic_config_write = lambda _path, data: WRITES.append(deepcopy(data))
    config.get_config_path = lambda: CONFIG_PATH[0]
    tools_config = types.ModuleType("hermes_cli.tools_config")
    tools_config._get_platform_tools = fake_platform_tools
    tools_config._get_plugin_toolset_keys = lambda: PLUGIN_TOOLSETS
    utils = types.ModuleType("utils")
    utils.fast_safe_load = fake_yaml_load
    modules = {
        "hermes_cli": hermes_cli,
        "hermes_cli.config": config,
        "hermes_cli.tools_config": tools_config,
        "utils": utils,
    }
    path = EXAMPLE_ROOT / "scripts" / "hermes_profile.py"
    spec = importlib.util.spec_from_file_location("query_claw_hermes_profile", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


PROFILE = load_profile_module()


def encode(value: dict) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return base64.b64encode(raw.encode()).decode()


class HermesProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        WRITES.clear()
        self.before = {
            "platform_toolsets": {"api_server": ["web"]},
            "tools": {"tool_search": {"enabled": "on"}},
            "operator": {"setting": "preserve-me"},
        }

    def applied(self, config: dict, receipt: dict) -> dict:
        live = deepcopy(config)
        self.assertTrue(PROFILE.apply(live, encode(receipt)))
        applied = WRITES[-1]
        PROFILE.verify(applied, encode(receipt))
        return applied

    def test_receipt_applies_exact_narrow_surface_and_restores(self) -> None:
        receipt = PROFILE.snapshot(self.before)
        self.assertEqual(2, receipt["schema_version"])
        for state in (receipt["before"], receipt["applied"]):
            self.assertEqual(
                state["sha256"], PROFILE.projection_fingerprint(state["fields"])
            )

        live = self.applied(self.before, receipt)
        self.assertEqual(
            PROFILE.TARGET_TOOLSETS, live["platform_toolsets"]["api_server"]
        )
        self.assertEqual(
            sorted(PLUGIN_TOOLSETS), live["known_plugin_toolsets"]["api_server"]
        )
        self.assertNotIn("unrelated-plugin", fake_platform_tools(live, "api_server"))
        self.assertEqual(PROFILE.TARGET_TOOL_SEARCH, live["tools"]["tool_search"])
        self.assertEqual(PROFILE.TARGET_SYSTEM_PROMPT, live["agent"]["system_prompt"])
        self.assertIs(live["skills"]["write_approval"], True)
        self.assertIs(live["skills"]["guard_agent_created"], True)

        live["operator"]["setting"] = "edited-after-setup"
        self.assertTrue(PROFILE.restore(live, encode(receipt)))
        restored = WRITES[-1]
        self.assertEqual("edited-after-setup", restored["operator"]["setting"])
        self.assertEqual(
            PROFILE.projection_state(self.before), PROFILE.projection_state(restored)
        )

    def test_apply_and_restore_are_idempotent_but_refuse_drift(self) -> None:
        receipt = PROFILE.snapshot(self.before)
        live = self.applied(self.before, receipt)
        self.assertFalse(PROFILE.apply(live, encode(receipt)))
        live["code_execution"]["mode"] = "operator-edit"
        with self.assertRaisesRegex(ValueError, "operator changes"):
            PROFILE.restore(live, encode(receipt))

    def test_reconciliation_is_retry_safe(self) -> None:
        before = deepcopy(self.before)
        receipt = PROFILE.snapshot(before)
        current = self.applied(before, receipt)
        prepared = PROFILE.prepare(current, encode(receipt))
        self.assertIn("previous_applied", prepared)
        self.assertFalse(PROFILE.apply(current, encode(prepared)))
        PROFILE.verify(current, encode(prepared))
        self.assertIn("skills", current["platform_toolsets"]["api_server"])

        # A failure between prepare and apply can still restore the original.
        self.assertTrue(PROFILE.restore(current, encode(prepared)))
        self.assertEqual(
            PROFILE.projection_state(before), PROFILE.projection_state(WRITES[-1])
        )

    def test_receipt_tamper_and_managed_drift_are_rejected(self) -> None:
        receipt = PROFILE.snapshot(self.before)
        receipt["applied"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            PROFILE.restore(deepcopy(self.before), encode(receipt))

        receipt = PROFILE.snapshot(self.before)
        drifted = deepcopy(self.before)
        drifted["tools"]["tool_search"] = {"operator": "edit"}
        with self.assertRaisesRegex(ValueError, "operator changes"):
            PROFILE.apply(drifted, encode(receipt))

    def test_existing_config_must_parse_as_a_yaml_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            CONFIG_PATH[0] = Path(directory) / "config.yaml"
            self.assertEqual({}, PROFILE.read_strict_config())
            CONFIG_PATH[0].write_text("model: test\n", encoding="utf-8")
            self.assertEqual({"model": "test"}, PROFILE.read_strict_config())
            CONFIG_PATH[0].write_text("- item\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "YAML mapping"):
                PROFILE.read_strict_config()
            CONFIG_PATH[0].write_text("broken: [\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not valid YAML"):
                PROFILE.read_strict_config()


if __name__ == "__main__":
    unittest.main()
