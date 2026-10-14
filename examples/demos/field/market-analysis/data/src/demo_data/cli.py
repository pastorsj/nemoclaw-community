# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""demo-data: validate | fetch | prepare [--structured | --corpus] | verify | list | clean.

Settings come from flags or the environment:
  DATA_PACK           pack id under the packs directory (default synthetic-market)
  DATA_PACK_PROFILE   generator profile (default: the pack's default_profile)
  DATA_CORPORA        comma-separated corpus sources to build (default: every corpus that is not opt-in)
  DATA_DIR            where builds live (default /data)
  DATA_SOURCE_DIR     where external datasets live, one directory per dataset (default /sources)
  DATA_SOURCE_<ID>    where `fetch` gets external dataset <id> from (see fetch.py)
  DATA_PACKS_DIR      where packs live (default: the packs directory next to this package)
  DATA_CONTRACTS_DIR  tool contracts to validate against (default: tools/*/contract/ in the repository)
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from demo_data import corpus
from demo_data import external
from demo_data import fetch
from demo_data import publish
from demo_data import sec
from demo_data import structured
from demo_data.corpus.common import CorpusError
from demo_data.corpus.common import Downloads
from demo_data.external import ExternalError
from demo_data.market import MarketError
from demo_data.pack import DATA_ROOT
from demo_data.pack import Pack
from demo_data.pack import PackError
from demo_data.pack import applicable_contracts
from demo_data.pack import declared_contract_errors
from demo_data.pack import find_contracts
from demo_data.pack import load_pack


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.run(args)
    except (
        PackError,
        CorpusError,
        ExternalError,
        MarketError,
        sec.SecError,
        structured.BuildError,
        subprocess.CalledProcessError,
    ) as error:
        print(f"demo-data: {error}", file=sys.stderr)
        return 1


def parser() -> argparse.ArgumentParser:
    env = os.environ.get
    root = argparse.ArgumentParser(
        prog="demo-data", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    root.add_argument("--pack", default=env("DATA_PACK") or "synthetic-market")
    root.add_argument("--packs-dir", type=Path, default=env("DATA_PACKS_DIR") or DATA_ROOT / "packs")
    root.add_argument("--data-dir", type=Path, default=env("DATA_DIR") or "/data")
    root.add_argument("--sources-dir", type=Path, default=env("DATA_SOURCE_DIR") or "/sources")
    root.add_argument("--contracts-dir", type=Path, default=env("DATA_CONTRACTS_DIR") or None)
    commands = root.add_subparsers(required=True, metavar="command")

    command = commands.add_parser("validate", help="check the pack: schema, cross-references, tool contracts")
    command.set_defaults(run=validate)

    command = commands.add_parser("fetch", help="fetch the pack's external datasets and verify them")
    command.add_argument("datasets", nargs="*", help="dataset ids (default: every external dataset of the pack)")
    command.add_argument("--verify-only", action="store_true", help="only hash what is already in place")
    command.add_argument("--jobs", type=int, default=int(env("DATA_FETCH_JOBS") or 8), help="parallel files")
    command.set_defaults(run=fetch_datasets)

    command = commands.add_parser("prepare", help="build the pack (or reuse its cached build) and make it active")
    part = command.add_mutually_exclusive_group()
    part.add_argument("--structured", action="store_true", help="only tables, database, ontology and prediction")
    part.add_argument("--corpus", action="store_true", help="only corpus/documents.jsonl")
    command.add_argument("--profile", default=env("DATA_PACK_PROFILE") or None)
    command.add_argument("--corpora", default=env("DATA_CORPORA") or None, help="comma-separated corpus sources")
    command.add_argument("--refresh-sec", action="store_true", help="fetch a new SEC company snapshot")
    command.set_defaults(run=prepare)

    command = commands.add_parser("verify", help="check the active build against its pack.json")
    command.set_defaults(run=verify)

    command = commands.add_parser("list", help="show the packs available and the builds present")
    command.set_defaults(run=list_packs)

    command = commands.add_parser("clean", help="remove inactive builds and the rollups only they used")
    command.add_argument("--all", action="store_true", help="also remove the download cache and every cache")
    command.set_defaults(run=clean)
    return root


def validate(args: argparse.Namespace) -> int:
    pack = load_pack(args.packs_dir / args.pack)
    contracts = find_contracts(args.contracts_dir)
    errors = declared_contract_errors(pack, contracts) if pack.structured else []
    if errors:
        raise PackError(pack.id, errors)
    print(f"{pack.id}@{pack.manifest['version']}: valid")
    print(f"  {len(pack.tables)} tables, {len(pack.manifest['sources'])} sources, {len(pack.questions)} questions")
    if declared := pack.manifest.get("analytics", {}).get("contract"):
        found = bool(applicable_contracts(pack, contracts))
        print(f"  contract {declared}: {'checked against schema.sql' if found else 'not found, not checked'}")
    return 0


def fetch_datasets(args: argparse.Namespace) -> int:
    pack = load_pack(args.packs_dir / args.pack)
    found = external.datasets(pack.manifest, args.sources_dir)
    if not found:
        print(f"{pack.id}: no external datasets")
        return 0
    unknown = sorted(set(args.datasets) - set(found))
    if unknown:
        raise PackError(pack.id, [f"no external dataset {unknown}; choose from {sorted(found)}"])
    for dataset_id in args.datasets or sorted(found):
        dataset = found[dataset_id]
        source = os.environ.get(dataset.source_variable, "").strip()
        started = time.monotonic()
        if source and not args.verify_only:
            print(f"{dataset_id}: fetching from {source.split('?', 1)[0]} into {dataset.root}")
            report = fetch.fetch(dataset, source, jobs=args.jobs)
        else:
            print(f"{dataset_id}: verifying {dataset.root}")
            report = external.verify(dataset, jobs=args.jobs)
        print(
            f"{dataset_id}: {report.files:,} files, {report.bytes / 1e9:.2f} GB, {report.hashed:,} hashed, "
            f"in {time.monotonic() - started:.1f}s"
        )
        if report.extra:
            print(f"{dataset_id}: {len(report.extra):,} files are not in the manifest (left alone): {report.extra[0]}")
        if report.bad:
            raise ExternalError(
                f"{dataset_id}: {len(report.bad):,} files are missing or do not match the manifest, "
                f"e.g. {report.bad[0]}; fetch them with {dataset.source_variable} set"
            )
        print(f"{dataset_id}: verified (fingerprint {dataset.fingerprint[:12]})")
    return 0


def prepare(args: argparse.Namespace) -> int:
    pack = load_pack(args.packs_dir / args.pack)
    profile = pack.resolve_profile(args.profile)
    corpora = pack.select_corpora(args.corpora.split(",") if args.corpora else None)
    wanted = ["structured"] if args.structured else ["corpus"] if args.corpus else ["structured", "corpus"]
    available = {"structured": pack.structured is not None, "corpus": bool(corpora)}
    parts = [part for part in wanted if available[part]]
    if not parts:
        print(f"{pack.id}: nothing to prepare for {' or '.join(wanted)}")
        return 0
    if "corpus" in parts:
        require_env(pack, corpora)
    contracts = find_contracts(args.contracts_dir)
    cache_dir = args.data_dir / "cache"
    inputs = []
    if pack.manifest.get("market", {}).get("companies") == "sec":
        inputs.append(f"sec {sec.digest(sec.snapshot(cache_dir, refresh=args.refresh_sec))}")
    digest = pack.digest(profile, corpora, publish.builder_fingerprint(), inputs)
    name = f"{pack.id}@{pack.manifest['version']}+{profile}+{digest[:12]}"

    with publish.locked(args.data_dir):
        build = publish.Build(args.data_dir, name)
        for part in parts:
            if build.has(part):
                print(f"{part}: up to date in {name}")
                continue
            started = time.monotonic()
            with build.staging(part) as staging:
                if part == "structured":
                    receipt = structured.build(
                        pack, profile, contracts, staging, sources_dir=args.sources_dir, cache_dir=cache_dir
                    )
                else:
                    downloads = Downloads(args.data_dir / "downloads")
                    receipt = {
                        "documents": corpus.build(pack, corpora, downloads, staging, sources_dir=args.sources_dir)
                    }
            # The build is keyed on the selected corpora, but pack.json offers their sources and questions only
            # once the corpus part is in it.
            served = corpora if part == "corpus" or build.has("corpus") else []
            root = (
                structured.market_root(pack, profile, args.sources_dir, cache_dir)
                if "market" in pack.manifest
                else None
            )
            resolved = pack.resolve(profile, served, root)
            build.record(part, structured.with_population(resolved, build.directory), receipt)
            print(f"{part}: built in {time.monotonic() - started:.1f}s ({summary(receipt)})")
            if imported := receipt.get("import"):
                rollup = imported["rollup"]
                dropped = ", ".join(f"{reason} {count:,}" for reason, count in imported["dropped"].items() if count)
                print(
                    f"import: {imported['assets']:,} of {imported['symbols']:,} symbols kept ({dropped or 'none'} "
                    f"dropped), {imported['sessions']:,} sessions; daily rollup {rollup['key']} "
                    + ("reused" if rollup["cached"] else f"built in {rollup['seconds']}s")
                )
        if not build.ready():
            print(
                f"demo-data: the corpus is in {name}, which has no structured part, so it is not active: run "
                "`prepare --structured` with the same DATA_PACK_PROFILE and DATA_CORPORA",
                file=sys.stderr,
            )
            return 1
        if publish.active_build(args.data_dir) == build.directory:
            print(f"active: builds/{name}")
        else:
            publish.activate(args.data_dir, build)
            print(f"active -> builds/{name}")
    return 0


def summary(receipt: dict[str, Any]) -> str:
    counts = receipt.get("rows") or receipt["documents"]
    unit = "rows" if "rows" in receipt else "documents"
    return ", ".join(f"{name} {count:,}" for name, count in counts.items()) + f" {unit}"


def require_env(pack: Pack, corpora: list[dict[str, Any]]) -> None:
    """Fail before any work when a corpus origin needs an environment variable that is not set."""
    missing = [
        (corpus["source"], variable)
        for corpus in corpora
        for variable in pack.origins[corpus["origin"]].get("requires_env", [])
        if not os.environ.get(variable)
    ]
    if missing:
        blocked = {source for source, _ in missing}
        others = list(dict.fromkeys(corpus["source"] for corpus in corpora if corpus["source"] not in blocked))
        problems = "; ".join(f"{source} needs {variable}" for source, variable in missing)
        if others:
            problems += f" (or skip it: set DATA_CORPORA={','.join(others)} for both --structured and --corpus)"
        raise PackError(pack.id, [problems])


def verify(args: argparse.Namespace) -> int:
    active = publish.active_build(args.data_dir)
    if active is None:
        print(f"demo-data: no active build in {args.data_dir}; run `demo-data prepare`", file=sys.stderr)
        return 1
    errors = publish.verify(active)
    for error in errors:
        print(f"demo-data: {active.name}: {error}", file=sys.stderr)
    if not errors:
        print(f"{active.name}: verified")
    return 1 if errors else 0


def list_packs(args: argparse.Namespace) -> int:
    print(f"packs in {args.packs_dir}:")
    for manifest in sorted(args.packs_dir.glob("*/pack.yaml")):
        pack = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        profiles = ", ".join(pack.get("generator", {}).get("profiles", {})) or "default"
        print(f"  {pack['id']}@{pack['version']}  {pack['title']}  (profiles: {profiles})")
    builds = args.data_dir / "builds"
    names = sorted(path.name for path in builds.iterdir() if not path.name.startswith(".")) if builds.is_dir() else []
    active = publish.active_build(args.data_dir)
    print(f"builds in {builds}:" if names else f"no builds in {builds}")
    for build in names:
        print(f"  {build}{'  (active)' if active and active.name == build else ''}")
    return 0


def clean(args: argparse.Namespace) -> int:
    with publish.locked(args.data_dir):
        for path in publish.clean(args.data_dir, downloads=args.all):
            print(f"removed {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
