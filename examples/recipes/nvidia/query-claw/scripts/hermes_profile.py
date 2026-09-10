#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Apply and safely restore Query Claw's narrow Hermes API profile.

The Query Claw skills and MCP registrations use NemoClaw's native lifecycle.
This helper owns only the small set of Hermes settings that limits API chats to
Query Claw's tools. Setup runs it inside the sandbox with Hermes' public atomic
configuration writer.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
from copy import deepcopy
from typing import Any

from hermes_cli.config import atomic_config_write, get_config_path
from hermes_cli.tools_config import _get_platform_tools, _get_plugin_toolset_keys
from utils import fast_safe_load


TARGET_TOOLSETS = [
    "code_execution",
    "skills",
    "query-claw",
]
TARGET_TOOL_SEARCH = {
    "enabled": "off",
    "search_default_limit": 5,
    "max_search_limit": 20,
}
TARGET_SYSTEM_PROMPT = """You are Query Claw, an evidence-led enterprise data agent.
Use only the installed Query Claw skills and configured records, documents, and
predictions routes. Load `query-claw` first, then only the specialist skills the
request needs. When calling `skill_view`, pass one exact bare skill name from
`skills_list`: use `{"skill_name":"query-claw"}`, never
`{"skill_name":"query-claw:query-claw"}`. Call `check_readiness` before the
first data query to discover only this deployment's active datasets and views.
Every data-bearing call (`check_answerable`, `ask_question`, `query`, or
`predict`) must include one exact `dataset_id`. If multiple active datasets can
answer an ambiguous request, ask the operator which one to use. Never infer or
enumerate hidden datasets from a tool error. Determine the evidence view the
request requires before choosing a tool, intersect it with the visible views,
and abstain when it is unavailable; never substitute another available view.
If trusted invocation context supplies a `scope_token`, forward it unchanged to
`check_readiness` and every data-bearing Query Claw call. Treat it as a credential: never print,
repeat, summarize, or place it in an answer, error, calculation, or diagnostic.
Start with one call to each selected data tool and make at most one corrective
retry. Preserve stable IDs and distinguish observed, retrieved, predicted, and
calculated evidence. Predictions run through NVIDIA Ontology and its governed
Kumo integration; never call Kumo directly or construct PQL. If the configured
sources cannot support a claim, say so rather than guessing. If a read-only
Query Claw data tool reports a transient transport error, retry that same tool
once. Split multi-view requests into complete route-specific subquestions.
For structured-record tools, preserve every applicable field, value, date,
population, ranking, and other filter, but omit requests for other views. For
requested arithmetic, use only numeric assignments and one `print` expression;
never use imports, comments, strings, containers, loops, or functions.
"""
SNAPSHOT_PATHS = (
    ("platform_toolsets", "api_server"),
    ("code_execution", "mode"),
    ("skills", "write_approval"),
    ("skills", "guard_agent_created"),
    ("agent", "disabled_toolsets"),
    ("agent", "system_prompt"),
    ("tools", "tool_search"),
    ("known_plugin_toolsets", "api_server"),
)


def nested_get(data: dict[str, Any], path: tuple[str, ...]) -> tuple[bool, Any]:
    current: Any = data
    for part in path:
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, deepcopy(current)


