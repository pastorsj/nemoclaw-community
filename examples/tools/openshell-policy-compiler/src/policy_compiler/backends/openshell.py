# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""OpenShell back end: lower an access set to a policy, and lift a policy back.

``lower`` turns an :class:`~policy_compiler.ir.access.AccessSet` into an
OpenShell sandbox policy (a plain ``dict`` ready for :func:`dump_policy_yaml`).
``lift`` reads an existing policy, a NemoClaw preset, or a bare
``network_policies`` fragment into an access set and lints it. Lowering a lifted
policy with ``normalize=False`` reproduces the authored policy after
:func:`canonicalize`.

Matcher keys
------------
The :class:`~policy_compiler.ir.access.Matcher` field names are the OpenShell
key names, so no renaming is needed. Sources: the OpenShell docs
``docs/how-it-works/policies/schema.mdx`` (REST ``method``/``path``/``query``
at "REST Rules"; GraphQL ``operation_type``/``operation_name``/``fields`` at
"GraphQL Rules"; MCP ``method``/``tool`` at "MCP Rules"; JSON-RPC ``method`` at
"JSON-RPC Rules") and ``L7Allow``/``L7DenyRule`` in
``crates/openshell-policy-schema/src/lib.rs``, which also defines the SQL
``command`` key. Allow matchers are wrapped as ``rules: [{allow: {...}}]``;
deny matchers are listed directly under ``deny_rules``.

Forms that the matcher cannot hold verbatim are lifted to equivalent matchers:

* ``tool: {any: [a, b]}`` becomes one matcher per alternative (``tool: a`` and
  ``tool: b``). Tool names are single values, so this is exact.
* ``params: {name: x}`` becomes ``tool: x``; the schema reference documents
  ``params.name`` as the lower-level equivalent of ``tool``.
* ``query: {k: {any: [a, b]}}`` becomes the repeated pairs ``(k, a), (k, b)``
  in ``Matcher.query``; :func:`lower` turns a repeated key back into ``any``.

Other matcher forms (for example nested ``params``) are not modelled. They are
reported as ``unsupported-matcher-field`` and handled in the safe direction: an
allow matcher that uses one is dropped (the lifted policy allows less), and a
deny matcher loses only that field (it denies more). A ``null``, wrongly typed,
or unknown matcher field is an ``invalid-matcher`` error: an allow matcher with
one is dropped, and a deny matcher with one also drops the endpoint's allow
rules, so the endpoint fails closed. :func:`canonicalize` treats the equivalent
authored forms above as equal.

Failing closed
--------------
``lower(..., normalize=True)`` never emits a value OpenShell rejects and never
widens access to make an endpoint valid. It reports what it removes in
``access.diagnostics``:

* ``tls`` other than ``skip``, unknown endpoint fields, and malformed
  ``credential_binding`` values are removed. An opaque endpoint is emitted as
  L4 with ``tls: skip``, because ``tls: skip`` cannot carry a request protocol.
* Empty deny rules match nothing in OpenShell's rego and are dropped
  (``dropped-deny-rule``).
* An endpoint is dropped (``endpoint-dropped-fail-closed``) when its deny rules
  cannot be enforced (no request inspection), when an inspected endpoint has
  no allow rules or valid ``access`` preset left (for MCP,
  ``mcp.allow_all_known_mcp_methods: true`` also counts), or for ``sql`` with
  ``enforcement: enforce``.
* Allow rules on an endpoint without request inspection (L4, TCP, or opaque)
  have no effect. They are not emitted when a whole-endpoint allow also grants
  the endpoint (``l7-rules-not-enforced``); otherwise the endpoint is dropped.
  ``lift`` adds that whole-endpoint allow, with a ``Widening``, for authored
  uninspected endpoints that carry rules, because OpenShell allows every
  request there. MCP tool allows that a deny on every ``tools/call`` shadows
  are dropped (``shadowed-allow-dropped``).
* ``mcp.allow_all_known_mcp_methods`` counts as a base allow only when an
  allow rule grants the endpoint. Because tool allows narrow ``tools/call``
  under that flag, ``lift`` clears the flag and keeps only deny rules when any
  rule on the endpoint cannot be read (``mcp-allow-all-cleared``).
* When an endpoint holding deny rules is dropped, every emitted endpoint with
  an overlapping host, port, and binary is dropped too, because OpenShell
  applies deny rules across overlapping rules.
* ``endpoint_extras`` never override modelled fields (``extras-override``),
  credential bindings come only from allow rules and must agree
  (``conflicting-credential-binding``), top-level fields OpenShell does not
  define (such as a NemoClaw ``preset``) are dropped, and protocol options are
  dropped from endpoints emitted without a protocol.
* An ``access`` preset and allow matchers never share an endpoint, and
  endpoints whose ``endpoint_extras`` differ are never merged.

