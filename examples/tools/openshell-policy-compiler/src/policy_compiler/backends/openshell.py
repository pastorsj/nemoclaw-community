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

Other matcher fields (for example nested ``params``) are not modelled. They are
reported as ``unsupported-matcher-field`` and handled in the safe direction: an
allow matcher that uses one is dropped (the lifted policy allows less), and a
deny matcher loses only that field (it denies more).

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
import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from policy_compiler.ir.access import (
    AccessRule,
    AccessSet,
    ClientSet,
    Diagnostic,
    Effect,
    EndpointKey,
    Enforcement,
    Matcher,
)
from policy_compiler.ir.common import Inspection, Protocol

__all__ = ["canonicalize", "dump_policy_yaml", "lift", "load_policy_file", "lower"]

NETWORK_KEYS = ("network_policies", "network_middlewares")
OMIT = "_omit"
"""Reserved ``endpoint_extras`` key: generated keys to drop when ``normalize=False``."""

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


def load_policy_file(path: Path) -> dict:
    """Load an OpenShell policy (or preset or fragment) from a YAML or JSON file."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    data = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
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
    lists and mappings are removed. Other values are left unchanged.
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
    return _sort_and_prune(policy)


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

    With ``normalize=True`` the result follows the generated-policy rules:
    explicit ``protocol`` and ``enforcement``, no legacy ``tls`` values, and no
    empty or unbounded ``binaries`` (a ``ValueError`` is raised instead). With
    ``normalize=False`` authored values carried in ``endpoint_extras`` are
    reproduced, so a lifted policy lowers back to its original form.
    """
    if base_policy is not None:
        top = {k: v for k, v in base_policy.items() if k not in NETWORK_KEYS}
    else:
        top = {k: v for k, v in access.extras.items() if k not in NETWORK_KEYS}
    middlewares = access.extras.get("network_middlewares")

    version = 1 if normalize else top.get("version", 1)
    out: dict[str, Any] = {"version": version}
    for key, value in top.items():
        if key != "version":
            out[key] = copy.deepcopy(value)
    out["network_policies"] = _lower_network_policies(access, normalize)
    if middlewares is not None:
        out["network_middlewares"] = copy.deepcopy(middlewares)
    return out


def _lower_network_policies(access: AccessSet, normalize: bool) -> dict[str, Any]:
    by_group: dict[str, list[AccessRule]] = {}
    for rule in access.rules:
        by_group.setdefault(rule.group, []).append(rule)
    order = [g for g in dict.fromkeys(access.group_order) if g in by_group]
    order += [g for g in by_group if g not in order]

    taken = set(order)
    policies: dict[str, Any] = {}
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
            policies[key] = _lower_entry(key, members, clients, normalize)
    return policies


def _lower_entry(key: str, rules: list[AccessRule], clients: ClientSet, normalize: bool) -> dict[str, Any]:
    name = next((rule.name for rule in rules if rule.name), None) or key
    endpoints: dict[EndpointKey, list[AccessRule]] = {}
    for rule in rules:
        endpoints.setdefault(rule.endpoint, []).append(rule)
    return {
        "name": name,
        "endpoints": [_lower_endpoint(ep, members, normalize) for ep, members in endpoints.items()],
        "binaries": _lower_binaries(key, clients, normalize),
    }


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


def _lower_endpoint(key: EndpointKey, rules: list[AccessRule], normalize: bool) -> dict[str, Any]:
    endpoint: dict[str, Any] = {}
    if key.host:
        endpoint["host"] = key.host
    if len(key.ports) == 1:
        endpoint["port"] = key.ports[0]
    elif key.ports:
        endpoint["ports"] = list(key.ports)
    if key.path:
        endpoint["path"] = key.path
    if key.protocol is not Protocol.L4:
        endpoint["protocol"] = key.protocol.value
    if key.inspection is Inspection.OPAQUE:
        endpoint["tls"] = "skip"
    if key.protocol is not Protocol.TCP:
        endpoint["enforcement"] = key.enforcement.value
    if key.allowed_ips:
        endpoint["allowed_ips"] = list(key.allowed_ips)

    presets = [rule.access_preset for rule in rules if rule.access_preset]
    if presets:
        endpoint["access"] = max(presets, key=lambda p: _PRESET_RANK.get(p, 0))

    allows = _unique(rule.matcher for rule in rules if rule.effect is Effect.ALLOW and rule.matcher is not None)
    denies = _unique(rule.matcher for rule in rules if rule.effect is Effect.DENY and rule.matcher is not None)
    if allows:
        endpoint["rules"] = [{"allow": _matcher_dict(m)} for m in allows]
    if denies:
        endpoint["deny_rules"] = [_matcher_dict(m) for m in denies]

    binding = next((rule.credential_binding for rule in rules if rule.credential_binding), None)
    if binding:
        endpoint["credential_binding"] = {"provider": binding}

    extras, omit = _merged_extras(rules)
    if normalize:
        extras.pop("protocol", None)
        extras.pop("enforcement", None)
        tls = extras.get("tls")
        if tls == "terminate":
            del extras["tls"]
        elif tls == "passthrough":
            extras["tls"] = "skip"
    for field_name, value in extras.items():
        endpoint[field_name] = copy.deepcopy(value)
    if not normalize:
        for field_name in omit:
            endpoint.pop(field_name, None)

    ordered = {k: endpoint[k] for k in _ENDPOINT_KEY_ORDER if k in endpoint}
    ordered.update((k, v) for k, v in endpoint.items() if k not in ordered)
    return ordered


