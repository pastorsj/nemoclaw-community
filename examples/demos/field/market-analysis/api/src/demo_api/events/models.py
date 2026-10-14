# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared value contracts for durable execution records.

These primitives deliberately validate shape and safety, not research intent. New
tools and capabilities can use any bounded identifier without changing this module.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import UTC
from datetime import datetime
from typing import Annotated
from typing import Any
from typing import TypeAlias

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import JsonValue
from pydantic import StringConstraints
from pydantic.alias_generators import to_camel


class ContractModel(BaseModel):
    """Strict, immutable base with one camel-case wire convention."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        frozen=True,
        json_schema_serialization_defaults_required=True,
        serialize_by_alias=True,
        validate_by_alias=True,
        validate_by_name=True,
    )


OpenIdentifier: TypeAlias = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$",
    ),
]
CorrelationIdentifier: TypeAlias = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=256,
        pattern=r"^[^\s\x00-\x1f\x7f]+$",
    ),
]
ContentDigest: TypeAlias = Annotated[
    str,
    StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$"),
]

MAX_JSON_DEPTH = 6
MAX_JSON_ITEMS = 100
MAX_JSON_NODES = 2_000
MAX_JSON_TEXT_CHARS = 32_000

_PROHIBITED_DISPLAY_KEY = re.compile(
    r"(?:thought|reasoning|prompt|embedding|password|passwd|secret|token|credential|api[_-]?key|"
    r"authorization|cookie|connection(?:_string)?|private[_-]?key|access[_-]?key|"
    r"filesystem|file[_-]?path|storage[_-]?uri|dsn)",
    re.IGNORECASE,
)
_SAFE_USAGE_KEYS = {
    "cache_write_tokens",
    "cached_input_tokens",
    "input_tokens",
    "max_output_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
    "token_usage",
}
_SAFE_NONNEGATIVE_METRIC_KEYS = {"query_embedding_ms"}
_UNSAFE_CONTROL_CHARACTER = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def normalize_aware_datetime(value: datetime, *, field_name: str) -> datetime:
    """Require timezone-aware timestamps and normalize them to UTC."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return value.astimezone(UTC)


def validate_display_text(value: str, *, field_name: str, max_chars: int) -> str:
    """Validate bounded text that is safe to place in a browser projection."""

    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field_name} must not be blank")
    if len(stripped) > max_chars:
        raise ValueError(f"{field_name} exceeds {max_chars} characters")
    if _UNSAFE_CONTROL_CHARACTER.search(stripped):
        raise ValueError(f"{field_name} contains an unsafe control character")
    return stripped


def validate_bounded_display_json(value: JsonValue, *, field_name: str) -> JsonValue:
    """Reject unbounded or obviously private data in display-safe JSON.

    Specialized receipt adapters remain responsible for allow-listing their actual
    result fields. This validator is the final generic boundary: it prevents a future
    adapter from accidentally copying private prompt/reasoning/credential fields into
    the browser contract and caps payload complexity.
    """

    visited_nodes = 0

    def walk(item: Any, depth: int, path: str) -> None:
        nonlocal visited_nodes
        visited_nodes += 1
        if visited_nodes > MAX_JSON_NODES:
            raise ValueError(f"{field_name} exceeds {MAX_JSON_NODES} JSON nodes")
        if depth > MAX_JSON_DEPTH:
            raise ValueError(f"{field_name} exceeds nesting depth {MAX_JSON_DEPTH}")

        if item is None or isinstance(item, bool | int):
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError(f"{path} contains a non-finite number")
            return
        if isinstance(item, str):
            if len(item) > MAX_JSON_TEXT_CHARS:
                raise ValueError(f"{path} exceeds {MAX_JSON_TEXT_CHARS} characters")
            if _UNSAFE_CONTROL_CHARACTER.search(item):
                raise ValueError(f"{path} contains an unsafe control character")
            return
        if isinstance(item, Mapping):
            if len(item) > MAX_JSON_ITEMS:
                raise ValueError(f"{path} exceeds {MAX_JSON_ITEMS} object fields")
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValueError(f"{path} contains a non-string object key")
                if not key or len(key) > 128:
                    raise ValueError(f"{path} contains an invalid object key")
                normalized_key = key.casefold()
                if normalized_key in _SAFE_NONNEGATIVE_METRIC_KEYS:
                    is_nonnegative_integer = isinstance(child, int) and not isinstance(child, bool) and child >= 0
                    is_nonnegative_finite_float = isinstance(child, float) and math.isfinite(child) and child >= 0
                    if not (is_nonnegative_integer or is_nonnegative_finite_float):
                        raise ValueError(f"{path}.{key} must be a finite, non-negative number")
                elif normalized_key not in _SAFE_USAGE_KEYS and _PROHIBITED_DISPLAY_KEY.search(key):
                    raise ValueError(f"{path}.{key} is not permitted in display-safe JSON")
                walk(child, depth + 1, f"{path}.{key}")
            return
        if isinstance(item, list | tuple):
            if len(item) > MAX_JSON_ITEMS:
                raise ValueError(f"{path} exceeds {MAX_JSON_ITEMS} array items")
            for index, child in enumerate(item):
                walk(child, depth + 1, f"{path}[{index}]")
            return
        raise ValueError(f"{path} contains a non-JSON value")

    walk(value, 0, field_name)
    return value


def require_unique(values: tuple[str, ...], *, field_name: str) -> tuple[str, ...]:
    """Require stable references to be unique while preserving their order."""

    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must not contain duplicates")
    return values


__all__ = [
    "MAX_JSON_DEPTH",
    "MAX_JSON_ITEMS",
    "MAX_JSON_NODES",
    "MAX_JSON_TEXT_CHARS",
    "ContractModel",
    "ContentDigest",
    "CorrelationIdentifier",
    "JsonValue",
    "OpenIdentifier",
    "normalize_aware_datetime",
    "require_unique",
    "validate_bounded_display_json",
    "validate_display_text",
]