Round-trip markers
------------------
``AccessRule.endpoint_extras`` holds endpoint fields the access set does not
model, plus authored values that :func:`lower` must reproduce only when
``normalize=False``: a legacy or templated ``tls``, an invalid ``protocol`` or
``enforcement``. The reserved key ``_omit`` lists generated keys the author did
not write (today only ``enforcement``); keys starting with ``_`` are never
emitted.
"""

from __future__ import annotations

import copy
import fnmatch
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from policy_compiler import paths
from policy_compiler.ir.access import (
    AccessRule,
    AccessSet,
    ClientSet,
    Diagnostic,
    Effect,
    EndpointKey,
    Enforcement,
    Matcher,
    Widening,
)
from policy_compiler.ir.common import Inspection, Protocol

__all__ = ["canonicalize", "dump_policy_yaml", "lift", "load_policy_file", "lower"]

NETWORK_KEYS = ("network_policies", "network_middlewares")
_TOP_LEVEL_KEYS = {"version", "filesystem_policy", "landlock", "process", *NETWORK_KEYS}
"""Top-level fields of PolicyDocument in crates/openshell-policy-schema/src/lib.rs."""
OMIT = "_omit"
"""Reserved ``endpoint_extras`` key: generated keys to drop when ``normalize=False``."""
AUTHORED = "_authored"
"""Reserved ``endpoint_extras`` key: modelled fields whose authored value lift kept for round trips."""
_L7_OPTION_FIELDS = ("mcp", "json_rpc", "persisted_queries", "graphql_persisted_queries", "graphql_max_body_bytes")
"""Protocol options that mean nothing on an endpoint emitted without a request protocol."""

_OPENSHELL_PROTOCOLS = {p.value: p for p in Protocol if p is not Protocol.L4}
_NO_INSPECTION = {Protocol.L4, Protocol.TCP}
_LEGACY_TLS = {"terminate", "passthrough"}
_PRESET_RANK = {"read-only": 1, "read-write": 2, "full": 3}
_MODELLED_ENDPOINT_KEYS = {
    "host",
    "port",
    "ports",
    "path",
    "protocol",
    "tls",
    "enforcement",
    "allowed_ips",
    "access",
    "rules",
    "deny_rules",
    "credential_binding",
}
_OPENSHELL_ENDPOINT_FIELDS = _MODELLED_ENDPOINT_KEYS | {
    # NetworkEndpoint in crates/openshell-policy-schema/src/lib.rs; OpenShell rejects any other field.
    "allow_encoded_slash",
    "websocket_credential_rewrite",
    "request_body_credential_rewrite",
    "allow_uninspected_credentials",
    "persisted_queries",
    "graphql_persisted_queries",
    "graphql_max_body_bytes",
    "credential_signing",
    "signing_service",
    "signing_region",
    "json_rpc",
    "mcp",
}
_TCP_REJECTED_FIELDS = (
    # additional_l7_fields in crates/openshell-supervisor-network/src/l7/mod.rs
    "allow_encoded_slash",
    "websocket_credential_rewrite",
    "request_body_credential_rewrite",
    "persisted_queries",
    "graphql_persisted_queries",
    "graphql_max_body_bytes",
    "json_rpc",
    "mcp",
    "credential_signing",
    "signing_service",
    "signing_region",
)
_ENDPOINT_KEY_ORDER = [
    "host",
    "port",
    "ports",
    "path",
    "protocol",
    "tls",
    "enforcement",
    "allowed_ips",
    "access",
    "rules",
    "deny_rules",
    "credential_binding",
]
_MATCHER_STRING_KEYS = ("method", "path", "operation_type", "operation_name", "command")
_PLACEHOLDER = re.compile(r"__[A-Za-z0-9_]+__|\{\{?\s*[A-Za-z_][A-Za-z0-9_.-]*\s*\}\}?|_PLACEHOLDER\b")
_GLOB_CHARS = re.compile(r"[*?\[]")


# --- loading and dumping ---------------------------------------------------------------


_POLICY_GET_HEADER_LINE = re.compile(r"[A-Z][A-Za-z0-9]*(?: [A-Za-z0-9]+)*:(?:[ \t].*)?")
"""One ``Key: value`` line of ``openshell policy get`` output, such as ``Version: 3`` or ``Config rev: 7``."""


def _strip_policy_get_header(text: str) -> str:
    """Return the policy YAML from saved ``openshell policy get`` output, or ``text`` unchanged.

    ``openshell policy get NAME --base`` (or ``--full``) prints ``Key: value`` lines,
    a line that is exactly ``---``, then the policy YAML. The header is removed only
    when every line before the first ``---`` is blank or a capitalized ``Key: value``
    line, and at least one is not blank. Policy keys are lowercase, so a plain policy
    file, including one that starts with ``---``, is returned unchanged.
    """
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        content = line.rstrip("\r\n")
        if content == "---":
            if any(earlier.strip() for earlier in lines[:index]):
                return "".join(lines[index + 1 :])
            return text
        if content.strip() and not _POLICY_GET_HEADER_LINE.fullmatch(content):
            return text
    return text


def load_policy_file(path: Path) -> dict:
    """Load an OpenShell policy (or preset or fragment) from a YAML or JSON file.

    A YAML file may also be saved ``openshell policy get`` output; its header is skipped.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        data = yaml.safe_load(_strip_policy_get_header(text))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping at the top level, got {type(data).__name__}")
    return data


class _BlockDumper(yaml.SafeDumper):
    """Safe dumper that indents sequences under their parent key."""

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        return super().increase_indent(flow, False)


def dump_policy_yaml(policy: dict, *, header: Iterable[str] = ()) -> str:
    """Render a policy as block-style YAML, keeping key order, with optional comment lines."""
    lines = []
    for line in header:
        if line.startswith("#"):
            lines.append(line)
        else:
            lines.append(f"# {line}" if line else "#")
    body = yaml.dump(
        policy,
        Dumper=_BlockDumper,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=4096,
    )
    prefix = "\n".join(lines) + "\n" if lines else ""
    return prefix + body


# --- canonicalize -----------------------------------------------------------------------


def _port_sort_key(port: Any) -> tuple[int, Any]:
    return (0, port) if isinstance(port, int) else (1, str(port))


def canonicalize(policy: dict) -> dict:
    """Return a comparison form of a policy.

    Mapping keys are sorted recursively, ``port`` is folded into a sorted
    ``ports`` list, a missing entry ``name`` defaults to its key, and empty
    lists and mappings are removed.

    Matchers in ``rules[].allow`` and ``deny_rules[]`` are rewritten into one
    equivalent form: ``params: {name: x}`` becomes ``tool: x`` (the schema
    reference calls ``params.name`` the lower-level equivalent of ``tool``),
    ``tool: {any: [a, b]}`` becomes one entry per tool, a single-element
    ``{any: [v]}`` becomes ``v`` for ``tool`` and ``query`` values, and
    identical entries are kept once. Other values are left unchanged.
    """
    policy = copy.deepcopy(policy)
    for section in NETWORK_KEYS:
        entries = policy.get(section)
        if not isinstance(entries, dict):
            continue
        for key, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            entry.setdefault("name", key)
            if section != "network_policies":
                continue
            for endpoint in entry.get("endpoints") or []:
                if not isinstance(endpoint, dict):
                    continue
                ports = list(endpoint.get("ports") or [])
                if "port" in endpoint:
                    ports.append(endpoint.pop("port"))
                if ports:
                    endpoint["ports"] = sorted(dict.fromkeys(ports), key=_port_sort_key)
                if isinstance(endpoint.get("rules"), list):
                    endpoint["rules"] = _canonical_rules(endpoint["rules"], wrapped=True)
                if isinstance(endpoint.get("deny_rules"), list):
                    endpoint["deny_rules"] = _canonical_rules(endpoint["deny_rules"], wrapped=False)
    return _sort_and_prune(policy)


