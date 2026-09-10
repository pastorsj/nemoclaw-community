#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Package generated data and materialize Query Claw's deployment allowlist."""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from pathlib import Path

from data_packs import discover_packs, materialize_active_packs, select_packs


EXAMPLE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GENERATED = EXAMPLE_ROOT / ".runtime" / "data"
DEFAULT_PACKS = EXAMPLE_ROOT / ".runtime" / "data-packs"
DEFAULT_ACTIVE = EXAMPLE_ROOT / ".runtime" / "active-data"

SUPPLY_CHAIN_DEFINITION = {
    "schema_version": 1,
    "id": "supply-chain",
    "title": "Synthetic Supply Chain",
    "description": (
        "Synthetic suppliers, orders, shipment events, outcomes, and notices "
        "for governed retrieval and late-delivery prediction."
    ),
    "industry": "Manufacturing",
    "views": {
        "structured": "structured",
        "documents": "documents",
        "predictions": "structured",
    },
    "bindings": {
        "ontology": {
            "database": "query_claw",
            "prediction_database": "query_claw",
            "prediction_probe": (
                "Using predictions only, as of 2026-05-31, which open purchase "
                "orders are most likely to deliver late in the next 30 days?"
            ),
        },
        "retriever": {"collection": "query-claw-supply-chain"},
    },
}


def install_supply_chain_pack(generated: Path, packs_root: Path) -> None:
    """Replace only the code-owned built-in pack; preserve operator packs."""

    service = generated / "service"
    if service.is_symlink() or not service.is_dir():
        raise ValueError(f"generated service data is unavailable: {service}")
    for name in ("structured", "documents"):
        source = service / name
        if source.is_symlink() or not source.is_dir():
            raise ValueError(f"generated {name} data is unavailable: {source}")

    packs_root.mkdir(parents=True, exist_ok=True)
    destination = packs_root / "supply-chain"
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise ValueError("built-in data-pack destination must be a regular directory")

    staging = Path(tempfile.mkdtemp(prefix=".supply-chain-", dir=packs_root))
    backup: Path | None = None
    try:
        for name in ("structured", "documents"):
            shutil.copytree(service / name, staging / name)
        (staging / "pack.json").write_text(
            json.dumps(SUPPLY_CHAIN_DEFINITION, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if destination.exists():
            backup = Path(
                tempfile.mkdtemp(prefix=".supply-chain.previous-", dir=packs_root)
            )
            backup.rmdir()
            destination.replace(backup)
        try:
            staging.replace(destination)
        except Exception:
            if backup is not None:
                backup.replace(destination)
                backup = None
            raise
        if backup is not None:
            shutil.rmtree(backup)
            backup = None
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generated", type=Path, default=DEFAULT_GENERATED)
    parser.add_argument("--packs-root", type=Path, default=DEFAULT_PACKS)
    parser.add_argument("--active-dir", type=Path, default=DEFAULT_ACTIVE)
    parser.add_argument("--datasets", default="supply-chain")
    parser.add_argument(
        "--no-install-built-in",
        dest="no_install_builtin",
        action="store_true",
        help="materialize operator packs without installing the built-in pack",
    )
    args = parser.parse_args()

    if args.active_dir.is_symlink():
        parser.error("--active-dir must not be a symlink")
    if not args.no_install_builtin:
        install_supply_chain_pack(args.generated.resolve(), args.packs_root.resolve())
    registry = discover_packs(args.packs_root.resolve())
    selected = select_packs(registry, value=args.datasets)
    if not args.no_install_builtin and "supply-chain" not in selected.ids:
        raise ValueError(
            "the one-command deployment requires supply-chain; supplemental "
            "document-only packs may be selected alongside it"
        )
    unsupported = [
        pack.id
        for pack in selected.packs
        if pack.id != "supply-chain" and set(pack.definition["views"]) != {"documents"}
    ]
    if not args.no_install_builtin and unsupported:
        raise ValueError(
            "the one-command deployment accepts supplemental document-only packs; "
            "pre-provision structured services separately for: "
            + ", ".join(unsupported)
        )
    manifest = materialize_active_packs(selected, args.active_dir)
    print(f"Activated {', '.join(selected.ids)} ({selected.fingerprint})")
    print(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