def _merged_extras(rules: list[AccessRule]) -> tuple[dict[str, Any], list[str]]:
    merged: dict[str, Any] = {}
    omit: list[str] = []
    for rule in rules:
        for key, value in rule.endpoint_extras.items():
            if key == OMIT:
                omit.extend(v for v in value if v not in omit)
            elif not key.startswith("_"):
                merged.setdefault(key, value)
    return merged, omit


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
                self.diag("error", "unknown-field", f"unknown network policy field {field_name!r} was dropped", f"{base}.{field_name}")
        name = entry.get("name")
        rule_name = str(name) if name is not None and name != key else None
        clients = self.binaries(base, entry.get("binaries"))
        endpoints = entry.get("endpoints") or []
        if not endpoints:
            self.diag("warning", "no-endpoints", "network policy has no endpoints and allows nothing", f"{base}.endpoints")
        for index, endpoint in enumerate(endpoints):
            self.endpoint(key, index, endpoint, clients, rule_name)

    def binaries(self, base: str, binaries: Any) -> ClientSet:
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
            if not isinstance(path, str) or not path:
                self.diag("error", "invalid-binary", "binary entries need a non-empty 'path'", where)
                continue
            if _GLOB_CHARS.search(path):
                self.diag("warning", "binary-glob", f"binary glob {path!r} matches more executables than one path", where)
            self.placeholders(path, f"{where}.path")
            paths.append(path)
        return ClientSet("list", tuple(paths)) if paths else ClientSet("none")

    # -- endpoints --

    def endpoint(self, key: str, index: int, ep: Any, clients: ClientSet, name: str | None) -> None:
        where = f"network_policies.{key}.endpoints[{index}]"
        if not isinstance(ep, dict):
            self.diag("error", "invalid-endpoint", "endpoint must be a mapping", where)
            return
        extras: dict[str, Any] = {k: copy.deepcopy(v) for k, v in ep.items() if k not in _MODELLED_ENDPOINT_KEYS}
        omit: list[str] = []
        self.placeholders(ep, where)

        host = ep.get("host") or ""
        if "*" in str(host):
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

        if protocol in _NO_INSPECTION:
            what = "native TCP" if protocol is Protocol.TCP else "no request protocol"
            target = f"{host or '*'}:{','.join(str(p) for p in ports)}"
            self.diag("warning", "l4-only", f"{what}: every request to {target} is allowed", where)
        if access_preset is not None and access_preset not in _PRESET_RANK:
            self.diag("error", "invalid-access", f"unknown access preset {access_preset!r}", f"{where}.access")
        if access_preset == "full":
            self.diag("warning", "access-full", "access: full allows every method and path", f"{where}.access")

        binding = ep.get("credential_binding")
        provider = None
        if isinstance(binding, dict) and set(binding) == {"provider"}:
            provider = binding["provider"]
        elif binding is not None:
            extras["credential_binding"] = copy.deepcopy(binding)

        endpoint = EndpointKey(
            host=host,
            ports=tuple(ports),
            protocol=protocol,
            inspection=inspection,
            enforcement=enforcement,
            allowed_ips=tuple(ep.get("allowed_ips") or ()),
            path=ep.get("path") or "",
        )

        def add(effect: Effect, matcher: Matcher | None, preset: str | None = None) -> None:
            self.access.rules.append(
                AccessRule(
                    effect=effect,
                    group=key,
                    clients=clients,
                    endpoint=endpoint,
                    matcher=matcher,
                    access_preset=preset,
                    reasons=[self.pointer(where)],
                    credential_binding=provider,
                    name=name,
                    endpoint_extras=copy.deepcopy(extras),
                )
            )

        if access_preset or not rules:
            add(Effect.ALLOW, None, access_preset)
        for j, rule in enumerate(rules):
            rule_where = f"{where}.rules[{j}]"
            allow = rule.get("allow") if isinstance(rule, dict) else None
            if not isinstance(allow, dict):
                self.diag("error", "rule-without-allow", "each rules entry must wrap its matcher in 'allow'", rule_where)
                continue
            path = allow.get("path")
            if path in ("/**", "**"):
                self.diag("warning", "path-wildcard-all", f"allow path {path!r} matches every path", f"{rule_where}.allow.path")
            for matcher in self.matchers(allow, f"{rule_where}.allow", deny=False):
                add(Effect.ALLOW, matcher)
        for j, rule in enumerate(deny_rules):
            rule_where = f"{where}.deny_rules[{j}]"
            if not isinstance(rule, dict):
                self.diag("error", "invalid-deny-rule", "deny rule must be a mapping", rule_where)
                continue
            for matcher in self.matchers(rule, rule_where, deny=True):
                add(Effect.DENY, matcher)

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

    def matchers(self, raw: dict, where: str, *, deny: bool) -> list[Matcher]:
        """Convert one authored matcher. Unmodelled fields are handled in the safe direction.

        An allow matcher with an unmodelled field is dropped entirely, because
        removing only the field would widen it. A deny matcher keeps its other
        fields, so it denies at least as much as the authored rule.
        """
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
        dropped += [k for k in raw if k not in {*_MATCHER_STRING_KEYS, "query", "fields", "tool", "params"}]

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
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            return [str(value)]
        if isinstance(value, dict) and set(value) == {"any"} and isinstance(value["any"], list) and value["any"]:
            return [str(v) for v in value["any"]]
        return None
