# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Command-line interface: ``policy-compiler <command>``."""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sys
from pathlib import Path
from typing import Sequence

import yaml

from policy_compiler import __version__, paths
from policy_compiler.analysis import (
    breadth,
    permitted_operations,
    rules_dropped_by_lowering,
)
from policy_compiler.backends.openshell import (
    dump_policy_yaml,
    lift,
    load_policy_file,
    lower,
)
from policy_compiler.graph.model import CatalogDocument, Service
from policy_compiler.graph.store import (
    CatalogError,
    Graph,
    dump_catalog_document,
    load_graph,
)
from policy_compiler.ir.common import Inspection, Layer, Privilege
from policy_compiler.ir.contract import Contract, Prohibition, UnknownKind
from policy_compiler.report import (
    ReportContext,
    compile_report_data,
    contract_to_dict,
    labelled_diagnostics,
    render_compile_report,
    render_explain_report,
)
from policy_compiler.resolve import Resolution, ResolveOptions, resolve

SPDX_HEADER = [
    "SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.",
    "SPDX-License-Identifier: Apache-2.0",
]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "agent"


def _semantic_index(graph: Graph):
    from policy_compiler.semantic import SemanticIndex

    return SemanticIndex(graph)


FORBID_LEVELS = {"read": Privilege.READ, "write": Privilege.WRITE, "admin": Privilege.ADMIN}


def _parse_forbid(values: list[str], graph: Graph) -> list[Prohibition]:
    """Parse ``--forbid PROVIDER:LEVEL``. A typo must fail loudly: a prohibition that matches nothing forbids nothing."""
    prohibitions = []
    for i, value in enumerate(values, start=1):
        provider, _, level = value.partition(":")
        if not provider or not level:
            raise SystemExit(f"--forbid expects PROVIDER:LEVEL (for example github:write), got {value!r}")
        privilege = FORBID_LEVELS.get(level)
        if privilege is None:
            raise SystemExit(f"--forbid level must be read, write, or admin, got {level!r}")
        if provider not in graph.providers:
            known = ", ".join(sorted(graph.providers))
            raise SystemExit(f"--forbid names an unknown provider {provider!r}; the catalog has: {known}")
        prohibitions.append(
            Prohibition(
                id=f"forbid-{i:02d}-{_slug(provider)}",
                rationale=f"No {provider} operation at {level} level or above (--forbid {value}).",
                provider=provider,
                min_privilege=privilege,
            )
        )
    return prohibitions


def _agent_layer_documents(contract: Contract, catalog_graph: Graph) -> tuple[list[CatalogDocument], list[str]]:
    """Import the API specs an agent definition references, unless a catalog already covers them.

    An agent-layer spec may only describe hosts that no base or org catalog serves, on any port: otherwise
    it could relabel a catalogued operation (a DELETE as ``read``) and slip past ``--forbid``. The check
    runs on the imported document, so server variables count. Agent-layer services also may not choose
    ``inspection`` or ``allowed_ips``; only base and org catalogs set those.
    """
    from policy_compiler.graph.importers.openapi import import_openapi, load_spec_file

    documents: list[CatalogDocument] = []
    notes: list[str] = []
    accepted: list[Service] = []
    for ref in contract.referenced_specs:
        if re.match(r"^[a-z]+://", ref):
            notes.append(f"Referenced spec {ref} is remote; import it into a catalog with `import openapi`.")
            continue
        path = Path(ref)
        if not path.exists():
            notes.append(f"Referenced spec {ref} was not found.")
            continue
        spec = load_spec_file(path)
        provider_id = spec.get("x-openshell-provider") or _slug(spec.get("info", {}).get("title", path.stem))
        if provider_id in catalog_graph.providers:
            notes.append(
                f"Referenced spec {path.name} is already described by the catalog "
                f"(provider {provider_id}); it was not imported again."
            )
            continue
        doc, diagnostics = import_openapi(spec, provider_id=provider_id, source=f"agent-spec-ref:{path.name}", layer=Layer.AGENT)
        notes.extend(f"{d.code}: {d.message}" for d in diagnostics if d.level != "info")
        kept: list[Service] = []
        for service in doc.services:
            hosts = ", ".join(dict.fromkeys(_normal_host(origin.host) for origin in service.origins))
            served = _services_serving(service, list(catalog_graph.services.values()))
            if served:
                notes.append(
                    f"Referenced spec {path.name} describes {hosts}, which the catalog already serves "
                    f"({', '.join(served)}); it was not imported, so the catalog's privileges and prohibitions apply."
                )
                continue
            earlier = _services_serving(service, accepted)
            if earlier:
                notes.append(
                    f"Referenced spec {path.name} describes {hosts}, which another referenced spec already "
                    f"describes ({', '.join(earlier)}); it was not imported."
                )
                continue
            _reset_agent_layer_network(service, path.name, hosts, notes)
            kept.append(service)
        if not kept:
            continue
        kept_ids = {service.id for service in kept}
        doc.services = kept
        doc.operations = [op for op in doc.operations if op.service in kept_ids]
        accepted.extend(kept)
        documents.append(doc)
        notes.append(
            f"Imported referenced spec {path.name} as provider {provider_id} into the agent layer "
            f"({len(doc.operations)} operations; privileges without annotations are guessed)."
        )
    return documents, notes


