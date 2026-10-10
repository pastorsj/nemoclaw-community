# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Markdown and JSON reports that explain every generated rule."""

from __future__ import annotations

import dataclasses
from enum import Enum
from typing import Any

from policy_compiler import __version__
from policy_compiler.analysis import SCORE_WEIGHTS, Breadth, PermittedOperation
from policy_compiler.graph.store import Graph
from policy_compiler.ir.access import AccessRule, AccessSet, Diagnostic, Effect
from policy_compiler.ir.contract import Contract
from policy_compiler.resolve import NeedResolution, Resolution

DROPPED_BY_LOWERING = "dropped by lowering"
_USE_CASE_NOTE_PREFIXES = ("Not justified by the use case", "Excluded because the use case")


@dataclasses.dataclass
class ReportContext:
    """What a compile report needs besides the resolution. Breadth and permitted operations describe the
    emitted policy and boundary (``lift(lower(access))``), not the access set before lowering."""

    policy_breadth: Breadth
    boundary_breadth: Breadth
    permitted: dict[str, PermittedOperation] = dataclasses.field(default_factory=dict)
    lowering_diagnostics: dict[str, list[Diagnostic]] = dataclasses.field(default_factory=dict)
    """``policy`` and ``boundary``: what ``lower`` dropped or changed."""
    catalog_diagnostics: list[Diagnostic] = dataclasses.field(default_factory=list)
    """Warnings from loading the catalog graph (``agent-layer-extends``, ``ignored-redefinition``, ...)."""
    dropped_rules: set[int] = dataclasses.field(default_factory=set)
    """Indexes into ``resolution.access.rules`` of the rules the emitted policy does not carry."""


def use_case_justified(resolution: Resolution) -> bool:
    """True only when use-case justification actually ran for this resolution."""
    justified = getattr(resolution, "justified", None)
    return bool(resolution.use_case_concepts) if justified is None else bool(justified)


def _need_notes(result: NeedResolution | None, justified: bool) -> list[str]:
    notes = list(result.notes) if result else []
    if not justified:
        notes = [n for n in notes if not n.startswith(_USE_CASE_NOTE_PREFIXES)]
    return notes


def labelled_diagnostics(resolution: Resolution, context: ReportContext) -> list[tuple[str, Diagnostic]]:
    labelled = [("resolver", d) for d in resolution.access.diagnostics]
    labelled += [("resolver", d) for d in resolution.boundary.diagnostics if d not in resolution.access.diagnostics]
    labelled += [("catalog", d) for d in context.catalog_diagnostics]
    for which, diagnostics in context.lowering_diagnostics.items():
        labelled += [(f"lowering ({which})", d) for d in diagnostics]
    return labelled


