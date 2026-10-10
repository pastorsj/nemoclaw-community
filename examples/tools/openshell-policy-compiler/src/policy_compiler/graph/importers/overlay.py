# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Apply an OpenAPI Overlay 1.0.0 document to an OpenAPI description.

An overlay lets you annotate an API description you do not own, for example an
internal service's spec, without editing it. The policy compiler uses overlays
to add its ``x-openshell-*`` fields (privilege levels, private address ranges,
inspection mode) to specs that lack them.

Semantics follow https://spec.openapis.org/overlay/v1.0.0.html:

* The overlay must have an ``overlay`` version string, an ``info`` object, and
  an ``actions`` list. Actions apply in order, each to the result of the
  previous one. An action with a key this module does not know, such as
  Overlay 1.1's ``copy``, is skipped with a warning.
* ``remove: true`` removes every selected object or array from the object or
  array that contains it. ``update`` is then ignored. Primitive values cannot be
  selected; select the object that contains them.
* ``update`` on a selected object is merged recursively: a primitive replaces a
  primitive, objects merge, and new properties are added. Overlay 1.0.0 does
  not say what happens to an array property inside the update; this module
  concatenates it, which is the rule Overlay 1.1.0 later wrote down. To replace
  an array, remove it first and then add it again.
* ``update`` on a selected array appends the update value as one entry.