def _normal_host(host: str) -> str:
    return host.lower().rstrip(".")


def _services_serving(service: Service, candidates: list[Service]) -> list[str]:
    """IDs of ``candidates`` with an origin host that overlaps an origin host of ``service``, on any port."""

    def overlap(a: str, b: str) -> bool:
        a, b = _normal_host(a), _normal_host(b)
        return a == b or paths.host_matches(a, b) or paths.host_matches(b, a)

    return [
        other.id
        for other in candidates
        if any(overlap(mine.host, theirs.host) for mine in service.origins for theirs in other.origins)
    ]


def _reset_agent_layer_network(service: Service, spec_name: str, hosts: str, notes: list[str]) -> None:
    """Agent-layer services keep L7 inspection and no ``allowed_ips``: both choices widen what a rule reaches."""
    changed = []
    if service.inspection is not Inspection.L7:
        changed.append(f"inspection {service.inspection.value}")
    if service.allowed_ips:
        changed.append(f"allowed_ips {', '.join(service.allowed_ips)}")
    if not changed:
        return
    service.inspection = Inspection.L7
    service.allowed_ips = []
    notes.append(
        f"Referenced spec {spec_name} sets {' and '.join(changed)} for {hosts}; only base and org catalogs may set "
        "inspection or allowed_ips, so the agent layer keeps L7 inspection and no allowed_ips."
    )


def _suggest_for_unknowns(contract: Contract, graph: Graph) -> None:
    try:
        index = _semantic_index(graph)
    except ImportError:
        return
    for unknown in contract.unknowns:
        if unknown.kind in (UnknownKind.OPAQUE_IMPL, UnknownKind.OPEN_TOOLSET) and unknown.description:
            unknown.suggestions = index.suggest(unknown.description)[:3]


