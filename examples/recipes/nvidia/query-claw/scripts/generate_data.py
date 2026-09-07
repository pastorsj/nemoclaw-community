#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Generate and validate Query Claw's deterministic synthetic data pack."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable


EXAMPLE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SPEC = EXAMPLE_ROOT / "data" / "supply-chain.json"
DEFAULT_OUTPUT = EXAMPLE_ROOT / ".runtime" / "data"


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _write_csv(
    path: Path, fieldnames: list[str], rows: Iterable[dict[str, Any]]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _prepare_output(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    service = output / "service"
    leaves = (service / "structured", service / "documents", output / "evaluation")
    for target in (service, *leaves):
        if target.is_symlink():
            raise ValueError(f"refusing to replace generated symlink: {target}")
        if target.exists() and not target.is_dir():
            raise ValueError(f"generated output path is not a directory: {target}")
        target.mkdir(parents=True, exist_ok=True)
    if set(service.iterdir()) != {service / "structured", service / "documents"}:
        raise ValueError(
            f"generated service directory has unexpected entries: {service}"
        )
    for target in leaves:
        for child in target.iterdir():
            if child.is_symlink() or not child.is_file():
                raise ValueError(
                    f"refusing to replace unexpected generated entry: {child}"
                )
            child.unlink()
    manifest = output / "manifest.json"
    if manifest.is_symlink():
        raise ValueError(f"refusing to replace generated symlink: {manifest}")
    manifest.unlink(missing_ok=True)


def _late_probability(
    supplier: dict[str, Any], facility: dict[str, Any], month: int
) -> float:
    seasonal = 0.07 if month in {11, 12, 1} else 0.0
    return min(
        0.85,
        max(
            0.01,
            1.0
            - float(supplier["reliability"])
            + float(facility["lane_risk"])
            + seasonal,
        ),
    )


def _order_amount(product: dict[str, Any], quantity: int) -> int:
    return int(product["unit_price_usd"]) * quantity


def generate(
    spec_path: Path = DEFAULT_SPEC, output: Path = DEFAULT_OUTPUT
) -> dict[str, Any]:
    """Generate the service-visible corpus and separately held evaluation labels."""

    spec = _read_json(spec_path)
    if spec.get("schema_version") != 1:
        raise ValueError("unsupported supply-chain specification version")

    dataset = spec["dataset"]
    rng = random.Random(int(dataset["seed"]))
    start = _parse_date(dataset["start_date"])
    cutoff = _parse_date(dataset["prediction_cutoff"])
    as_of = _parse_date(dataset["as_of_date"])
    horizon = int(dataset["prediction_horizon_days"])
    if as_of != cutoff + timedelta(days=horizon):
        raise ValueError(
            "as_of_date must equal prediction_cutoff plus prediction_horizon_days"
        )

    suppliers = list(spec["suppliers"])
    facilities = list(spec["facilities"])
    products = list(spec["products"])
    if len({row["supplier_id"] for row in suppliers}) != len(suppliers):
        raise ValueError("supplier identifiers must be unique")

    _prepare_output(output)
    structured = output / "service" / "structured"
    documents = output / "service" / "documents"
    evaluation = output / "evaluation"

    supplier_rows = [
        {
            "supplier_id": row["supplier_id"],
            "supplier_name": row["name"],
            "region": row["region"],
            "criticality": row["criticality"],
        }
        for row in suppliers
    ]
    _write_csv(structured / "suppliers.csv", list(supplier_rows[0]), supplier_rows)
    _write_csv(structured / "facilities.csv", list(facilities[0]), facilities)
    _write_csv(structured / "products.csv", list(products[0]), products)

    order_rows: list[dict[str, Any]] = []
    event_rows: list[dict[str, Any]] = []
    outcome_rows: list[dict[str, Any]] = []
    label_rows: list[dict[str, Any]] = []
    history_span = (cutoff - start).days + 1

    for index in range(1, int(dataset["historical_order_count"]) + 1):
        while True:
            supplier = rng.choice(suppliers)
            facility = rng.choice(facilities)
            product = rng.choice(products)
            order_date = start + timedelta(days=rng.randrange(history_span))
            promised = order_date + timedelta(
                days=int(supplier["base_lead_days"]) + rng.randint(-2, 3)
            )
            late = rng.random() < _late_probability(supplier, facility, promised.month)
            delay_days = rng.randint(2, 12) if late else rng.randint(-3, 0)
            delivered = promised + timedelta(days=delay_days)
            if delivered <= cutoff:
                break
        quantity = rng.randint(8, 80)
        order_id = f"PO-H{index:05d}"
        order_rows.append(
            {
                "order_id": order_id,
                "supplier_id": supplier["supplier_id"],
                "product_id": product["product_id"],
                "facility_id": facility["facility_id"],
                "order_date": order_date.isoformat(),
                "promised_date": promised.isoformat(),
                "quantity": quantity,
                "amount_usd": _order_amount(product, quantity),
                "status_at_cutoff": "delivered",
                "split": "history",
            }
        )
        event_rows.extend(
            [
                {
                    "event_id": f"EV-{order_id}-1",
                    "order_id": order_id,
                    "event_date": order_date.isoformat(),
                    "event_type": "order_created",
                    "severity": "info",
                },
                {
                    "event_id": f"EV-{order_id}-2",
                    "order_id": order_id,
                    "event_date": delivered.isoformat(),
                    "event_type": "delivered_late" if late else "delivered_on_time",
                    "severity": "warning" if late else "info",
                },
            ]
        )
        outcome_rows.append(
            {
                "outcome_id": f"OUT-{order_id}",
                "order_id": order_id,
                "outcome_date": delivered.isoformat(),
                "outcome": "late" if late else "on_time",
            }
        )

    weighted_suppliers = [
        supplier
        for supplier in suppliers
        for _ in range(max(1, round((1.05 - float(supplier["reliability"])) * 20)))
    ]
    for index in range(1, int(dataset["evaluation_order_count"]) + 1):
        supplier = rng.choice(weighted_suppliers)
        facility = rng.choice(facilities)
        product = rng.choice(products)
        promised = cutoff + timedelta(days=1 + ((index * 7) % horizon))
        # Evaluation orders must already exist at the prediction cutoff. Their
        # eventual delivery outcomes stay exclusively in evaluation/labels.csv.
        order_date = min(
            promised - timedelta(days=int(supplier["base_lead_days"])),
            cutoff - timedelta(days=1 + (index % 7)),
        )
        late = rng.random() < min(
            0.95, _late_probability(supplier, facility, promised.month) + 0.1
        )
        delay_days = rng.randint(3, 14) if late else rng.randint(-2, 0)
        actual_delivery = promised + timedelta(days=delay_days)
        quantity = rng.randint(30, 120)
        order_id = f"PO-E{index:04d}"
        order_rows.append(
            {
                "order_id": order_id,
                "supplier_id": supplier["supplier_id"],
                "product_id": product["product_id"],
                "facility_id": facility["facility_id"],
                "order_date": order_date.isoformat(),
                "promised_date": promised.isoformat(),
                "quantity": quantity,
                "amount_usd": _order_amount(product, quantity),
                "status_at_cutoff": "in_transit",
                "split": "evaluation",
            }
        )
        signal_type = (
            "supplier_delay_signal"
            if float(supplier["reliability"]) < 0.85
            else "in_transit"
        )
        event_rows.extend(
            [
                {
                    "event_id": f"EV-{order_id}-1",
                    "order_id": order_id,
                    "event_date": order_date.isoformat(),
                    "event_type": "order_created",
                    "severity": "info",
                },
                {
                    "event_id": f"EV-{order_id}-2",
                    "order_id": order_id,
                    "event_date": (cutoff - timedelta(days=index % 5)).isoformat(),
                    "event_type": signal_type,
                    "severity": "warning" if signal_type.endswith("signal") else "info",
                },
            ]
        )
        label_rows.append(
            {
                "order_id": order_id,
                "supplier_id": supplier["supplier_id"],
                "actual_delivery_date": actual_delivery.isoformat(),
                "late": str(late).lower(),
                "late_within_horizon": str(
                    late and cutoff < actual_delivery <= as_of
                ).lower(),
                "delay_days": max(0, delay_days),
            }
        )

    order_fields = [
        "order_id",
        "supplier_id",
        "product_id",
        "facility_id",
        "order_date",
        "promised_date",
        "quantity",
        "amount_usd",
        "status_at_cutoff",
        "split",
    ]
    _write_csv(structured / "purchase_orders.csv", order_fields, order_rows)
    _write_csv(
        structured / "shipment_events.csv",
        ["event_id", "order_id", "event_date", "event_type", "severity"],
        event_rows,
    )
    _write_csv(
        structured / "delivery_outcomes.csv",
        ["outcome_id", "order_id", "outcome_date", "outcome"],
        outcome_rows,
    )
    _write_csv(
        evaluation / "labels.csv",
        [
            "order_id",
            "supplier_id",
            "actual_delivery_date",
            "late",
            "late_within_horizon",
            "delay_days",
        ],
        label_rows,
    )

    documents.mkdir(parents=True, exist_ok=True)
    for supplier in suppliers:
        text = (
            f"# {supplier['name']} supplier notice\n\n"
            f"Supplier ID: `{supplier['supplier_id']}`\n\n"
            f"## Current notice\n\n{supplier['notice']}\n\n"
            f"## Relevant contract term\n\n{supplier['contract_term']}\n"
        )
        (documents / f"{supplier['supplier_id'].lower()}-notice.md").write_text(
            text, encoding="utf-8"
        )

    generated_files = sorted(
        path
        for path in output.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    )
    file_records = [
        {
            "path": path.relative_to(output).as_posix(),
            "sha256": _sha256(path),
            "bytes": path.stat().st_size,
            "visibility": "evaluation-only"
            if path.is_relative_to(evaluation)
            else "service",
        }
        for path in generated_files
    ]
    manifest = {
        "schema_version": 1,
        "dataset_id": dataset["id"],
        "dataset_version": dataset["version"],
        "seed": dataset["seed"],
        "prediction_cutoff": dataset["prediction_cutoff"],
        "as_of_date": dataset["as_of_date"],
        "prediction_horizon_days": horizon,
        "row_counts": {
            "suppliers": len(supplier_rows),
            "facilities": len(facilities),
            "products": len(products),
            "purchase_orders": len(order_rows),
            "shipment_events": len(event_rows),
            "delivery_outcomes": len(outcome_rows),
            "evaluation_labels": len(label_rows),
        },
        "files": file_records,
    }
    fingerprint_input = "\n".join(
        f"{row['path']}:{row['sha256']}" for row in file_records
    )
    manifest["fingerprint"] = hashlib.sha256(
        fingerprint_input.encode("utf-8")
    ).hexdigest()
    _write_json(output / "manifest.json", manifest)
    return manifest


def _csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def validate(output: Path = DEFAULT_OUTPUT) -> dict[str, Any]:
    """Validate hashes, entity links, and the prediction leakage boundary."""

    manifest = _read_json(output / "manifest.json")
    cutoff = _parse_date(manifest["prediction_cutoff"])
    for record in manifest["files"]:
        path = output / record["path"]
        if not path.is_file() or _sha256(path) != record["sha256"]:
            raise ValueError(f"generated file hash mismatch: {record['path']}")

    structured = output / "service" / "structured"
    suppliers = _csv_rows(structured / "suppliers.csv")
    facilities = _csv_rows(structured / "facilities.csv")
    products = _csv_rows(structured / "products.csv")
    orders = _csv_rows(structured / "purchase_orders.csv")
    events = _csv_rows(structured / "shipment_events.csv")
    outcomes = _csv_rows(structured / "delivery_outcomes.csv")
    labels = _csv_rows(output / "evaluation" / "labels.csv")

    supplier_ids = {row["supplier_id"] for row in suppliers}
    facility_ids = {row["facility_id"] for row in facilities}
    product_ids = {row["product_id"] for row in products}
    order_ids = {row["order_id"] for row in orders}
    if len(order_ids) != len(orders):
        raise ValueError("generated purchase-order identifiers are not unique")
    if orders and {"delivered_date", "delivery_outcome"} & set(orders[0]):
        raise ValueError("prediction labels must not be embedded in purchase orders")
    horizon_end = _parse_date(manifest["as_of_date"])
    for row in orders:
        if (
            row["supplier_id"] not in supplier_ids
            or row["facility_id"] not in facility_ids
            or row["product_id"] not in product_ids
        ):
            raise ValueError(
                f"generated purchase order has an unresolved entity: {row['order_id']}"
            )
        if row["split"] == "evaluation" and not (
            row["status_at_cutoff"] == "in_transit"
            and _parse_date(row["order_date"]) <= cutoff
            and cutoff < _parse_date(row["promised_date"]) <= horizon_end
        ):
            raise ValueError(
                f"evaluation order is outside the prediction population: {row['order_id']}"
            )
    for row in events:
        if row["order_id"] not in order_ids:
            raise ValueError(
                f"shipment event has an unresolved order: {row['event_id']}"
            )
        if _parse_date(row["event_date"]) > cutoff:
            raise ValueError(
                f"post-cutoff event leaked into service data: {row['event_id']}"
            )
    historical_ids = {row["order_id"] for row in orders if row["split"] == "history"}
    if (
        len(outcomes) != len(historical_ids)
        or {row["order_id"] for row in outcomes} != historical_ids
    ):
        raise ValueError("historical delivery outcomes do not match historical orders")
    if len(outcomes) != len({row["outcome_id"] for row in outcomes}):
        raise ValueError("generated delivery-outcome identifiers are not unique")
    for row in outcomes:
        if row["outcome"] not in {"late", "on_time"}:
            raise ValueError(
                f"invalid historical delivery outcome: {row['outcome_id']}"
            )
        if _parse_date(row["outcome_date"]) > cutoff:
            raise ValueError(
                f"post-cutoff outcome leaked into service data: {row['outcome_id']}"
            )
    if {row["order_id"] for row in labels} != {
        row["order_id"] for row in orders if row["split"] == "evaluation"
    }:
        raise ValueError("evaluation labels do not match evaluation purchase orders")
    for row in labels:
        actual = _parse_date(row["actual_delivery_date"])
        expected = str(row["late"] == "true" and cutoff < actual <= horizon_end).lower()
        if row["late_within_horizon"] != expected:
            raise ValueError(f"invalid horizon label: {row['order_id']}")

    document_ids = set()
    for path in sorted((output / "service" / "documents").glob("*.md")):
        supplier_id = path.name.removesuffix("-notice.md").upper()
        if supplier_id not in supplier_ids or f"`{supplier_id}`" not in path.read_text(
            encoding="utf-8"
        ):
            raise ValueError(
                f"generated document has an unresolved supplier: {path.name}"
            )
        document_ids.add(supplier_id)
    if document_ids != supplier_ids:
        raise ValueError("generated supplier notices are incomplete")

    return {
        "dataset_id": manifest["dataset_id"],
        "fingerprint": manifest["fingerprint"],
        "service_files": sum(
            1 for row in manifest["files"] if row["visibility"] == "service"
        ),
        "evaluation_files": sum(
            1 for row in manifest["files"] if row["visibility"] == "evaluation-only"
        ),
        "purchase_orders": len(orders),
        "evaluation_orders": len(labels),
        "documents": len(document_ids),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--validate",
        action="store_true",
        help="validate the generated output after writing it",
    )
    args = parser.parse_args()

    manifest = generate(args.spec.resolve(), args.output.resolve())
    print(
        f"Generated {manifest['dataset_id']} {manifest['dataset_version']}: "
        f"{manifest['row_counts']['purchase_orders']} orders"
    )
    if args.validate:
        summary = validate(args.output.resolve())
        print(f"Validated data fingerprint: {summary['fingerprint']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
