# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Export the API's wire contracts as JSON Schema.

``scripts/gen-contracts.sh`` runs this and then turns the schemas into TypeScript::

    python -m demo_api.contracts CONTRACTS_DIR

Schemas describe what the API sends (serialization mode), so every field with a
default is required.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter
from pydantic.json_schema import GenerateJsonSchema

from demo_api.benchmark import Benchmark
from demo_api.benchmark import RetrievalBenchmark
from demo_api.events import ExecutionEventV2
from demo_api.pack import PackView
from demo_api.receipts import ReceiptV2

JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"

# Schema file stem -> (root type name, model).
SCHEMAS: dict[str, tuple[str, Any]] = {
    "execution-event": ("ExecutionEventV2", ExecutionEventV2),
    "receipt": ("ReceiptV2", ReceiptV2),
    "benchmark": ("Benchmark", Benchmark),
    "retrieval-benchmark": ("RetrievalBenchmark", RetrievalBenchmark),
    "pack": ("PackView", PackView),
}


class _ContractSchema(GenerateJsonSchema):
    """Give titles to models only, so the generated TypeScript has no alias per field.

    ``TypedDict`` keys stay optional unless declared required; the models' "every
    default is sent" rule does not apply to them.
    """

    def field_title_should_be_set(self, schema: Any) -> bool:
        return False

    def field_is_required(self, field: Any, total: bool) -> bool:
        if field["type"] == "typed-dict-field":
            return field.get("required", total)
        return super().field_is_required(field, total)


def json_schema(title: str, model: Any) -> dict[str, Any]:
    schema = TypeAdapter(model).json_schema(mode="serialization", schema_generator=_ContractSchema)
    return {"$schema": JSON_SCHEMA_DIALECT, **schema, "title": title}


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def export(contracts_dir: Path) -> None:
    schemas_dir = contracts_dir / "schemas"
    schemas_dir.mkdir(parents=True, exist_ok=True)
    for stem, (title, model) in SCHEMAS.items():
        write_json(schemas_dir / f"{stem}.schema.json", json_schema(title, model))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("contracts_dir", type=Path, help="directory to write schemas/ into")
    export(parser.parse_args().contracts_dir)


if __name__ == "__main__":
    main()