def _write_outputs(
    contract: Contract,
    graph: Graph,
    options: ResolveOptions,
    out_dir: Path,
    base_policy: dict | None,
    header: list[str],
) -> int:
    """Resolve, lower, and write every output. Returns the exit code (2 when the run needs attention)."""
    try:
        index = _semantic_index(graph)
    except ImportError:
        index = None
    resolution = resolve(contract, graph, options, concept_index=index)
    # Lower copies so that lowering diagnostics stay apart from the resolver's.
    policy_access = dataclasses.replace(resolution.access, diagnostics=[])
    boundary_access = dataclasses.replace(resolution.boundary, diagnostics=[])
    policy = lower(policy_access, base_policy=base_policy)
    boundary = lower(boundary_access, base_policy=base_policy)
    # The report describes what was emitted: lowering may drop endpoints and rules (fail closed).
    emitted = lift(policy, source="policy.yaml")
    emitted_boundary = lift(boundary, source="boundary.yaml")
    permitted = permitted_operations(emitted, graph)
    context = ReportContext(
        policy_breadth=breadth(emitted, graph, permitted),
        boundary_breadth=breadth(emitted_boundary, graph),
        permitted=permitted,
        lowering_diagnostics={"policy": policy_access.diagnostics, "boundary": boundary_access.diagnostics},
        catalog_diagnostics=[d for d in getattr(graph, "diagnostics", []) if d.level != "info"],
        dropped_rules=rules_dropped_by_lowering(resolution.access, emitted),
    )
    _note_dropped_needs(resolution, context.dropped_rules)
    policy_breadth, boundary_breadth = context.policy_breadth, context.boundary_breadth

    out_dir.mkdir(parents=True, exist_ok=True)
    outputs = {
        "policy.yaml": out_dir / "policy.yaml",
        "boundary.yaml": out_dir / "boundary.yaml",
        "contract.yaml": out_dir / "contract.yaml",
        "report.md": out_dir / "report.md",
        "report.json": out_dir / "report.json",
    }
    generated = f"Generated by policy-compiler {__version__} for {contract.agent}. Review before use."
    outputs["policy.yaml"].write_text(dump_policy_yaml(policy, header=header + [generated]))
    outputs["boundary.yaml"].write_text(
        dump_policy_yaml(
            boundary,
            header=header + [generated, "Boundary (ceiling): the same capabilities without resource scope."],
        )
    )
    contract_data = contract_to_dict(contract)
    if "referenced_specs" in contract_data:
        contract_data["referenced_specs"] = [_display_path(p) for p in contract_data["referenced_specs"]]
    contract_text = yaml.safe_dump(contract_data, sort_keys=False, allow_unicode=True)
    outputs["contract.yaml"].write_text("".join(f"# {h}\n" for h in header) + contract_text)
    outputs["report.md"].write_text(
        render_compile_report(resolution, context, outputs={k: str(v) for k, v in outputs.items()})
    )
    outputs["report.json"].write_text(json.dumps(compile_report_data(resolution, context), indent=2) + "\n")
    print(f"{contract.agent}: {policy_breadth.allow_rules} allow rules, {policy_breadth.deny_rules} deny rules")
    print(f"  hosts: {', '.join(policy_breadth.hosts) or '(none)'}")
    p = policy_breadth.permitted
    print(
        f"  catalogued operations reachable: {p.get('read', 0)} read, {p.get('write', 0)} write, "
        f"{p.get('admin', 0)} admin, {p.get('unknown', 0)} unknown (boundary: "
        f"{sum(boundary_breadth.permitted.values())})"
    )
    statuses = {}
    for n in resolution.needs:
        statuses[n.status] = statuses.get(n.status, 0) + 1
    print(f"  needs: {', '.join(f'{v} {k}' for k, v in statuses.items()) or 'none'}; unknowns: {len(contract.unknowns)}")
    for source, d in labelled_diagnostics(resolution, context):
        if d.level in ("error", "warning"):
            print(f"  {d.level}: {'' if source == 'resolver' else source + ': '}{d.code}: {d.message}")
    print(f"  wrote {out_dir}/: " + ", ".join(outputs))
    problems = _exit_problems(resolution, context)
    if problems:
        sys.stdout.flush()
        print(f"policy-compiler: exit 2: {', '.join(problems)}", file=sys.stderr)
        return 2
    return 0


def _exit_problems(resolution: Resolution, context: ReportContext) -> list[str]:
    """Why the run must exit 2: conflict or unresolved needs, or error-level diagnostics from any stage."""
    problems = []
    for status in ("conflict", "unresolved"):
        ids = [n.need for n in resolution.needs if n.status == status]
        if ids:
            problems.append(f"{len(ids)} {status} need{'' if len(ids) == 1 else 's'} ({', '.join(ids)})")
    errors = [f"{source}: {d.code}" for source, d in labelled_diagnostics(resolution, context) if d.level == "error"]
    if errors:
        unique = list(dict.fromkeys(errors))
        problems.append(f"{len(errors)} error diagnostic{'' if len(errors) == 1 else 's'} ({', '.join(unique)})")
    return problems


