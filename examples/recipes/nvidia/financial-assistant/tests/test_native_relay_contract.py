# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Offline contracts for the recipe's native NeMo Relay integration."""

from __future__ import annotations

import re
import shlex
import unittest
from pathlib import Path

import tomllib

EXAMPLE_DIR = Path(__file__).resolve().parents[1]
HERMES_DIR = EXAMPLE_DIR / "agents" / "hermes"
PHOENIX_ENDPOINT = "http://host.openshell.internal:6006/v1/traces"


def docker_arg(dockerfile: str, name: str) -> str:
    match = re.search(rf"^ARG {re.escape(name)}=(.+)$", dockerfile, re.MULTILINE)
    if match is None:
        raise AssertionError(f"Dockerfile does not define ARG {name}")
    return match.group(1).strip()


class NativeRelayRecipeContractTest(unittest.TestCase):
    def test_native_relay_runtime_and_configuration(self) -> None:
        dockerfile = (HERMES_DIR / "Dockerfile").read_text(encoding="utf-8")
        generator = (HERMES_DIR / "generate-config.ts").read_text(encoding="utf-8")
        start = (HERMES_DIR / "start.sh").read_text(encoding="utf-8")

        self.assertEqual(
            docker_arg(dockerfile, "HERMES_COMMIT"),
            "a1bfbccc02d5bfdaef1568facfca2cc1456c59f0",
        )
        self.assertEqual(docker_arg(dockerfile, "HERMES_SEMVER"), "0.20.0")
        self.assertIn('"nemo-relay":"0.7.2"', dockerfile)

        lock_patch = (HERMES_DIR / "nemo-relay" / "hermes-relay-lock.patch").read_text(
            encoding="utf-8"
        )
        self.assertEqual(lock_patch.count("diff --git "), 1)
        self.assertIn("diff --git a/uv.lock b/uv.lock", lock_patch)
        self.assertNotIn("pyproject.toml", lock_patch)
        self.assertNotIn("cryptography", lock_patch)

        extras = shlex.split(docker_arg(dockerfile, "HERMES_UV_EXTRAS"))
        self.assertNotIn("nemo-relay", extras)
        self.assertNotRegex(dockerfile, r"--extra(?:=|\s+)['\"]?nemo[-_]relay\b")
        active_start = "\n".join(
            line for line in start.splitlines() if not line.lstrip().startswith("#")
        )
        self.assertNotRegex(
            active_start,
            r"(?m)^\s*(?:exec\s+)?(?:/\S*/)?nemo[-_]relay(?:\s|$)",
        )
        relay_code = [
            str(path.relative_to(HERMES_DIR))
            for path in (HERMES_DIR / "nemo-relay").rglob("*")
            if path.is_file() and path.suffix in {".js", ".py", ".sh", ".ts"}
        ]
        self.assertEqual(relay_code, [], "recipe must not ship a custom Relay runtime")
        shim = (HERMES_DIR / "nemo-relay" / "hermes-cli-shim").read_text(
            encoding="utf-8"
        )
        active_shim = [
            line.strip()
            for line in shim.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertEqual(active_shim[-1], 'exec /usr/local/bin/hermes "$@"')
        self.assertNotRegex("\n".join(active_shim), r"\bfinali[sz](?:e|er|ation)\b")
        self.assertNotRegex(generator, r"(?m)^\s*hooks\s*:")
        self.assertRegex(
            generator,
            r'enabled\s*:\s*\[\s*"observability/nemo_relay"\s*\]',
        )
        self.assertIn("export HERMES_NEMO_RELAY_ATIF_ENABLED=1", start)
        self.assertIn(
            "export HERMES_NEMO_RELAY_ATIF_OUTPUT_DIRECTORY=/sandbox/.hermes-data/atif",
            start,
        )
        self.assertIn("financial-assistant-atif-{session_id}.json", start)

        template = (HERMES_DIR / "nemo-relay" / "plugins.toml.in").read_text(
            encoding="utf-8"
        )
        rendered = (
            template.replace("@@PHOENIX_ENABLED@@", "true")
            .replace("@@PHOENIX_ENDPOINT@@", PHOENIX_ENDPOINT)
            .replace("@@PHOENIX_PROJECT_NAME@@", "financial-assistant")
        )
        config = tomllib.loads(rendered)
        self.assertEqual(config["version"], 1)
        components = {
            component["kind"]: component for component in config["components"]
        }

        observability = components["observability"]
        self.assertIs(observability["enabled"], True)
        self.assertEqual(observability["config"]["version"], 3)
        self.assertNotIn(
            "atif",
            observability["config"],
            "ATIF must use Hermes's per-session native subscriber",
        )
        otel = observability["config"]["opentelemetry"]
        self.assertIs(otel["enabled"], True)
        self.assertEqual(len(otel["endpoints"]), 1)
        self.assertEqual(otel["endpoints"][0]["endpoint"], PHOENIX_ENDPOINT)
        self.assertEqual(otel["endpoints"][0]["type"], "openinference")
        self.assertEqual(otel["endpoints"][0]["transport"], "http_binary")

        redaction = components["pii_redaction"]
        self.assertIs(redaction["enabled"], True)
        self.assertTrue(
            any(
                profile.get("builtin", {}).get("action") == "regex_replace"
                for profile in redaction["config"]["profiles"]
            )
        )
        adaptive = components["adaptive"]
        self.assertIs(adaptive["enabled"], True)
        self.assertEqual(adaptive["config"]["tool_parallelism"]["mode"], "observe_only")

    def test_phoenix_network_boundary(self) -> None:
        compose = (EXAMPLE_DIR / "observability" / "phoenix-compose.yml").read_text(
            encoding="utf-8"
        )
        port_bindings = re.findall(
            r"(?m)^\s*-\s*['\"]?((?:[^:'\"\s#]+:)?\d+:\d+)['\"]?\s*(?:#.*)?$",
            compose,
        )
        self.assertTrue(port_bindings, "Phoenix Compose must publish a host port")
        self.assertTrue(
            all(binding.startswith("127.0.0.1:") for binding in port_bindings),
            f"Phoenix ports must bind to IPv4 loopback: {port_bindings}",
        )
        self.assertIn("127.0.0.1:6006:6006", port_bindings)

        policy = (EXAMPLE_DIR / "policy.yaml").read_text(encoding="utf-8")
        lines = policy.splitlines()
        host_indexes = [
            index
            for index, line in enumerate(lines)
            if re.fullmatch(r"\s*-\s*host:\s*host\.openshell\.internal\s*", line)
        ]
        self.assertEqual(
            len(host_indexes), 1, "policy must define one Phoenix host route"
        )
        start_index = host_indexes[0]
        host_indent = len(lines[start_index]) - len(lines[start_index].lstrip())
        endpoint_lines: list[str] = []
        for line in lines[start_index:]:
            stripped = line.strip()
            indent = len(line) - len(line.lstrip())
            leaves_endpoint = (
                endpoint_lines
                and stripped
                and not stripped.startswith("#")
                and indent < host_indent
            )
            if leaves_endpoint:
                break
            endpoint_lines.append(line)
        endpoint = "\n".join(endpoint_lines)
        self.assertRegex(endpoint, r"(?m)^\s*port:\s*6006\s*$")
        self.assertRegex(endpoint, r"(?m)^\s*protocol:\s*rest\s*$")
        self.assertRegex(endpoint, r"\bmethod:\s*POST\b")
        self.assertEqual(
            re.findall(r"\bpath:\s*['\"]?([^'\"\s},#]+)", endpoint),
            ["/v1/traces"],
        )


if __name__ == "__main__":
    unittest.main()