def nested_set(data: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    current = data
    for part in path[:-1]:
        child = current.get(part)
        if child is None:
            child = {}
            current[part] = child
        if not isinstance(child, dict):
            raise ValueError(f"configuration field {part!r} must be a mapping")
        current = child
    current[path[-1]] = deepcopy(value)


def nested_delete(data: dict[str, Any], path: tuple[str, ...]) -> None:
    current: Any = data
    parents: list[tuple[dict[str, Any], str]] = []
    for part in path[:-1]:
        if not isinstance(current, dict) or part not in current:
            return
        parents.append((current, part))
        current = current[part]
    if not isinstance(current, dict):
        return
    current.pop(path[-1], None)
    for parent, key in reversed(parents):
        child = parent.get(key)
        if isinstance(child, dict) and not child:
            parent.pop(key)
        else:
            break


def decode_argument(value: str) -> str:
    try:
        return base64.b64decode(value, validate=True).decode("utf-8")
    except (ValueError, UnicodeError) as exc:
        raise ValueError("invalid base64-encoded configuration argument") from exc


def projection(config: dict[str, Any]) -> dict[str, Any]:
    fields = {}
    for path in SNAPSHOT_PATHS:
        present, value = nested_get(config, path)
        fields[".".join(path)] = {"present": present, "value": value}
    return fields


def projection_fingerprint(fields: dict[str, Any]) -> str:
    payload = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def projection_state(config: dict[str, Any]) -> dict[str, Any]:
    fields = projection(config)
    return {"fields": fields, "sha256": projection_fingerprint(fields)}


def validate_projection_state(state: Any) -> dict[str, Any]:
    expected = {".".join(path) for path in SNAPSHOT_PATHS}
    if not isinstance(state, dict) or set(state) != {"fields", "sha256"}:
        raise ValueError("Query Claw Hermes profile receipt is malformed")
    fields = state["fields"]
    if not isinstance(fields, dict) or set(fields) != expected:
        raise ValueError("Query Claw Hermes profile receipt has an invalid projection")
    for field in fields.values():
        if not isinstance(field, dict) or set(field) != {"present", "value"}:
            raise ValueError("Query Claw Hermes profile receipt is malformed")
        if not isinstance(field["present"], bool):
            raise ValueError("profile receipt presence markers must be booleans")
        if field["present"] is False and field["value"] is not None:
            raise ValueError("absent profile receipt fields must have a null value")
    if state["sha256"] != projection_fingerprint(fields):
        raise ValueError("Query Claw Hermes profile receipt fingerprint differs")
    return deepcopy(state)


def decode_receipt(encoded: str) -> dict[str, Any]:
    try:
        document = json.loads(decode_argument(encoded))
    except json.JSONDecodeError as exc:
        raise ValueError("Query Claw Hermes profile receipt is not valid JSON") from exc
    if not isinstance(document, dict) or document.get("schema_version") != 2:
        raise ValueError("Query Claw Hermes profile receipt has an invalid schema")
    required = {"schema_version", "before", "applied"}
    if set(document) not in (required, required | {"previous_applied"}):
        raise ValueError("Query Claw Hermes profile receipt is malformed")
    receipt = {
        "schema_version": 2,
        "before": validate_projection_state(document["before"]),
        "applied": validate_projection_state(document["applied"]),
    }
    if "previous_applied" in document:
        receipt["previous_applied"] = validate_projection_state(
            document["previous_applied"]
        )
    return receipt


def states_equal(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return left["sha256"] == right["sha256"] and left["fields"] == right["fields"]


def restore_fields(config: dict[str, Any], fields: dict[str, Any]) -> None:
    for path in SNAPSHOT_PATHS:
        field = fields[".".join(path)]
        if field["present"]:
            nested_set(config, path, field["value"])
        else:
            nested_delete(config, path)


def mutate_profile(config: dict[str, Any]) -> None:
    nested_set(config, ("platform_toolsets", "api_server"), TARGET_TOOLSETS)
    nested_set(
        config,
        ("known_plugin_toolsets", "api_server"),
        sorted(_get_plugin_toolset_keys()),
    )
    nested_set(config, ("code_execution", "mode"), "strict")
    # Hermes groups read-only skill discovery and persistent skill mutation in
    # one toolset. Keep skill loading available, but stage every write for an
    # operator and scan agent-authored content before it can be approved.
    nested_set(config, ("skills", "write_approval"), True)
    nested_set(config, ("skills", "guard_agent_created"), True)
    nested_set(config, ("tools", "tool_search"), TARGET_TOOL_SEARCH)
    nested_set(config, ("agent", "system_prompt"), TARGET_SYSTEM_PROMPT)
    present, disabled = nested_get(config, ("agent", "disabled_toolsets"))
    if present and not isinstance(disabled, list):
        raise ValueError("agent.disabled_toolsets must be a list")
    remaining = set(disabled if present else []) - set(TARGET_TOOLSETS)
    nested_set(config, ("agent", "disabled_toolsets"), sorted(remaining))
    # Hermes can recover platform-native and newly installed plugin toolsets
    # beyond the explicit list. Resolve the live catalog, then suppress every
    # recovered toolset outside Query Claw's intentionally narrow API surface.
    remaining.update(_get_platform_tools(config, "api_server") - set(TARGET_TOOLSETS))
    nested_set(config, ("agent", "disabled_toolsets"), sorted(remaining))


def desired_from(
    config: dict[str, Any], before: dict[str, Any] | None = None
) -> dict[str, Any]:
    candidate = deepcopy(config)
    if before is not None:
        # Rebase every retry on the original operator state.
        restore_fields(candidate, before["fields"])
    mutate_profile(candidate)
    return candidate


def snapshot(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "before": projection_state(config),
        "applied": projection_state(desired_from(config)),
    }


def receipt_matches_current(receipt: dict[str, Any], current: dict[str, Any]) -> bool:
    return any(
        states_equal(current, receipt[name])
        for name in ("before", "applied", "previous_applied")
        if name in receipt
    )


def prepare(config: dict[str, Any], encoded_receipt: str) -> dict[str, Any]:
    receipt = decode_receipt(encoded_receipt)
    current = projection_state(config)
    if not receipt_matches_current(receipt, current):
        raise ValueError(
            "Hermes API profile drifted after Query Claw recorded its receipt; "
            "refusing to overwrite operator changes"
        )
    candidate = desired_from(config, receipt["before"])
    return {
        "schema_version": 2,
        "before": receipt["before"],
        "applied": projection_state(candidate),
        "previous_applied": current,
    }


def apply(config: dict[str, Any], encoded_receipt: str) -> bool:
    receipt = decode_receipt(encoded_receipt)
    current = projection_state(config)
    if not receipt_matches_current(receipt, current):
        raise ValueError(
            "Hermes API profile drifted after Query Claw recorded its receipt; "
            "refusing to overwrite operator changes"
        )
    candidate = desired_from(config, receipt["before"])
    if not states_equal(projection_state(candidate), receipt["applied"]):
        raise ValueError("the prepared Hermes API projection changed before apply")
    if candidate == config:
        return False
    atomic_config_write(get_config_path(), candidate)
    return True


def restore_needed(config: dict[str, Any], encoded: str) -> tuple[bool, dict[str, Any]]:
    receipt = decode_receipt(encoded)
    current = projection_state(config)
    if states_equal(current, receipt["before"]):
        return False, receipt
    restorable = states_equal(current, receipt["applied"]) or (
        "previous_applied" in receipt
        and states_equal(current, receipt["previous_applied"])
    )
    if not restorable:
        raise ValueError(
            "Hermes API profile no longer matches Query Claw's applied receipt; "
            "refusing to overwrite operator changes"
        )
    return True, receipt


def restore(config: dict[str, Any], encoded: str) -> bool:
    needed, receipt = restore_needed(config, encoded)
    if not needed:
        return False
    restored = deepcopy(config)
    restore_fields(restored, receipt["before"]["fields"])
    atomic_config_write(get_config_path(), restored)
    return True


def verify(config: dict[str, Any], encoded: str) -> None:
    receipt = decode_receipt(encoded)
    if not states_equal(projection_state(config), receipt["applied"]):
        raise ValueError("live Hermes API profile differs from its Query Claw receipt")
    effective = _get_platform_tools(config, "api_server")
    if effective != set(TARGET_TOOLSETS):
        raise ValueError(f"effective api_server toolsets differ: {sorted(effective)!r}")


def read_strict_config() -> dict[str, Any]:
    path = get_config_path()
    try:
        with path.open(encoding="utf-8") as handle:
            config = fast_safe_load(handle)
    except FileNotFoundError:
        return {}
    except Exception as exc:
        raise ValueError("existing Hermes config is not valid YAML") from exc
    if not isinstance(config, dict):
        raise ValueError("existing Hermes config must be a YAML mapping")
    return config


def main() -> int:
    if len(sys.argv) < 2:
        raise ValueError(
            "expected snapshot, prepare, apply, verify, restore-check, or restore"
        )
    action = sys.argv[1]
    config = read_strict_config()
    if action == "snapshot" and len(sys.argv) == 2:
        result: object = snapshot(config)
    elif action == "prepare" and len(sys.argv) == 3:
        result = prepare(config, sys.argv[2])
    elif action == "apply" and len(sys.argv) == 3:
        result = "changed" if apply(config, sys.argv[2]) else "unchanged"
    elif action == "verify" and len(sys.argv) == 3:
        verify(config, sys.argv[2])
        result = "verified"
    elif action == "restore-check" and len(sys.argv) == 3:
        needed, _ = restore_needed(config, sys.argv[2])
        result = "restore-needed" if needed else "already-restored"
    elif action == "restore" and len(sys.argv) == 3:
        result = "restored" if restore(config, sys.argv[2]) else "already-restored"
    else:
        raise ValueError(f"invalid arguments for Hermes profile action {action!r}")
    if isinstance(result, dict):
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    else:
        print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