Only a subset of JSONPath is supported: ``$``, ``.name``, ``['name']``,
``["name"]``, ``.*``, ``[*]``, ``[n]``, and the filter ``[?(@.key == 'value')]``
(parentheses optional, ``@.a.b`` paths allowed, string, number, boolean, or null
literals). Anything else raises ``ValueError``.
"""

from __future__ import annotations

import json
import re
from typing import Any

from policy_compiler.ir.access import Diagnostic

_NAME = re.compile(r"[^.\[\]\s'\"]+")
_INDEX = re.compile(r"\[\s*(-?\d+)\s*\]")
_FILTER = re.compile(
    r"""\[\?\s*(?P<open>\()?\s*
        @(?P<key>(?:\.[A-Za-z0-9_$-]+)+)\s*
        ==\s*
        (?P<value>'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?|true|false|null)\s*
        (?(open)\))\s*\]""",
    re.VERBOSE,
)
_QUOTED = re.compile(r"""\[\s*(?P<quoted>'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")\s*\]""")
_WILDCARD = re.compile(r"\[\s*\*\s*\]")

_ACTION_KEYS = frozenset({"target", "description", "update", "remove"})

_Step = tuple
"""One parsed selector: ``("name", str)``, ``("wild",)``, ``("index", int)``, or ``("filter", keys, value)``."""


def apply_overlay(spec: dict, overlay: dict, *, diagnostics: list[Diagnostic] | None = None) -> dict:
    """Return a copy of ``spec`` with the overlay's actions applied. The inputs are not changed.

    Problems that do not make the overlay unusable (a target that selects
    nothing, an update of the wrong shape) are appended to ``diagnostics`` as
    warnings when a list is given. A malformed overlay raises ``ValueError``.
    """
    if not isinstance(overlay, dict):
        raise ValueError("an overlay must be a mapping")
    if not isinstance(overlay.get("overlay"), str):
        raise ValueError("an overlay needs an 'overlay' version string, for example '1.0.0'")
    if not isinstance(overlay.get("info"), dict):
        raise ValueError("an overlay needs an 'info' object")
    actions = overlay.get("actions")
    if not isinstance(actions, list):
        raise ValueError("an overlay needs an 'actions' list")
    sink: list[Diagnostic] = diagnostics if diagnostics is not None else []
    result = _tree_copy(spec)
    for index, action in enumerate(actions):
        pointer = f"actions[{index}]"
        if not isinstance(action, dict) or not isinstance(action.get("target"), str):
            raise ValueError(f"overlay {pointer} needs a string 'target'")
        target = action["target"]
        steps = parse_jsonpath(target)
        unsupported = sorted(str(key) for key in action if key not in _ACTION_KEYS and not str(key).startswith("x-"))
        if unsupported:
            message = (
                f"overlay action for {target} uses unsupported key(s) {', '.join(unsupported)}; "
                "only Overlay 1.0.0 update and remove are applied, so the action was skipped"
            )
            sink.append(Diagnostic("warning", "overlay-unsupported-action", message, pointer))
            continue
        matches = _select(result, steps)
        if not matches:
            sink.append(Diagnostic("warning", "overlay-no-match", f"overlay target {target} selects nothing", pointer))
            continue
        if action.get("remove") is True:
            _remove(matches, pointer, sink)
        elif "update" in action:
            _update(matches, action["update"], target, pointer, sink)
        else:
            sink.append(
                Diagnostic(
                    "warning", "overlay-empty-action", f"overlay action for {target} has no update or remove", pointer
                )
            )
    return result


def parse_jsonpath(expression: str) -> list[_Step]:
    """Parse the supported JSONPath subset into a list of steps."""
    if not isinstance(expression, str) or not expression.startswith("$"):
        raise ValueError(f"JSONPath must start with '$': {expression!r}")
    steps: list[_Step] = []
    position = 1
    while position < len(expression):
        if expression.startswith("..", position):
            raise ValueError(f"recursive descent is not supported: {expression!r}")
        if expression.startswith(".*", position):
            steps.append(("wild",))
            position += 2
            continue
        if expression[position] == ".":
            match = _NAME.match(expression, position + 1)
            if not match:
                raise ValueError(f"expected a name at position {position + 1} in {expression!r}")
            steps.append(("name", match.group(0)))
            position = match.end()
            continue
        for pattern in (_QUOTED, _WILDCARD, _INDEX, _FILTER):
            match = pattern.match(expression, position)
            if match:
                break
        else:
            raise ValueError(f"unsupported JSONPath syntax at position {position} in {expression!r}")
        if pattern is _QUOTED:
            steps.append(("name", _unquote(match.group("quoted"))))
        elif pattern is _WILDCARD:
            steps.append(("wild",))
        elif pattern is _INDEX:
            steps.append(("index", int(match.group(1))))
        else:
            keys = tuple(match.group("key").split(".")[1:])
            steps.append(("filter", keys, _literal(match.group("value"))))
        position = match.end()
    return steps


def _tree_copy(value: Any) -> Any:
    """Copy mappings and lists without keeping shared references.

    ``copy.deepcopy`` preserves sharing, so a YAML anchor used in two places
    would still be one object, and an update to one place would change both.
    Overlays treat the document as a tree, so every place gets its own copy.
    """
    if isinstance(value, dict):
        return {key: _tree_copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_tree_copy(item) for item in value]
    return value


def _unquote(token: str) -> str:
    return re.sub(r"\\(.)", r"\1", token[1:-1])


def _literal(token: str) -> Any:
    if token[0] in "'\"":
        return _unquote(token)
    return json.loads(token)


# A match is (container, key, node). The root has no container.
_Match = tuple[Any, Any, Any]


def _children(node: Any) -> list[_Match]:
    if isinstance(node, dict):
        return [(node, key, value) for key, value in node.items()]
    if isinstance(node, list):
        return [(node, index, value) for index, value in enumerate(node)]
    return []


def _select(root: Any, steps: list[_Step]) -> list[_Match]:
    current: list[_Match] = [(None, None, root)]
    for step in steps:
        selected: list[_Match] = []
        for _, _, node in current:
            kind = step[0]
            if kind == "name":
                if isinstance(node, dict) and step[1] in node:
                    selected.append((node, step[1], node[step[1]]))
            elif kind == "wild":
                selected.extend(_children(node))
            elif kind == "index":
                if isinstance(node, list) and -len(node) <= step[1] < len(node):
                    index = step[1] % len(node)
                    selected.append((node, index, node[index]))
            else:
                selected.extend(child for child in _children(node) if _filter_matches(child[2], step[1], step[2]))
        current = selected
    return current


def _filter_matches(node: Any, keys: tuple[str, ...], expected: Any) -> bool:
    value: Any = node
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            return False
        value = value[key]
    if isinstance(value, bool) or isinstance(expected, bool):
        return isinstance(value, bool) and isinstance(expected, bool) and value == expected
    return value == expected


def _update(matches: list[_Match], update: Any, target: str, pointer: str, sink: list[Diagnostic]) -> None:
    for _, _, node in matches:
        if isinstance(node, dict):
            if not isinstance(update, dict):
                sink.append(
                    Diagnostic(
                        "warning",
                        "overlay-invalid-update",
                        f"overlay target {target} selects an object, so its update must be an object",
                        pointer,
                    )
                )
                continue
            _merge(node, update)
        elif isinstance(node, list):
            node.append(_tree_copy(update))
        else:
            sink.append(
                Diagnostic(
                    "warning",
                    "overlay-invalid-target",
                    f"overlay target {target} selects a primitive value; select the object that contains it",
                    pointer,
                )
            )


def _merge(target: dict, update: dict) -> None:
    for key, value in update.items():
        current = target.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            _merge(current, value)
        elif isinstance(current, list) and isinstance(value, list):
            current.extend(_tree_copy(value))
        else:
            target[key] = _tree_copy(value)


def _remove(matches: list[_Match], pointer: str, sink: list[Diagnostic]) -> None:
    list_indices: dict[int, tuple[list, set[int]]] = {}
    for container, key, node in matches:
        if container is None:
            sink.append(Diagnostic("warning", "overlay-invalid-target", "overlay cannot remove the root ($)", pointer))
        elif not isinstance(node, (dict, list)):
            message = "overlay remove selects a primitive value; select the object or array that contains it"
            sink.append(Diagnostic("warning", "overlay-invalid-target", message, pointer))
        elif isinstance(container, dict):
            container.pop(key, None)
        else:
            list_indices.setdefault(id(container), (container, set()))[1].add(key)
    # Delete from the end so earlier indices stay valid.
    for container, indices in list_indices.values():
        for index in sorted(indices, reverse=True):
            del container[index]