def _canonical_rules(items: list[Any], *, wrapped: bool) -> list[Any]:
    out: list[Any] = []
    for item in items:
        inner = item.get("allow") if wrapped and isinstance(item, dict) else item
        if not isinstance(inner, dict):
            expanded = [item]
        elif wrapped:
            expanded = [{**item, "allow": m} for m in _canonical_matchers(inner)]
        else:
            expanded = _canonical_matchers(inner)
        out.extend(entry for entry in expanded if entry not in out)
    return out


def _canonical_matchers(matcher: dict[str, Any]) -> list[dict[str, Any]]:
    matcher = dict(matcher)
    params = matcher.get("params")
    if "tool" not in matcher and isinstance(params, dict) and set(params) == {"name"}:
        matcher["tool"] = params["name"]
        del matcher["params"]
    if isinstance(matcher.get("query"), dict):
        matcher["query"] = {name: _collapse_any(value) for name, value in matcher["query"].items()}
    tool = matcher.get("tool")
    if _is_any(tool):
        return [{**matcher, "tool": name} for name in tool["any"]]
    return [matcher]


def _is_any(value: Any) -> bool:
    return isinstance(value, dict) and set(value) == {"any"} and isinstance(value["any"], list) and bool(value["any"])


def _collapse_any(value: Any) -> Any:
    return value["any"][0] if _is_any(value) and len(value["any"]) == 1 else value