def to_plain(value: Any) -> Any:
    """Convert dataclasses, enums, and tuples into YAML- and JSON-friendly values."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [to_plain(v) for v in value]
        return sorted(items) if isinstance(value, (set, frozenset)) else items
    return value


def contract_to_dict(contract: Contract) -> dict:
    return _prune(to_plain(contract))


def _prune(value: Any) -> Any:
    if isinstance(value, dict):
        pruned = {k: _prune(v) for k, v in value.items()}
        return {k: v for k, v in pruned.items() if v not in (None, [], {}, "")}
    if isinstance(value, list):
        return [_prune(v) for v in value]
    return value


def _cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _privilege_counts(b: Breadth) -> str:
    p = b.permitted
    return f"{p.get('read', 0)} / {p.get('write', 0)} / {p.get('admin', 0)} / {p.get('unknown', 0)}"


def _rule_rows(access: AccessSet) -> dict[tuple[str, tuple[str, ...]], list[tuple[int, AccessRule]]]:
    groups: dict[tuple[str, tuple[str, ...]], list[tuple[int, AccessRule]]] = {}
    for index, rule in enumerate(access.rules):
        groups.setdefault((rule.group, rule.clients.paths), []).append((index, rule))
    return groups


def _render_rules(access: AccessSet, lines: list[str], dropped: set[int] = frozenset()) -> None:
    for (group, clients), rules in _rule_rows(access).items():
        binaries = ", ".join(f"`{c}`" for c in clients) or "(none)"
        lines += [f"### `{group}`: {binaries}", ""]
        lines += ["| Effect | Host | Method | Path / tool | Privilege | Operations | Why |", "| --- | --- | --- | --- | --- | --- | --- |"]
        for index, rule in rules:
            matcher = rule.matcher
            if matcher is None:
                method, where = "(any)", f"connection level ({rule.endpoint.protocol.value})"
            else:
                method = matcher.method or ""
                where = matcher.path or (f"tool `{matcher.tool}`" if matcher.tool else "")
            host = f"{rule.endpoint.host}:{','.join(str(p) for p in rule.endpoint.ports)}{rule.endpoint.path}"
            ops = ", ".join(rule.operations) or "not in catalog"
            privilege = rule.privilege.value if rule.privilege else ""
            effect = f"{rule.effect.value}, **{DROPPED_BY_LOWERING}**" if index in dropped else rule.effect.value
            lines.append(
                f"| {effect} | {_cell(host)} | {_cell(method)} | `{_cell(where)}` | {privilege} "
                f"| {_cell(ops)} | {_cell(', '.join(rule.reasons))} |"
            )
        lines.append("")


def render_compile_report(
    resolution: Resolution,
    context: ReportContext,
    *,
    outputs: dict[str, str] | None = None,
) -> str:
    """Render the Markdown report. Breadth figures describe the emitted policy and boundary."""
    contract = resolution.contract
    access = resolution.access
    policy_breadth, boundary_breadth = context.policy_breadth, context.boundary_breadth
    justified = use_case_justified(resolution)
    diagnostics = labelled_diagnostics(resolution, context)
    lines: list[str] = [f"# Policy report: {contract.agent}", ""]
    lines += [
        f"Generated by policy-compiler {__version__} from {', '.join(f'`{s}`' for s in contract.sources) or 'input'}. "
        "Review every rule before you apply it. Generated rules are only as accurate as the agent "
        "definition and the catalog.",
        "",
    ]
    statuses: dict[str, int] = {}
    for n in resolution.needs:
        statuses[n.status] = statuses.get(n.status, 0) + 1
    errors = sum(1 for _, d in diagnostics if d.level == "error")
    warnings = sum(1 for _, d in diagnostics if d.level == "warning")
    lines += [
        "## Summary",
        "",
        "| Measure | Policy | Boundary |",
        "| --- | --- | --- |",
        f"| Allow rules | {policy_breadth.allow_rules} | {boundary_breadth.allow_rules} |",
        f"| Deny rules | {policy_breadth.deny_rules} | {boundary_breadth.deny_rules} |",
        f"| Hosts | {', '.join(policy_breadth.hosts) or '-'} | {', '.join(boundary_breadth.hosts) or '-'} |",
        f"| Catalogued operations reachable (read / write / admin / unknown) | {_privilege_counts(policy_breadth)} "
        f"| {_privilege_counts(boundary_breadth)} |",
        f"| Hosts not in the catalog | {', '.join(policy_breadth.uncatalogued_hosts) or '-'} "
        f"| {', '.join(boundary_breadth.uncatalogued_hosts) or '-'} |",
        f"| Breadth score (lower is narrower) | {policy_breadth.score} | {boundary_breadth.score} |",
        "",
        f"- Client binaries: {', '.join(f'`{c}`' for c in contract.default_clients) or '(none)'}",
        f"- Needs: {', '.join(f'{v} {k}' for k, v in statuses.items()) or 'none'}",
        f"- Unknowns: {len(contract.unknowns)}",
        f"- Diagnostics: {errors} errors, {warnings} warnings",
    ]
    if outputs:
        lines.append("- Files: " + ", ".join(f"`{name}`" for name in outputs))
    lines.append("")

    lines += ["## Rules", ""]
    if context.dropped_rules:
        lines += [
            f"Rules marked **{DROPPED_BY_LOWERING}** are not in `policy.yaml`: lowering removed them rather than "
            "emit something OpenShell would reject or that would allow more (fail closed). The lowering "
            "diagnostics say why.",
            "",
        ]
    if access.rules:
        _render_rules(access, lines, context.dropped_rules)
    else:
        lines += ["No rules were generated. The sandbox will have no network access beyond OpenShell defaults.", ""]

    lines += ["## Needs", "", "| Need | Declared by | Target | Status | Notes |", "| --- | --- | --- | --- | --- |"]
    by_id = {n.need: n for n in resolution.needs}
    for need in contract.needs:
        r = by_id.get(need.id)
        evidence = need.evidence[0] if need.evidence else None
        declared = f"{need.subject or ''} ({evidence.source}{' ' + evidence.pointer if evidence and evidence.pointer else ''})" if evidence else need.subject
        notes = _need_notes(r, justified)
        if need.approval.value != "none":
            notes.append("The definition asks for confirmation before this call; OpenShell cannot prompt mid-request.")
        if need.credential:
            notes.append(f"Carries a credential ({need.credential.placement}); bind it with an OpenShell provider.")
        lines.append(
            f"| {need.id} | {_cell(declared)} | {_cell(need.target.describe())} | {r.status if r else '-'} "
            f"| {_cell(' '.join(notes))} |"
        )
    lines.append("")

    lines += ["## Unknowns", ""]
    if contract.unknowns:
        lines += [
            "The compiler cannot see what these components do on the network. Nothing is granted for them. "
            "Suggestions come from the semantic layer and are never granted automatically.",
            "",
            "| Unknown | Kind | Subject | Description | Suggested concepts |",
            "| --- | --- | --- | --- | --- |",
        ]
        for u in contract.unknowns:
            suggestions = ", ".join(f"{s.concept} ({s.score:.2f})" for s in u.suggestions) or "-"
            lines.append(
                f"| {u.id} | {u.kind.value} | {_cell(u.subject)} | {_cell(u.description)} | {_cell(suggestions)} |"
            )
    else:
        lines.append("None.")
    lines.append("")

    if justified:
        lines += ["## Use-case justification", ""]
        lines.append(
            "Concepts matched by the use case: "
            + (", ".join(f"`{c}`" for c in resolution.use_case_concepts) or "none")
            + "."
        )
        lines.append("")
        unjustified = [n for n in resolution.needs if n.unjustified]
        if unjustified:
            lines += ["| Need | Action | Not justified by the use case |", "| --- | --- | --- |"]
            for n in unjustified:
                lines.append(f"| {n.need} | {n.unjustified_action} | {_cell(', '.join(n.unjustified))} |")
            lines.append("")

    if access.widenings:
        lines += ["## Widenings", "", "| Need | Reason | Detail |", "| --- | --- | --- |"]
        for w in access.widenings:
            lines.append(f"| {w.need} | {w.reason} | {_cell(w.detail)} |")
        lines.append("")

    if contract.prohibitions:
        lines += ["## Prohibitions", "", "| Prohibition | Rule | Operations forbidden |", "| --- | --- | --- |"]
        for p in contract.prohibitions:
            subject = p.target.describe() if p.target else f"{p.provider} at {p.min_privilege.value if p.min_privilege else 'any'} level or above"
            lines.append(f"| {p.id} | {_cell(p.rationale or subject)} | {len(resolution.forbidden_by.get(p.id, []))} |")
        lines.append("")

    if diagnostics:
        lines += [
            "## Diagnostics",
            "",
            "The source is the stage that reported the finding: the resolver, the catalog, or lowering of the "
            "policy or the boundary.",
            "",
            "| Source | Level | Code | Message |",
            "| --- | --- | --- | --- |",
        ]
        for source, d in diagnostics:
            lines.append(f"| {source} | {d.level} | {d.code} | {_cell(d.message)} |")
        lines.append("")

    if contract.notes:
        lines += ["## Notes", ""] + [f"- {n}" for n in contract.notes] + [""]

    lines += [
        "## Next steps",
        "",
        "1. Review the rules, unknowns, and widenings above.",
        "2. Apply the policy to a sandbox: `openshell policy set <sandbox> --policy policy.yaml --wait`.",
        "3. Check that the policy stays within the boundary with the OpenShell prover: "
        "`openshell-prover check policy.yaml --boundary boundary.yaml`.",
        "4. Run the agent and watch for denials: `openshell logs <sandbox> --since 10m --source sandbox`.",
        "",
        "The breadth score weights are: "
        + ", ".join(f"{k} = {v}" for k, v in SCORE_WEIGHTS.items())
        + ". The score is a heuristic for comparing policies, not a security rating.",
        "",
    ]
    return "\n".join(lines)


def compile_report_data(
    resolution: Resolution,
    context: ReportContext,
) -> dict:
    """The JSON report. ``policy``, ``boundary``, and ``permitted_operations`` describe the emitted files."""
    justified = use_case_justified(resolution)
    needs = to_plain(resolution.needs)
    for need, result in zip(needs, resolution.needs):
        need["notes"] = _need_notes(result, justified)
        if not justified:
            for key in ("justified_by", "unjustified", "unjustified_action"):
                need.pop(key, None)
    return _prune(
        {
            "version": __version__,
            "agent": resolution.contract.agent,
            "sources": resolution.contract.sources,
            "policy": to_plain(context.policy_breadth),
            "boundary": to_plain(context.boundary_breadth),
            "permitted_operations": sorted(context.permitted),
            "needs": needs,
            "unknowns": to_plain(resolution.contract.unknowns),
            "widenings": to_plain(resolution.access.widenings),
            "diagnostics": to_plain(resolution.access.diagnostics),
            "catalog_diagnostics": to_plain(context.catalog_diagnostics),
            "lowering_diagnostics": to_plain(context.lowering_diagnostics),
            "forbidden_operations": resolution.forbidden_operations,
            "forbidden_by_prohibition": resolution.forbidden_by,
            "use_case_justified": justified,
            "use_case_concepts": resolution.use_case_concepts if justified else [],
            "rules": [
                {
                    "effect": r.effect.value,
                    "group": r.group,
                    "clients": list(r.clients.paths),
                    "host": r.endpoint.host,
                    "ports": list(r.endpoint.ports),
                    "endpoint_path": r.endpoint.path or None,
                    "protocol": r.endpoint.protocol.value,
                    "method": r.matcher.method if r.matcher else None,
                    "path": r.matcher.path if r.matcher else None,
                    "tool": r.matcher.tool if r.matcher else None,
                    "privilege": r.privilege.value if r.privilege else None,
                    "operations": r.operations,
                    "reasons": r.reasons,
                    "dropped_by_lowering": True if index in context.dropped_rules else None,
                }
                for index, r in enumerate(resolution.access.rules)
            ],
        }
    )


def render_explain_report(
    source: str,
    access: AccessSet,
    graph: Graph,
    b: Breadth,
    permitted: dict[str, PermittedOperation],
) -> str:
    lines = [f"# Policy explanation: {source}", ""]
    lines += [
        "| Measure | Value |",
        "| --- | --- |",
        f"| Allow rules / deny rules | {b.allow_rules} / {b.deny_rules} |",
        f"| Hosts | {', '.join(b.hosts) or '-'} |",
        f"| Hosts not in the catalog | {', '.join(b.uncatalogued_hosts) or '-'} |",
        f"| Catalogued operations reachable (read / write / admin / unknown) | {_privilege_counts(b)} |",
        f"| Connection-level endpoints / unenforced L7 endpoints / opaque | "
        f"{b.connection_level_endpoints} / {b.unenforced_endpoints} / {b.opaque_endpoints} |",
        f"| Match-all paths / any-method rules / binary globs / wildcard hosts | "
        f"{b.match_all_paths} / {b.any_method_rules} / {b.binary_globs} / {b.wildcard_hosts} |",
        f"| Breadth score (lower is narrower) | {b.score} |",
        "",
    ]
    risky = [p for p in permitted.values() if p.privilege.value in ("write", "admin", "unknown")]
    if risky:
        lines += ["## Write, admin, and unclassified operations reachable", "", "| Operation | Privilege | Summary |", "| --- | --- | --- |"]
        for p in sorted(risky, key=lambda x: (x.privilege.rank, x.operation), reverse=True):
            op = graph.operations[p.operation]
            lines.append(f"| {p.operation} | {p.privilege.value} | {_cell(op.summary)} |")
        lines.append("")
    if access.diagnostics:
        lines += ["## Lint findings", "", "| Level | Code | Message | Where |", "| --- | --- | --- | --- |"]
        for d in access.diagnostics:
            lines.append(f"| {d.level} | {d.code} | {_cell(d.message)} | {_cell(d.pointer)} |")
        lines.append("")
    lines += ["## Rules", ""]
    _render_rules(access, lines)
    return "\n".join(lines)


def deny_rule_count(access: AccessSet) -> int:
    return sum(1 for r in access.rules if r.effect == Effect.DENY)