def _note_dropped_needs(resolution: Resolution, dropped: set[int]) -> None:
    """Tell each need whose rules lowering dropped that the emitted policy does not grant them."""
    needs = {n.need: n for n in resolution.needs}
    affected = dict.fromkeys(
        reason for index in sorted(dropped) for reason in resolution.access.rules[index].reasons if reason in needs
    )
    for need_id in affected:
        needs[need_id].notes.append(
            "Some of its rules were dropped by lowering (fail closed), so the emitted policy does not grant "
            "them; see the lowering diagnostics."
        )


def _display_path(value: str) -> str:
    """Show local paths relative to the working directory so generated files stay machine-independent."""
    path = Path(value)
    if not path.is_absolute():
        return value
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return value


def _load_base_policy(path: str | None) -> dict | None:
    return load_policy_file(Path(path)) if path else None


def _graph_for(args, extra: list[CatalogDocument] = ()) -> Graph:
    try:
        return load_graph([Path(c) for c in args.catalog], extra_documents=list(extra))
    except CatalogError as exc:
        raise SystemExit(f"catalog error: {exc}") from exc


def cmd_compile(args) -> int:
    from policy_compiler.frontends.agentspec import contract_from_agentspec

    if not args.client:
        raise SystemExit("compile needs at least one --client (the real path of the binary that runs the agent).")
    contract = contract_from_agentspec(
        Path(args.agent_spec),
        default_clients=args.client,
        reference_files=[Path(r) for r in args.reference_file],
    )
    catalog_graph = _graph_for(args)
    agent_docs, notes = _agent_layer_documents(contract, catalog_graph)
    contract.notes.extend(notes)
    graph = _graph_for(args, agent_docs) if agent_docs else catalog_graph
    contract.prohibitions.extend(_parse_forbid(args.forbid, graph))
    _suggest_for_unknowns(contract, graph)
    use_case = None
    if args.use_case_file:
        use_case = Path(args.use_case_file).read_text()
    elif args.use_case:
        use_case = args.use_case
    options = ResolveOptions(
        use_case=use_case,
        strict_use_case=args.strict_use_case,
        route_inference_local=args.route_inference_local,
    )
    out_dir = Path(args.out or f"out/{_slug(contract.agent)}")
    return _write_outputs(contract, graph, options, out_dir, _load_base_policy(args.base_policy), _header(args))


def cmd_from_use_case(args) -> int:
    from policy_compiler.frontends.usecase import contract_from_use_case

    if not args.client:
        raise SystemExit("from-use-case needs at least one --client (the real path of the binary that runs the agent).")
    graph = _graph_for(args)
    text = Path(args.text_file).read_text() if args.text_file else args.text
    if not text:
        raise SystemExit("Give the use case as an argument or with --text-file.")
    contract = contract_from_use_case(
        text, graph, default_clients=args.client, inference=args.inference, agent_name=args.name
    )
    contract.prohibitions.extend(_parse_forbid(args.forbid, graph))
    # The needs come from the text itself, so justifying them against it again adds nothing.
    options = ResolveOptions(use_case="")
    out_dir = Path(args.out or f"out/{_slug(args.name)}")
    return _write_outputs(contract, graph, options, out_dir, _load_base_policy(args.base_policy), _header(args))