def _sort_and_prune(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key in sorted(value, key=str):
            item = _sort_and_prune(value[key])
            if isinstance(item, (list, dict)) and not item:
                continue
            out[key] = item
        return out
    if isinstance(value, list):
        return [_sort_and_prune(item) for item in value]
    return value


# --- lower ----------------------------------------------------------------------------


def lower(access: AccessSet, *, base_policy: dict | None = None, normalize: bool = True) -> dict:
    """Render an access set as an OpenShell policy.

    With ``normalize=True`` the result follows the generated-policy rules and
    fails closed: it never emits a value OpenShell rejects, and it never widens
    access to make an endpoint valid. Content it has to drop is reported in
    ``access.diagnostics`` (``dropped-deny-rule``, ``endpoint-dropped-fail-closed``,
    and so on). Empty or unbounded ``binaries`` raise ``ValueError``.

    With ``normalize=False`` authored values carried in ``endpoint_extras`` are
    reproduced, so a lifted policy lowers back to its original form.
    """
    report = _Reporter(access.diagnostics)
    if base_policy is not None:
        top = {k: v for k, v in base_policy.items() if k not in NETWORK_KEYS}
    else:
        top = {k: v for k, v in access.extras.items() if k not in NETWORK_KEYS}
    middlewares = access.extras.get("network_middlewares")

    version = 1 if normalize else top.get("version", 1)
    out: dict[str, Any] = {"version": version}
    for key, value in top.items():
        if key == "version":
            continue
        if normalize and key not in _TOP_LEVEL_KEYS:
            report(
                "warning",
                "dropped-top-level-field",
                f"OpenShell rejects the top-level field {key!r}; it was dropped",
                key,
            )
            continue
        out[key] = copy.deepcopy(value)
    out["network_policies"] = _lower_network_policies(access, normalize, report)
    if middlewares is not None:
        out["network_middlewares"] = copy.deepcopy(middlewares)
    return out


class _Reporter:
    """Append diagnostics to a list once, so repeated ``lower`` calls do not duplicate them."""

    def __init__(self, sink: list[Diagnostic]) -> None:
        self.sink = sink

    def __call__(self, level: str, code: str, message: str, pointer: str) -> None:
        diagnostic = Diagnostic(level, code, message, pointer)
        if diagnostic not in self.sink:
            self.sink.append(diagnostic)


@dataclass
class _Lowered:
    """One emitted endpoint, with what the cross-entry deny check needs."""

    entry: str
    key: EndpointKey
    clients: ClientSet
    body: dict[str, Any]
    where: str


def _lower_network_policies(access: AccessSet, normalize: bool, report: _Reporter) -> dict[str, Any]:
    by_group: dict[str, list[AccessRule]] = {}
    for rule in access.rules:
        by_group.setdefault(rule.group, []).append(rule)
    order = [g for g in dict.fromkeys(access.group_order) if g in by_group]
    order += [g for g in by_group if g not in order]

    taken = set(order)
    entries: dict[str, tuple[str, ClientSet]] = {}
    emitted: list[_Lowered] = []
    dropped_denies: list[tuple[EndpointKey, ClientSet]] = []
    for group in order:
        rules = by_group[group]
        client_sets = list(dict.fromkeys(rule.clients for rule in rules))
        for index, clients in enumerate(client_sets):
            if index == 0:
                key = group
            else:
                suffix = 2
                while f"{group}_{suffix}" in taken:
                    suffix += 1
                key = f"{group}_{suffix}"
                taken.add(key)
            members = [rule for rule in rules if rule.clients == clients]
            entries[key] = (next((rule.name for rule in members if rule.name), None) or key, clients)
            for endpoint_key, extras, bucket in _endpoint_buckets(members):
                where = next((rule.reasons[0] for rule in bucket if rule.reasons), f"network_policies.{key}")
                body = _lower_endpoint(key, endpoint_key, extras, bucket, normalize, report, where)
                if body is not None:
                    emitted.append(_Lowered(key, endpoint_key, clients, body, where))
                elif any(rule.effect is Effect.DENY for rule in bucket):
                    dropped_denies.append((endpoint_key, clients))

    if normalize:
        emitted = _drop_deny_overlaps(emitted, dropped_denies, report)

    policies: dict[str, Any] = {}
    for key, (name, clients) in entries.items():
        bodies = [item.body for item in emitted if item.entry == key]
        if normalize and not bodies:
            continue  # every endpoint failed closed; the diagnostics say why
        policies[key] = {"name": name, "endpoints": bodies, "binaries": _lower_binaries(key, clients, normalize)}
    return policies


def _drop_deny_overlaps(
    emitted: list[_Lowered], dropped: list[tuple[EndpointKey, ClientSet]], report: _Reporter
) -> list[_Lowered]:
    """Drop endpoints that a dropped deny rule would have restricted.

    OpenShell applies a deny rule to every endpoint that matches the same
    request for the same binary, across policy entries. When an endpoint that
    holds deny rules is dropped, every emitted endpoint with an overlapping
    host, port, and binary loses that deny, so it is dropped too. Dropping
    repeats until nothing more overlaps.
    """
    pending = list(dropped)
    while pending:
        key, clients = pending.pop()
        kept = []
        for item in emitted:
            if _endpoints_overlap(item.key, key) and _clients_overlap(item.clients, clients):
                report(
                    "error",
                    "endpoint-dropped-fail-closed",
                    f"{_label(item.key)}: a deny rule for an overlapping host, port, and binary was dropped, and "
                    "OpenShell applies deny rules across overlapping rules; the endpoint was dropped",
                    item.where,
                )
                if "deny_rules" in item.body:
                    pending.append((item.key, item.clients))
            else:
                kept.append(item)
        emitted = kept
    return emitted


def _endpoints_overlap(a: EndpointKey, b: EndpointKey) -> bool:
    if not set(a.ports) & set(b.ports):
        return False
    if not a.host or not b.host:
        return True  # a hostless endpoint matches any host on its ports
    return a.host.lower() == b.host.lower() or paths.host_matches(a.host, b.host) or paths.host_matches(b.host, a.host)


def _clients_overlap(a: ClientSet, b: ClientSet) -> bool:
    if "any" in (a.mode, b.mode):
        return True
    return any(p == q or fnmatch.fnmatchcase(p, q) or fnmatch.fnmatchcase(q, p) for p in a.paths for q in b.paths)


def _label(key: EndpointKey) -> str:
    return f"{key.host or '*'}:{','.join(str(p) for p in key.ports)}"


def _endpoint_buckets(rules: list[AccessRule]) -> list[tuple[EndpointKey, dict[str, Any], list[AccessRule]]]:
    """Group rules into endpoint entries.

    Rules share an endpoint entry only when they have the same ``EndpointKey``
    and the same ``endpoint_extras``, and are of the same kind: an ``access``
    preset and allow matchers never share an entry, because OpenShell rejects
    an endpoint that combines them. A whole-endpoint allow joins the first
    entry of its endpoint. A deny rule joins every entry with its
    ``EndpointKey``, whatever the extras, so splitting never separates a deny
    from the allows it restricts.
    """

    def base(rule: AccessRule) -> tuple[EndpointKey, str]:
        return rule.endpoint, json.dumps(rule.endpoint_extras, sort_keys=True, default=repr)

    def kind(rule: AccessRule) -> str | None:
        if rule.effect is Effect.ALLOW and rule.access_preset:
            return "access"
        if rule.effect is Effect.ALLOW and rule.matcher is not None:
            return "rules"
        return None

    kinds: dict[tuple[EndpointKey, str], list[str]] = {}
    for rule in rules:
        k = kind(rule)
        if k is not None and k not in kinds.setdefault(base(rule), []):
            kinds[base(rule)].append(k)

    buckets: dict[tuple[tuple[EndpointKey, str], str | None], list[AccessRule]] = {}
    for rule in rules:
        if rule.effect is Effect.DENY:
            continue
        b = base(rule)
        target = (b, kind(rule) or (kinds.get(b) or [None])[0])
        buckets.setdefault(target, []).append(rule)
    for rule in rules:
        if rule.effect is not Effect.DENY:
            continue
        targets = [t for t in buckets if t[0][0] == rule.endpoint] or [(base(rule), None)]
        for target in targets:
            buckets.setdefault(target, []).append(rule)
    # Each entry takes the extras of the rules that define it, not of denies that joined it.
    return [(b[0], copy.deepcopy(members[0].endpoint_extras), members) for (b, _), members in buckets.items()]


def _lower_binaries(key: str, clients: ClientSet, normalize: bool) -> list[dict[str, str]]:
    if clients.mode == "any":
        raise ValueError(f"network policy {key!r}: OpenShell has no form for 'any binary'; list the binaries")
    if clients.mode == "list" and clients.paths:
        return [{"path": path} for path in clients.paths]
    if clients.mode not in {"list", "none"}:
        raise ValueError(f"network policy {key!r}: unknown client set mode {clients.mode!r}")
    if normalize:
        raise ValueError(
            f"network policy {key!r} has no binaries; an empty list allows nothing in OpenShell "
            "and means 'any binary' in some tooling"
        )
    return []


def _lower_endpoint(
    entry_key: str,
    key: EndpointKey,
    endpoint_extras: dict[str, Any],
    rules: list[AccessRule],
    normalize: bool,
    report: _Reporter,
    where: str,
) -> dict[str, Any] | None:
    if any(rule.effect is Effect.DENY and rule.matcher is None for rule in rules):
        raise ValueError(
            f"endpoint {key.host or '*'}:{key.ports}: a whole-endpoint deny has no OpenShell form; "
            "remove the allow rules for the endpoint instead"
        )
    label = _label(key)
    omit = list(endpoint_extras.get(OMIT) or [])
    authored = set(endpoint_extras.get(AUTHORED) or [])
    extras = {k: v for k, v in endpoint_extras.items() if not k.startswith("_")}
    allow_rules = [rule for rule in rules if rule.effect is Effect.ALLOW]
    presets = [rule.access_preset for rule in allow_rules if rule.access_preset]
    preset = max(presets, key=lambda p: _PRESET_RANK.get(p, 0)) if presets else None
    allows = _unique(rule.matcher for rule in allow_rules if rule.matcher is not None)
    denies = _unique(rule.matcher for rule in rules if rule.effect is Effect.DENY and rule.matcher is not None)
    whole = any(rule.matcher is None and not rule.access_preset for rule in allow_rules)
    protocol = key.protocol
    path = key.path

    bindings = list(dict.fromkeys(rule.credential_binding for rule in allow_rules if rule.credential_binding))
    binding = bindings[0] if bindings else None
    if len(bindings) > 1:
        report(
            "error",
            "conflicting-credential-binding",
            f"{label}: allow rules bind different providers ({', '.join(bindings)})"
            + ("; no credential binding was emitted" if normalize else ""),
            where,
        )
        if normalize:
            binding = None

    if normalize:
        plan = _fail_closed(
            key, label, where, extras, authored, preset, allows, denies, report, bool(allow_rules), whole
        )
        if plan is None:
            return None
        protocol, preset, allows, denies = plan
        if protocol is Protocol.TCP:
            removed = [name for name, value in (("path", path), ("credential_binding", binding)) if value]
            removed += [name for name in _TCP_REJECTED_FIELDS if extras.pop(name, None) is not None]
            if removed:
                report(
                    "warning",
                    "dropped-tcp-fields",
                    f"{label}: OpenShell rejects {', '.join(removed)} on protocol tcp; they were dropped",
                    where,
                )
            path, binding = "", None
        elif protocol is Protocol.L4:
            removed = [name for name in _L7_OPTION_FIELDS if extras.pop(name, None) is not None]
            if removed:
                report(
                    "warning",
                    "dropped-l7-options",
                    f"{label}: {', '.join(removed)} have no effect without a request protocol; they were dropped",
                    where,
                )

    endpoint: dict[str, Any] = {}
    if key.host:
        endpoint["host"] = key.host
    if len(key.ports) == 1:
        endpoint["port"] = key.ports[0]
    elif key.ports:
        endpoint["ports"] = list(key.ports)
    if path:
        endpoint["path"] = path
    if protocol is not Protocol.L4:
        endpoint["protocol"] = protocol.value
    if key.inspection is Inspection.OPAQUE:
        endpoint["tls"] = "skip"
    if protocol is not Protocol.TCP:
        endpoint["enforcement"] = key.enforcement.value
    if key.allowed_ips:
        endpoint["allowed_ips"] = list(key.allowed_ips)
    if preset:
        endpoint["access"] = preset
    if allows:
        endpoint["rules"] = [{"allow": _matcher_dict(m)} for m in allows]
    if denies:
        endpoint["deny_rules"] = [_matcher_dict(m) for m in denies]
    if binding:
        endpoint["credential_binding"] = {"provider": binding}
    for field_name, value in extras.items():
        endpoint[field_name] = copy.deepcopy(value)
    if not normalize:
        for field_name in omit:
            endpoint.pop(field_name, None)

    ordered = {k: endpoint[k] for k in _ENDPOINT_KEY_ORDER if k in endpoint}
    ordered.update((k, v) for k, v in endpoint.items() if k not in ordered)
    return ordered


def _fail_closed(
    key: EndpointKey,
    label: str,
    where: str,
    extras: dict[str, Any],
    authored: set[str],
    preset: str | None,
    allows: list[Matcher],
    denies: list[Matcher],
    report: _Reporter,
    has_allow: bool,
    whole: bool,
) -> tuple[Protocol, str | None, list[Matcher], list[Matcher]] | None:
    """Make an endpoint valid for OpenShell without widening it.

    Edits ``extras`` in place. ``has_allow`` says whether any allow rule
    defines the endpoint; ``whole`` whether one of them is a whole-endpoint
    allow. Returns the protocol, preset, and matchers to emit, or ``None``
    when the endpoint must be dropped.
    """

    def drop(reason: str) -> None:
        report("error", "endpoint-dropped-fail-closed", f"{label}: {reason}; the endpoint was dropped", where)

    # Extras never override modelled fields. Lift stores authored values of
    # modelled fields (a legacy tls, an invalid protocol) only for round trips.
    for field_name in [k for k in extras if k in _MODELLED_ENDPOINT_KEYS]:
        value = extras.pop(field_name)
        if field_name not in authored:
            report(
                "error",
                "extras-override",
                f"{label}: endpoint_extras tried to set the modelled field {field_name}={value!r}; it was ignored",
                where,
            )
    removed = [k for k in extras if k not in _OPENSHELL_ENDPOINT_FIELDS]
    for field_name in removed:
        del extras[field_name]
    if removed:
        report(
            "warning",
            "dropped-unknown-field",
            f"{label}: OpenShell rejects the endpoint fields {', '.join(removed)}; they were dropped",
            where,
        )

    protocol = key.protocol
    if key.inspection is Inspection.OPAQUE and protocol not in _NO_INSPECTION:
        protocol = Protocol.L4  # tls: skip cannot be combined with a request protocol

    kept = []
    for matcher in denies:
        if _matcher_dict(matcher):
            kept.append(matcher)
        else:
            report(
                "warning", "dropped-deny-rule", f"{label}: an empty deny rule matches nothing and was dropped", where
            )
    denies = kept

    if preset is not None and (preset not in _PRESET_RANK or protocol in (Protocol.MCP, Protocol.JSON_RPC)):
        report(
            "warning",
            "dropped-access-preset",
            f"{label}: access {preset!r} is not valid for protocol {protocol.value} and was dropped",
            where,
        )
        preset = None

    if protocol in _NO_INSPECTION:
        if denies:
            drop("its deny rules cannot be enforced without request inspection")
            return None
        if allows or preset:
            if not whole:
                drop(
                    "it is not inspected, so its request rules cannot be enforced, and nothing grants the "
                    "whole endpoint"
                )
                return None
            report(
                "warning",
                "l7-rules-not-enforced",
                f"{label}: request rules have no effect without request inspection and were not emitted; "
                "a whole-endpoint allow already grants every request",
                where,
            )
            allows, preset = [], None
        return protocol, preset, allows, denies

    if protocol is Protocol.MCP and any(m.tool is not None for m in allows):
        if any(m.tool is None and m.method and fnmatch.fnmatchcase("tools/call", m.method) for m in denies):
            report(
                "warning",
                "shadowed-allow-dropped",
                f"{label}: a deny rule blocks every tools/call, so the tool allow rules it shadows were dropped",
                where,
            )
            allows = [m for m in allows if m.tool is None]

    mcp_options = extras.get("mcp")
    allow_all_mcp = (
        protocol is Protocol.MCP
        and isinstance(mcp_options, dict)
        and mcp_options.get("allow_all_known_mcp_methods") is True
    )
    if allow_all_mcp and not has_allow:
        drop("mcp.allow_all_known_mcp_methods is set, but no allow rule grants the endpoint")
        return None
    if not (allows or preset or allow_all_mcp):
        if denies:
            drop("it has deny rules but no allow rules or access preset, and allowing everything would fail open")
        else:
            drop(f"protocol {protocol.value} needs allow rules or an access preset, and none remain")
        return None
    if protocol is Protocol.SQL and key.enforcement is Enforcement.ENFORCE:
        drop("OpenShell rejects protocol sql with enforcement: enforce, and audit would stop enforcing the rules")
        return None
    return protocol, preset, allows, denies


def _unique(matchers: Iterable[Matcher]) -> list[Matcher]:
    return list(dict.fromkeys(matchers))


def _matcher_dict(matcher: Matcher) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if matcher.method is not None:
        out["method"] = matcher.method
    if matcher.path is not None:
        out["path"] = matcher.path
    if matcher.query:
        query: dict[str, list[str]] = {}
        for name, glob in matcher.query:
            query.setdefault(name, []).append(glob)
        out["query"] = {name: globs[0] if len(globs) == 1 else {"any": globs} for name, globs in query.items()}
    if matcher.tool is not None:
        out["tool"] = matcher.tool
    if matcher.operation_type is not None:
        out["operation_type"] = matcher.operation_type
    if matcher.operation_name is not None:
        out["operation_name"] = matcher.operation_name
    if matcher.fields:
        out["fields"] = list(matcher.fields)
    if matcher.command is not None:
        out["command"] = matcher.command
    return out


# --- lift -----------------------------------------------------------------------------


def lift(policy: dict, *, source: str = "") -> AccessSet:
    """Read an OpenShell policy, NemoClaw preset, or ``network_policies`` fragment.

    Every endpoint becomes access rules (one per allow matcher, one per deny
    matcher, and one endpoint-level rule when the endpoint uses ``access`` or
    has no allow rules). Non-network sections, including ``network_middlewares``,
    are kept in ``extras``. Lint findings are reported as diagnostics.
    """
    if not isinstance(policy, dict):
        raise ValueError(f"{source or 'policy'}: expected a mapping, got {type(policy).__name__}")
    lifter = _Lifter(source)
    access = lifter.access
    for key, value in policy.items():
        if key != "network_policies":
            access.extras[key] = copy.deepcopy(value)
    if "version" in policy and policy["version"] != 1:
        lifter.diag(
            "error",
            "invalid-version",
            f"version is {policy['version']!r}; OpenShell accepts only version 1",
            "version",
        )
    network = policy.get("network_policies")
    if network is None:
        return access
    if not isinstance(network, dict):
        raise ValueError(f"{source or 'policy'}: network_policies must be a mapping")
    for key, entry in network.items():
        access.group_order.append(str(key))
        lifter.entry(str(key), entry)
    return access


class _Lifter:
    def __init__(self, source: str) -> None:
        self.source = source
        self.access = AccessSet()

    def pointer(self, path: str) -> str:
        return f"{self.source}#{path}"

    def diag(self, level: str, code: str, message: str, path: str) -> None:
        self.access.diagnostics.append(Diagnostic(level, code, message, self.pointer(path)))

    def placeholders(self, value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                self.placeholders(item, f"{path}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                self.placeholders(item, f"{path}[{index}]")
        elif isinstance(value, str) and _PLACEHOLDER.search(value):
            self.diag("info", "placeholder", f"{value!r} looks like a template placeholder", path)

    # -- entries --

    def entry(self, key: str, entry: Any) -> None:
        base = f"network_policies.{key}"
        if not isinstance(entry, dict):
            self.diag("error", "invalid-entry", "network policy entry must be a mapping", base)
            return
        for field_name in entry:
            if field_name not in {"name", "endpoints", "binaries"}:
                self.diag(
                    "error",
                    "unknown-field",
                    f"unknown network policy field {field_name!r} was dropped",
                    f"{base}.{field_name}",
                )
        name = entry.get("name")
        rule_name = str(name) if name is not None and name != key else None
        clients = self.binaries(base, entry.get("binaries"))
        endpoints = entry.get("endpoints") or []
        if not endpoints:
            self.diag(
                "warning", "no-endpoints", "network policy has no endpoints and allows nothing", f"{base}.endpoints"
            )
        for index, endpoint in enumerate(endpoints):
            self.endpoint(key, index, endpoint, clients, rule_name)

    def binaries(self, base: str, binaries: Any) -> ClientSet:
        if binaries is not None and not isinstance(binaries, list):
            self.diag(
                "error",
                "invalid-binaries",
                "binaries must be a list of {path: ...} entries; the rule allows nothing",
                f"{base}.binaries",
            )
            return ClientSet("none")
        if not binaries:
            self.diag(
                "warning",
                "empty-binaries",
                "no binaries: OpenShell allows nothing, but some tooling treats an empty list as any binary",
                f"{base}.binaries",
            )
            return ClientSet("none")
        paths = []
        for index, binary in enumerate(binaries):
            where = f"{base}.binaries[{index}]"
            path = binary.get("path") if isinstance(binary, dict) else binary
            if isinstance(binary, str):
                self.diag(
                    "warning",
                    "binary-string-form",
                    f"binary {binary!r} is a bare string; OpenShell expects {{path: {binary}}}",
                    where,
                )
            if not isinstance(path, str) or not path:
                self.diag("error", "invalid-binary", "binary entries need a non-empty 'path'", where)
                continue
            if _GLOB_CHARS.search(path):
                self.diag(
                    "warning", "binary-glob", f"binary glob {path!r} matches more executables than one path", where
                )
            self.placeholders(path, f"{where}.path")
            paths.append(path)
        return ClientSet("list", tuple(paths)) if paths else ClientSet("none")

    # -- endpoints --

    def endpoint_shape_errors(self, ep: dict) -> list[tuple[str, str]]:
        """Type errors that make an endpoint unreadable. Such an endpoint is skipped, so it allows nothing."""

        def port_value(value: Any) -> bool:
            return isinstance(value, (int, str)) and not isinstance(value, bool)

        checks = {
            "host": lambda v: isinstance(v, str),
            "path": lambda v: isinstance(v, str),
            "port": port_value,
            "ports": lambda v: isinstance(v, list) and all(port_value(p) for p in v),
            "allowed_ips": lambda v: isinstance(v, list) and all(isinstance(ip, str) for ip in v),
            "rules": lambda v: isinstance(v, list),
            "deny_rules": lambda v: isinstance(v, list),
        }
        return [
            (name, f"{name} has invalid value {ep[name]!r}")
            for name, ok in checks.items()
            if name in ep and not ok(ep[name])
        ]

    def endpoint(self, key: str, index: int, ep: Any, clients: ClientSet, name: str | None) -> None:
        where = f"network_policies.{key}.endpoints[{index}]"
        if not isinstance(ep, dict):
            self.diag("error", "invalid-endpoint", "endpoint must be a mapping", where)
            return
        shape_errors = self.endpoint_shape_errors(ep)
        for field_name, problem in shape_errors:
            self.diag(
                "error",
                f"invalid-{field_name.replace('_', '-')}",
                f"{problem}; the endpoint was skipped, so it allows nothing",
                f"{where}.{field_name}",
            )
        if shape_errors:
            return
        extras: dict[str, Any] = {k: copy.deepcopy(v) for k, v in ep.items() if k not in _MODELLED_ENDPOINT_KEYS}
        omit: list[str] = []
        self.placeholders(ep, where)
        for field_name in ep:
            if field_name not in _OPENSHELL_ENDPOINT_FIELDS:
                self.diag(
                    "error",
                    "unknown-field",
                    f"unknown endpoint field {field_name!r}; OpenShell rejects it and normalized output drops it",
                    f"{where}.{field_name}",
                )

        host = ep.get("host") or ""
        if "*" in host:
            self.diag("info", "wildcard-host", f"wildcard host {host!r} matches many names", f"{where}.host")
        ports = list(ep.get("ports") or [])
        if "port" in ep and ep["port"] not in ports:
            ports.append(ep["port"])

        rules = ep.get("rules") or []
        deny_rules = ep.get("deny_rules") or []
        access_preset = ep.get("access") or None
        protocol = self.protocol(ep, where, extras, has_l7=bool(rules or deny_rules or access_preset))
        inspection = self.tls(ep, where, extras)
        enforcement = self.enforcement(ep, where, extras, omit, protocol)
        if omit:
            extras[OMIT] = omit
        mcp_options = ep.get("mcp")
        allow_all = (
            protocol is Protocol.MCP
            and isinstance(mcp_options, dict)
            and mcp_options.get("allow_all_known_mcp_methods") is True
        )

        if protocol in _NO_INSPECTION:
            what = "native TCP" if protocol is Protocol.TCP else "no request protocol"
            target = f"{host or '*'}:{','.join(str(p) for p in ports)}"
            self.diag("warning", "l4-only", f"{what}: every request to {target} is allowed", where)
        if access_preset is not None and access_preset not in _PRESET_RANK:
            self.diag("error", "invalid-access", f"unknown access preset {access_preset!r}", f"{where}.access")
        if access_preset == "full":
            self.diag("warning", "access-full", "access: full allows every method and path", f"{where}.access")
        if protocol is Protocol.SQL and enforcement is Enforcement.ENFORCE:
            self.diag(
                "error",
                "sql-enforce",
                "OpenShell rejects protocol sql with enforcement: enforce; SQL rules can only be audited",
                f"{where}.enforcement",
            )

        binding = ep.get("credential_binding")
        provider = None
        if isinstance(binding, dict) and set(binding) == {"provider"} and isinstance(binding["provider"], str):
            provider = binding["provider"]
        elif binding is not None:
            extras["credential_binding"] = copy.deepcopy(binding)
            self.diag(
                "error",
                "invalid-credential-binding",
                "malformed credential_binding (expected {provider: <name>}); normalized output drops it",
                f"{where}.credential_binding",
            )
        authored = sorted(k for k in extras if k in _MODELLED_ENDPOINT_KEYS)
        if authored:
            extras[AUTHORED] = authored

        endpoint = EndpointKey(
            host=host,
            ports=tuple(ports),
            protocol=protocol,
            inspection=inspection,
            enforcement=enforcement,
            allowed_ips=tuple(ep.get("allowed_ips") or ()),
            path=ep.get("path") or "",
        )

        pending: list[tuple[Effect, Matcher | None, str | None]] = []
        fail_closed = False
        allow_dropped = False
        if access_preset or not rules:
            pending.append((Effect.ALLOW, None, access_preset))
        for j, rule in enumerate(rules):
            rule_where = f"{where}.rules[{j}]"
            allow = rule.get("allow") if isinstance(rule, dict) else None
            if not isinstance(allow, dict):
                self.diag(
                    "error", "rule-without-allow", "each rules entry must wrap its matcher in 'allow'", rule_where
                )
                allow_dropped = True
                continue
            for extra_key in [k for k in rule if k != "allow"]:
                self.diag(
                    "error",
                    "unknown-field",
                    f"unknown rules entry field {extra_key!r}; OpenShell rejects it",
                    f"{rule_where}.{extra_key}",
                )
            path = allow.get("path")
            if path in ("/**", "**"):
                self.diag(
                    "warning",
                    "path-wildcard-all",
                    f"allow path {path!r} matches every path",
                    f"{rule_where}.allow.path",
                )
            matchers = self.matchers(allow, f"{rule_where}.allow", deny=False) or []
            allow_dropped = allow_dropped or not matchers
            pending.extend((Effect.ALLOW, matcher, None) for matcher in matchers)
        for j, rule in enumerate(deny_rules):
            rule_where = f"{where}.deny_rules[{j}]"
            matchers = self.matchers(rule, rule_where, deny=True) if isinstance(rule, dict) else None
            if not isinstance(rule, dict):
                self.diag(
                    "error",
                    "invalid-deny-rule",
                    "deny rule must be a mapping; the endpoint's allow rules were dropped so it fails closed",
                    rule_where,
                )
            if matchers is None:
                fail_closed = True
                continue
            pending.extend((Effect.DENY, matcher, None) for matcher in matchers)

        if allow_all and allow_dropped:
            # With allow_all_known_mcp_methods, tool allows narrow tools/call: losing one would widen it.
            fail_closed = True
        if fail_closed:
            # A rule the access set cannot hold must not turn into "allow": keep only the denies.
            pending = [item for item in pending if item[0] is Effect.DENY]
            if allow_all:
                options = {k: v for k, v in extras["mcp"].items() if k != "allow_all_known_mcp_methods"}
                if options:
                    extras["mcp"] = options
                else:
                    del extras["mcp"]
                self.diag(
                    "error",
                    "mcp-allow-all-cleared",
                    "a rule on this endpoint could not be read, so mcp.allow_all_known_mcp_methods was cleared "
                    "and only the deny rules were kept (fail closed)",
                    f"{where}.mcp.allow_all_known_mcp_methods",
                )
        elif (protocol in _NO_INSPECTION or inspection is Inspection.OPAQUE) and (rules or access_preset):
            # OpenShell ignores request rules it cannot inspect: the authored endpoint allows every request.
            pending.insert(0, (Effect.ALLOW, None, None))
            self.access.widenings.append(
                Widening(
                    need=self.pointer(where),
                    reason="uninspected-rules",
                    detail=f"{host or '*'}:{','.join(str(p) for p in ports)} is not inspected, so its request "
                    "rules have no effect and every request is allowed",
                )
            )

        for effect, matcher, preset in pending:
            self.access.rules.append(
                AccessRule(
                    effect=effect,
                    group=key,
                    clients=clients,
                    endpoint=endpoint,
                    matcher=matcher,
                    access_preset=preset,
                    reasons=[self.pointer(where)],
                    credential_binding=provider if effect is Effect.ALLOW else None,
                    name=name,
                    endpoint_extras=copy.deepcopy(extras),
                )
            )

    def protocol(self, ep: dict, where: str, extras: dict, *, has_l7: bool) -> Protocol:
        raw = ep.get("protocol")
        if raw is None:
            return Protocol.L4
        text = str(raw)
        if text == "":
            extras["protocol"] = raw
            return Protocol.L4
        known = _OPENSHELL_PROTOCOLS.get(text.lower())
        if known is not None:
            if text != known.value:
                extras["protocol"] = raw
            return known
        extras["protocol"] = raw
        guess = Protocol.REST if has_l7 else Protocol.L4
        self.diag(
            "error",
            "invalid-protocol",
            f"protocol {text!r} is not an OpenShell protocol (expected one of "
            f"{', '.join(sorted(_OPENSHELL_PROTOCOLS))}); read as {guess.value}",
            f"{where}.protocol",
        )
        return guess

    def tls(self, ep: dict, where: str, extras: dict) -> Inspection:
        raw = ep.get("tls")
        if raw is None or raw == "":
            return Inspection.L7
        if raw == "skip":
            self.diag("warning", "tls-skip", "tls: skip relays traffic without inspection", f"{where}.tls")
            return Inspection.OPAQUE
        extras["tls"] = raw
        if raw in _LEGACY_TLS:
            replacement = "omit the field" if raw == "terminate" else "use tls: skip"
            self.diag(
                "warning",
                "legacy-tls-value",
                f"tls: {raw} is a legacy value that current OpenShell rejects; {replacement}",
                f"{where}.tls",
            )
            if raw == "passthrough":
                self.diag("warning", "tls-skip", "tls: passthrough relays traffic without inspection", f"{where}.tls")
                return Inspection.OPAQUE
            return Inspection.L7
        if not (isinstance(raw, str) and _PLACEHOLDER.search(raw)):
            self.diag("error", "invalid-tls", f"unknown tls value {raw!r} (expected skip or no value)", f"{where}.tls")
        return Inspection.L7

    def enforcement(self, ep: dict, where: str, extras: dict, omit: list[str], protocol: Protocol) -> Enforcement:
        raw = ep.get("enforcement")
        if raw is None:
            omit.append("enforcement")
            if protocol not in _NO_INSPECTION:
                self.diag(
                    "warning",
                    "missing-enforcement",
                    "no enforcement: OpenShell defaults to audit, so the endpoint's L7 rules are logged, not enforced",
                    where,
                )
            return Enforcement.AUDIT
        if protocol is Protocol.TCP:
            extras["enforcement"] = raw  # lower() never emits enforcement for tcp
        if raw in (Enforcement.ENFORCE.value, Enforcement.AUDIT.value):
            return Enforcement(raw)
        extras["enforcement"] = raw
        self.diag("error", "invalid-enforcement", f"unknown enforcement value {raw!r}", f"{where}.enforcement")
        return Enforcement.AUDIT

    # -- matchers --

    def matchers(self, raw: dict, where: str, *, deny: bool) -> list[Matcher] | None:
        """Convert one authored matcher, handling anything it cannot hold in the safe direction.

        A field with an invalid value (``null``, or the wrong type) is an
        ``invalid-matcher`` error. An allow matcher with one is dropped. A deny
        matcher with one returns ``None``; the caller then drops the endpoint's
        allow rules, because guessing what the deny meant could leave a gap.

        A valid but unmodelled field (``unsupported-matcher-field``) drops an
        allow matcher entirely, because removing only the field would widen it.
        A deny matcher loses only that field; every such field (query, tool,
        params) narrows the match, so the deny then matches more requests.
        """
        invalid = [key for key, value in raw.items() if not _valid_matcher_value(key, value)]
        if invalid:
            outcome = (
                "the deny rule cannot be represented, so the endpoint's allow rules were dropped (fail closed)"
                if deny
                else "the allow rule was dropped"
            )
            for key in invalid:
                problem = f"has invalid value {raw[key]!r}" if key in _KNOWN_MATCHER_KEYS else "is not a matcher field"
                self.diag("error", "invalid-matcher", f"matcher field {key!r} {problem}; {outcome}", f"{where}.{key}")
            return None if deny else []

        values: dict[str, Any] = {}
        tools: list[str | None] = [None]
        dropped: list[str] = []

        for key in _MATCHER_STRING_KEYS:
            if key in raw:
                values[key] = str(raw[key])
        query: list[tuple[str, str]] = []
        for name, glob in (raw.get("query") or {}).items():
            alternatives = self.any_of(glob)
            if alternatives is None:
                dropped.append(f"query.{name}")
                continue
            query.extend((str(name), alt) for alt in alternatives)
        if "fields" in raw:
            values["fields"] = tuple(str(f) for f in raw.get("fields") or ())

        tool = raw.get("tool")
        params = raw.get("params")
        if tool is None and isinstance(params, dict) and set(params) == {"name"}:
            tool = params["name"]
        elif params is not None:
            dropped.append("params")
        if tool is not None:
            alternatives = self.any_of(tool)
            if alternatives is None:
                dropped.append("tool")
            else:
                tools = list(alternatives)

        for field_name in dropped:
            outcome = (
                "the field was dropped, so the lifted deny rule matches more requests"
                if deny
                else "the whole allow rule was dropped, so the lifted policy allows less"
            )
            self.diag(
                "warning",
                "unsupported-matcher-field",
                f"matcher field {field_name!r} is not modelled; {outcome}",
                f"{where}.{field_name}",
            )
        if dropped and not deny:
            return []
        return [Matcher(query=tuple(query), tool=t, **values) for t in tools]

    @staticmethod
    def any_of(value: Any) -> list[str] | None:
        """Alternatives of a glob or ``{any: [globs]}`` value, or ``None`` when the form is unsupported."""
        if _scalar(value):
            return [str(value)]
        if _is_any(value) and all(_scalar(v) for v in value["any"]):
            return [str(v) for v in value["any"]]
        return None


_KNOWN_MATCHER_KEYS = {*_MATCHER_STRING_KEYS, "query", "fields", "tool", "params"}


def _scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float)) and not isinstance(value, bool)


def _valid_matcher_value(key: str, value: Any) -> bool:
    """Reject unknown keys, ``null``, and wrongly typed matcher values."""
    if key not in _KNOWN_MATCHER_KEYS or value is None:
        return False
    if key in _MATCHER_STRING_KEYS:
        return _scalar(value)
    if key == "fields":
        return isinstance(value, list) and all(_scalar(item) for item in value)
    if key in ("query", "params"):
        return isinstance(value, dict)
    return _scalar(value) or isinstance(value, dict)  # tool