def cmd_explain(args) -> int:
    graph = _graph_for(args)
    rows = []
    for name in args.policy:
        access = lift(load_policy_file(Path(name)), source=name)
        permitted = permitted_operations(access, graph)
        b = breadth(access, graph, permitted)
        if len(args.policy) == 1 or args.detail:
            print(render_explain_report(name, access, graph, b, permitted))
        rows.append((name, access, b))
    if len(args.policy) > 1:
        print("| Policy | Score | Hosts | Reachable ops (r/w/a/u) | Not in catalog | Errors | Warnings |")
        print("| --- | --- | --- | --- | --- | --- | --- |")
        for name, access, b in sorted(rows, key=lambda r: r[2].score, reverse=True):
            p = b.permitted
            errors = sum(1 for d in access.diagnostics if d.level == "error")
            warnings = sum(1 for d in access.diagnostics if d.level == "warning")
            print(
                f"| {name} | {b.score} | {len(b.hosts)} | {p.get('read', 0)}/{p.get('write', 0)}/"
                f"{p.get('admin', 0)}/{p.get('unknown', 0)} | {len(b.uncatalogued_hosts)} | {errors} | {warnings} |"
            )
    return 0


def cmd_lift(args) -> int:
    access = lift(load_policy_file(Path(args.policy)), source=args.policy)
    for d in access.diagnostics:
        print(f"# {d.level}: {d.code}: {d.message} {d.pointer}".rstrip())
    if args.normalize:
        print(dump_policy_yaml(lower(access, normalize=True)))
    return 1 if any(d.level == "error" for d in access.diagnostics) else 0


def _write_catalog(doc: CatalogDocument, out: str | None, header: list[str]) -> None:
    data = dump_catalog_document(doc)
    text = "".join(f"# {h}\n" for h in header) + yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(text)
        print(
            f"wrote {out}: {len(doc.services)} services, {len(doc.capabilities)} capabilities, "
            f"{len(doc.operations)} operations"
        )
    else:
        sys.stdout.write(text)


def cmd_import_openapi(args) -> int:
    from policy_compiler.graph.importers.openapi import import_openapi, load_spec_file

    spec = load_spec_file(Path(args.spec))
    overlay = load_spec_file(Path(args.overlay)) if args.overlay else None
    doc, diagnostics = import_openapi(
        spec,
        provider_id=args.provider_id,
        source=f"openapi:{Path(args.spec).name}",
        layer=Layer.ORG,
        overlay=overlay,
        server_url=args.server_url,
        provider_name=args.provider_name,
    )
    for d in diagnostics:
        print(f"{d.level}: {d.code}: {d.message}", file=sys.stderr)
    _write_catalog(doc, args.out, _header(args))
    return 1 if any(d.level == "error" for d in diagnostics) else 0


def cmd_import_github(args) -> int:
    from policy_compiler.graph.importers.github_permissions import (
        import_github_permissions,
    )

    data = json.loads(Path(args.file).read_text())
    doc = import_github_permissions(data, source=Path(args.file).name, layer=Layer.ORG)
    _write_catalog(doc, args.out, _header(args))
    return 0


def cmd_import_policies(args) -> int:
    from policy_compiler.graph.importers.openshell_policies import (
        import_openshell_policies,
    )

    known = _graph_for(args)
    policies = [(name, load_policy_file(Path(name))) for name in args.policy]
    doc, usage = import_openshell_policies(policies, known=known, layer=Layer.ORG)
    if args.usage:
        Path(args.usage).write_text(json.dumps(usage, indent=2, sort_keys=True) + "\n")
    print("| Host | In catalog | Policies | Methods | Binaries |", file=sys.stderr)
    print("| --- | --- | --- | --- | --- |", file=sys.stderr)
    for host, entry in sorted(usage.items(), key=lambda kv: (-len(kv[1]["policies"]), kv[0])):
        print(
            f"| {host} | {'yes' if entry['catalogued'] else 'no'} | {len(entry['policies'])} | "
            f"{', '.join(entry['methods']) or '-'} | {len(entry['binaries'])} |",
            file=sys.stderr,
        )
    _write_catalog(doc, args.out, _header(args))
    return 0


def cmd_catalog_check(args) -> int:
    graph = _graph_for(args)
    print(
        f"providers {len(graph.providers)}, services {len(graph.services)}, capabilities {len(graph.capabilities)}, "
        f"operations {len(graph.operations)}, concepts {len(graph.concepts)}, clients {len(graph.clients)}"
    )
    for d in getattr(graph, "diagnostics", []):
        print(f"{d.level}: {d.code}: {d.message}")
    return 0


def _header(args) -> list[str]:
    return SPDX_HEADER if getattr(args, "spdx_header", False) else []


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="policy-compiler",
        description="Compile agent definitions and API knowledge into least-privilege OpenShell policies.",
    )
    parser.add_argument("--version", action="version", version=f"policy-compiler {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser, *, clients: bool = False, outputs: bool = False) -> None:
        p.add_argument("--catalog", action="append", default=[], metavar="DIR", help="Org catalog directory (repeatable).")
        if clients:
            p.add_argument(
                "--client",
                action="append",
                default=[],
                metavar="PATH",
                help="Real path of a binary that makes the agent's calls (repeatable).",
            )
            p.add_argument(
                "--forbid", action="append", default=[], metavar="PROVIDER:LEVEL", help="Forbid a provider at a level."
            )
        if outputs:
            p.add_argument("--base-policy", help="Keep filesystem, process, and Landlock sections from this policy.")
            p.add_argument("--out", help="Output directory (default: out/<agent>).")
        p.add_argument("--spdx-header", action="store_true", help="Start generated YAML files with SPDX lines.")

    p = sub.add_parser("compile", help="Compile an Agent Spec file into a policy, boundary, and report.")
    p.add_argument("agent_spec")
    common(p, clients=True, outputs=True)
    p.add_argument("--reference-file", action="append", default=[], help="Agent Spec file with referenced components.")
    p.add_argument("--use-case", help="Text that describes the intended use, to justify needs.")
    p.add_argument("--use-case-file")
    p.add_argument("--strict-use-case", action="store_true", help="Drop needs the use case does not justify.")
    p.add_argument("--route-inference-local", action="store_true", help="Send model traffic to inference.local.")
    p.set_defaults(func=cmd_compile)

    p = sub.add_parser("from-use-case", help="Compile a plain-language use case into a policy.")
    p.add_argument("text", nargs="?")
    p.add_argument("--text-file")
    p.add_argument("--name", default="use-case")
    p.add_argument("--inference", choices=["local", "nvidia", "openai", "none"], default="local")
    common(p, clients=True, outputs=True)
    p.set_defaults(func=cmd_from_use_case)

    p = sub.add_parser("explain", help="Explain what existing policies permit, with lint findings.")
    p.add_argument("policy", nargs="+")
    p.add_argument("--detail", action="store_true", help="Print the full explanation for every policy.")
    common(p)
    p.set_defaults(func=cmd_explain)

    p = sub.add_parser("lift", help="Lint an existing policy; optionally print a normalized version.")
    p.add_argument("policy")
    p.add_argument("--normalize", action="store_true")
    p.set_defaults(func=cmd_lift)

    imp = sub.add_parser("import", help="Import API knowledge into a catalog file.").add_subparsers(
        dest="kind", required=True
    )
    p = imp.add_parser("openapi", help="Import an OpenAPI spec, optionally with an OpenAPI Overlay.")
    p.add_argument("spec")
    p.add_argument("--provider-id", required=True)
    p.add_argument("--provider-name")
    p.add_argument("--overlay")
    p.add_argument("--server-url")
    p.add_argument("--out")
    common(p)
    p.set_defaults(func=cmd_import_openapi)

    p = imp.add_parser("github-permissions", help="Import GitHub's fine-grained permission data.")
    p.add_argument("file")
    p.add_argument("--out")
    common(p)
    p.set_defaults(func=cmd_import_github)

    p = imp.add_parser("openshell-policies", help="Mine existing OpenShell policies into a catalog file.")
    p.add_argument("policy", nargs="+")
    p.add_argument("--out")
    p.add_argument("--usage", help="Write the host usage index as JSON.")
    common(p)
    p.set_defaults(func=cmd_import_policies)

    p = sub.add_parser("catalog", help="Catalog utilities.").add_subparsers(dest="action", required=True).add_parser(
        "check", help="Load and validate the layered catalog."
    )
    common(p)
    p.set_defaults(func=cmd_catalog_check)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
