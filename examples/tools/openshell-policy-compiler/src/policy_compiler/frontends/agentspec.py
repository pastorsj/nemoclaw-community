# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Agent Spec front end: turn an Oracle Open Agent Spec configuration into a contract.

The configuration is read as raw YAML or JSON, not through ``pyagentspec``, so
that plugin components and spec versions newer than the SDK become unknowns
instead of parse failures. The front end:

1. resolves ``{"$component_ref": id}`` against every ``$referenced_components``
   table in the file and in the reference files (reference files win, as a
   ``pyagentspec`` components registry does);
2. walks every dict that has a ``component_type``, in document order, keeping a
   JSONPath-like pointer such as ``$.tools[0]`` or ``$.nodes[2].tool``;
3. maps each component that reaches the network to a raw-endpoint need, and
   each one whose access it cannot see to an unknown.

A component that is reachable from several places (a shared reference) is
mapped once, at its first pointer. Unresolved references to sensitive values
(``api_key``, ``sensitive_headers``, PEM paths, passwords) never hide a
destination, so they become credential hints rather than unknowns.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from policy_compiler.ir.common import Evidence, EvidenceKind, Protocol
from policy_compiler.ir.contract import (
    Approval,
    Contract,
    Credential,
    Need,
    RawEndpoint,
    Target,
    Unknown,
    UnknownKind,
)
from policy_compiler.paths import has_placeholder, normalize_agentspec_template

REF = "$component_ref"
DEFINITIONS = "$referenced_components"

_SENSITIVE_FIELDS = frozenset(
    {
        "api_key",
        "auth_file_location",
        "ca_file",
        "cert_file",
        "client_secret",
        "credentials",
        "dsn",
        "key_file",
        "password",
        "sensitive_headers",
        "ssl_ca_cert",
        "sslcert",
        "sslkey",
        "sslrootcert",
        "user",
        "wallet_location",
        "wallet_password",
    }
)
"""Fields whose value is a secret. An unresolved reference here hides no destination."""

_STRUCTURAL_TYPES = frozenset(
    {
        "A2AConnectionConfig",
        "Agent",
        "AgentSpecializationParameters",
        "ControlFlowEdge",
        "DataFlowEdge",
        "Flow",
        "InMemoryCollectionDatastore",
        "LlmGenerationConfig",
        "MCPToolSpec",
        "ManagerWorkers",
        "OAuthClientConfig",
        "OAuthConfig",
        "Property",
        "RetryPolicy",
        "SpecializedAgent",
        "Swarm",
        "ToolBox",
    }
)
_STRUCTURAL_SUFFIXES = ("Node", "Transport", "Transform", "AuthConfig")

_HANDLERS = {
    "RemoteTool": "_remote_call",
    "ApiNode": "_remote_call",
    "OpenAiCompatibleConfig": "_openai_compatible",
    "VllmConfig": "_openai_compatible",
    "OllamaConfig": "_openai_compatible",
    "OpenAiConfig": "_openai",
    "OciGenAiConfig": "_oci_genai",
    "GeminiConfig": "_gemini",
    "MCPToolBox": "_mcp",
    "MCPTool": "_mcp",
    "A2AAgent": "_a2a",
    "ServerTool": "_opaque_tool",
    "BuiltinTool": "_opaque_tool",
    "ClientTool": "_client_tool",
}

_UNSUPPORTED_HINTS = {
    "DbmsVectorChainLlmConfig": (
        " Oracle Database makes this model call, so it leaves from the database, not the sandbox;"
        " the sandbox still needs the database connection."
    ),
    "OciAgent": " OCI agents are reached through client_config.service_endpoint.",
}

_REMOTE_MCP_TRANSPORTS = frozenset(
    {"SSETransport", "SSEmTLSTransport", "StreamableHTTPTransport", "StreamableHTTPmTLSTransport"}
)
_MTLS_MCP_TRANSPORTS = frozenset({"SSEmTLSTransport", "StreamableHTTPmTLSTransport"})

_DEFAULT_PORTS = {"http": 80, "https": 443, "postgresql": 5432, "postgres": 5432}
_HTTP = ("http", "https")
_SCHEME_PREFIX = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_HOST_LABEL = re.compile(r"^[a-z0-9_](?:[a-z0-9_-]*[a-z0-9_])?$")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_USERINFO = re.compile(r"^\s*([A-Za-z][A-Za-z0-9+.-]*://)?[^/@?#]*@")


# --- Loading and reference resolution --------------------------------------------------------


def load_agentspec(path: Path, *, reference_files: Sequence[Path] = ()) -> dict:
    """Read an Agent Spec configuration and resolve its component references.

    The result has every resolvable ``$component_ref`` replaced by the component
    or value it names, and no ``$referenced_components`` tables. A component that
    is referenced from several places appears as the same object in each place.
    An unresolved reference stays in place as ``{"$component_ref": id}``.
    """
    return _load(Path(path), [Path(p) for p in reference_files]).tree


@dataclass
class _Loaded:
    tree: dict
    circular: set[str]


def _load(path: Path, reference_files: list[Path]) -> _Loaded:
    root = _read(path)
    if not isinstance(root, dict):
        raise ValueError(f"{path}: an Agent Spec configuration must be a mapping at the top level")
    if set(root) == {DEFINITIONS}:
        raise ValueError(
            f"{path}: contains only $referenced_components; pass it as a reference file instead"
        )
    definitions: dict[str, Any] = {}
    _collect_definitions(root, definitions, set())
    for reference in reference_files:
        content = _read(reference)
        if not isinstance(content, dict):
            raise ValueError(f"{reference}: a reference file must be a mapping at the top level")
        found: dict[str, Any] = {}
        _collect_definitions(content, found, set())
        definitions.update(found)
    resolver = _Resolver(definitions)
    return _Loaded(tree=resolver.resolve(root), circular=resolver.circular)


def _read(path: Path) -> Any:
    suffix = path.suffix.lower()
    if suffix not in {".yaml", ".yml", ".json"}:
        raise ValueError(f"{path}: unsupported file type {suffix!r}; expected .yaml, .yml, or .json")
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text) if suffix == ".json" else yaml.safe_load(text)
    except (yaml.YAMLError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path}: cannot parse the Agent Spec configuration: {exc}") from exc


def _collect_definitions(node: Any, into: dict[str, Any], visited: set[int]) -> None:
    """Gather ``$referenced_components`` entries at any depth. The first definition of an ID wins."""
    if not isinstance(node, (dict, list)) or id(node) in visited:
        return
    visited.add(id(node))
    if isinstance(node, dict):
        table = node.get(DEFINITIONS)
        if isinstance(table, dict):
            for key, value in table.items():
                into.setdefault(str(key), value)
        children = node.values()
    else:
        children = node
    for child in children:
        _collect_definitions(child, into, visited)


class _Resolver:
    """Copy a parsed document, replacing component references by what they name."""

    def __init__(self, definitions: dict[str, Any]) -> None:
        self._definitions = definitions
        self._by_ref: dict[str, Any] = {}
        self._active_refs: set[str] = set()
        self._copies: dict[int, Any] = {}
        self._active_nodes: set[int] = set()
        self.circular: set[str] = set()

    def resolve(self, node: Any) -> Any:
        if not isinstance(node, (dict, list)):
            return node
        if isinstance(node, dict) and REF in node:
            return self._resolve_ref(node[REF])
        key = id(node)
        if key in self._copies:
            return self._copies[key]  # a YAML alias: keep it shared
        if key in self._active_nodes:
            raise ValueError("the Agent Spec configuration contains a recursive YAML alias")
        self._active_nodes.add(key)
        if isinstance(node, dict):
            copy: Any = {k: self.resolve(v) for k, v in node.items() if k != DEFINITIONS}
        else:
            copy = [self.resolve(v) for v in node]
        self._active_nodes.discard(key)
        self._copies[key] = copy
        return copy

    def _resolve_ref(self, ref: Any) -> Any:
        ref_id = str(ref) if isinstance(ref, (str, int)) else None
        if ref_id is None or ref_id not in self._definitions:
            return {REF: ref}
        if ref_id in self._by_ref:
            return self._by_ref[ref_id]
        if ref_id in self._active_refs:
            self.circular.add(ref_id)
            return {REF: ref}
        self._active_refs.add(ref_id)
        value = self.resolve(self._definitions[ref_id])
        self._active_refs.discard(ref_id)
        self._by_ref[ref_id] = value
        return value


# --- Contract construction -------------------------------------------------------------------


def contract_from_agentspec(
    path: Path,
    *,
    default_clients: list[str],
    reference_files: Sequence[Path] = (),
) -> Contract:
    """Build a contract from an Agent Spec configuration.

    ``reference_files`` hold disaggregated components (files whose top level is
    ``$referenced_components``). They are listed in ``sources`` after the main file.
    """
    path = Path(path)
    references = [Path(p) for p in reference_files]
    loaded = _load(path, references)
    root = loaded.tree
    agent = _text(root.get("name")) or path.stem
    builder = _Builder(path, loaded.circular)
    builder.run(root, agent)
    use_case = "\n\n".join(
        text for text in (_text(root.get("description")), _text(root.get("system_prompt"))) if text
    )
    return Contract(
        agent=agent,
        sources=[str(path), *(str(p) for p in references)],
        default_clients=list(default_clients),
        use_case=use_case,
        needs=builder.needs,
        unknowns=builder.unknowns,
        notes=builder.notes,
        referenced_specs=builder.specs,
    )


@dataclass(frozen=True)
class _Url:
    scheme: str
    host: str
    port: int
    path: str


class _BadUrl(ValueError):
    pass


class _Builder:
    def __init__(self, path: Path, circular: set[str]) -> None:
        self.file_name = path.name
        self.source = f"agentspec:{path.name}"
        self.base_dir = path.parent
        self.circular = circular
        self.needs: list[Need] = []
        self.unknowns: list[Unknown] = []
        self.notes: list[str] = []
        self.specs: list[str] = []
        self._visited: set[int] = set()
        self._consumed: set[int] = set()
        self._reported_refs: set[str] = set()

    # -- walking --

    def run(self, root: dict, agent: str) -> None:
        if not isinstance(root.get("component_type"), str):
            self._unknown(
                UnknownKind.UNSUPPORTED_COMPONENT,
                agent,
                "$",
                f"The top level of {self.file_name} has no component_type, so it is not an Agent"
                " Spec component (early drafts used `type`); only nested components that have a"
                " component_type were read.",
            )
        self.walk(root, "$", None)

    def walk(self, node: Any, pointer: str, key: str | None) -> None:
        if not isinstance(node, (dict, list)) or id(node) in self._visited:
            return
        if isinstance(node, list):
            self._visited.add(id(node))
            for index, value in enumerate(node):
                self.walk(value, f"{pointer}[{index}]", key)
            return
        if REF in node:
            self._unresolved(node[REF], pointer, key)
            return
        self._visited.add(id(node))
        component_type = node.get("component_type")
        if isinstance(component_type, str) and id(node) not in self._consumed:
            self._classify(node, component_type, pointer, key)
        for child_key, value in node.items():
            self.walk(value, _child_pointer(pointer, child_key), str(child_key))

    def _classify(self, node: dict, ctype: str, pointer: str, key: str | None) -> None:
        name = _component_name(node, ctype)
        plugin = node.get("component_plugin_name")
        if plugin:
            version = node.get("component_plugin_version")
            self._unknown(
                UnknownKind.UNSUPPORTED_COMPONENT,
                name,
                pointer,
                f"{ctype} '{name}' is a plugin component ({plugin}"
                + (f" {version}" if version else "")
                + "); the compiler cannot tell what it connects to.",
            )
            return
        if ctype in _HANDLERS:
            getattr(self, _HANDLERS[ctype])(node, ctype, name, pointer)
        elif "Postgres" in ctype:
            self._postgres(node, ctype, name, pointer)
        elif "Oracle" in ctype and ("Datastore" in ctype or "Database" in ctype):
            self._oracle(node, ctype, name, pointer)
        elif not _is_structural(ctype):
            self._unsupported(ctype, name, pointer, key)

    # -- REST tools and nodes --

    def _remote_call(self, node: dict, ctype: str, name: str, pointer: str) -> None:
        self._record_spec(node.get("api_spec_uri"))
        notes: list[str] = []
        base = _text(node.get("description")) or f"{ctype} {name}"
        method = _method(node.get("http_method"), notes)
        approval = Approval.CONFIRM if node.get("requires_confirmation") is True else Approval.NONE
        credential = _header_credential(node.get("headers"), node.get("sensitive_headers"))
        raw_url = node.get("url")
        if _is_ref(raw_url):
            return  # reported by the walker as an unresolved reference
        if not isinstance(raw_url, str) or not raw_url.strip():
            self._open_destination(ctype, name, pointer, "it has no url")
            return
        allow_list = node.get("url_allow_list")
        if isinstance(allow_list, list) and not allow_list:
            self.notes.append(
                f"{ctype} '{name}' has an empty url_allow_list, so the runtime rejects every"
                " request it makes; no access was generated for it."
            )
            return
        entries = self._allow_list_entries(allow_list, ctype, name)
        if has_placeholder(_destination_part(raw_url)):
            if not isinstance(allow_list, list):
                self._unknown(
                    UnknownKind.OPEN_DESTINATION,
                    name,
                    pointer,
                    f"{ctype} '{name}' builds its destination from run-time input"
                    f" ({_redact(raw_url)}) and has no url_allow_list, so it can reach any host.",
                )
                return
            for entry_text, entry in entries:
                self._add_need(
                    subject=name,
                    endpoint=RawEndpoint(
                        host=entry.host,
                        port=entry.port,
                        scheme=entry.scheme,
                        protocol=Protocol.REST,
                        method=method,
                        path=entry.path.rstrip("/") + "/**",
                    ),
                    rationale=_compose(
                        base,
                        notes
                        + [
                            "the destination is templated; access is limited to url_allow_list"
                            f" entry {_redact(entry_text)}"
                        ],
                    ),
                    pointer=pointer,
                    approval=approval,
                    credential=credential,
                    path_prefixes=[entry.path],
                )
            return
        try:
            url = _parse_url(normalize_agentspec_template(raw_url), schemes=_HTTP, declared=raw_url)
        except _BadUrl as exc:
            self._open_destination(ctype, name, pointer, str(exc))
            return
        prefixes = [e.path for _, e in entries if (e.scheme, e.host, e.port) == (url.scheme, url.host, url.port)]
        if entries and not prefixes:
            notes.append("no url_allow_list entry matches this URL, so the runtime rejects the call")
        self._add_need(
            subject=name,
            endpoint=RawEndpoint(
                host=url.host,
                port=url.port,
                scheme=url.scheme,
                protocol=Protocol.REST,
                method=method,
                path=url.path,
            ),
            rationale=_compose(base, notes),
            pointer=pointer,
            approval=approval,
            credential=credential,
            path_prefixes=prefixes,
        )

    def _allow_list_entries(self, allow_list: Any, ctype: str, name: str) -> list[tuple[str, _Url]]:
        entries = []
        for entry in allow_list if isinstance(allow_list, list) else []:
            try:
                if not isinstance(entry, str):
                    raise _BadUrl("it is not a string")
                entries.append((entry, _parse_url(entry, schemes=_HTTP)))
            except _BadUrl as exc:
                shown = repr(_redact(entry)) if isinstance(entry, str) else type(entry).__name__
                self.notes.append(f"{ctype} '{name}': ignored url_allow_list entry {shown}: {exc}.")
        return entries

    # -- LLM configurations --

    def _openai_compatible(self, node: dict, ctype: str, name: str, pointer: str) -> None:
        raw_url = node.get("url")
        if _is_ref(raw_url):
            return
        if not isinstance(raw_url, str) or not raw_url.strip():
            self._open_destination(ctype, name, pointer, "it has no url")
            return
        try:
            # Agent Spec adapters prepend http:// to a URL without a scheme.
            url = _parse_url(raw_url, schemes=_HTTP, default_scheme="http")
        except _BadUrl as exc:
            self._open_destination(ctype, name, pointer, str(exc))
            return
        notes: list[str] = []
        path = _openai_path(url.path, node.get("api_type"), notes)
        self._add_need(
            subject=name,
            endpoint=RawEndpoint(host=url.host, port=url.port, scheme=url.scheme, method="POST", path=path),
            rationale=_compose(_text(node.get("description")) or _llm_label(ctype, name, node), notes),
            pointer=pointer,
            credential=_llm_credential(node),
        )

    def _openai(self, node: dict, ctype: str, name: str, pointer: str) -> None:
        endpoint = "/v1/responses" if str(node.get("api_type")) == "responses" else "/v1/chat/completions"
        note = (
            "Agent Spec adapters ignore url for OpenAiConfig and use the OpenAI SDK default"
            " (api.openai.com, or OPENAI_BASE_URL when set)"
        )
        if isinstance(node.get("url"), str):
            note += f"; the declared url {_redact(node['url'])} is not used"
        self._add_need(
            subject=name,
            endpoint=RawEndpoint(host="api.openai.com", port=443, method="POST", path=endpoint),
            rationale=_compose(_text(node.get("description")) or _llm_label(ctype, name, node), [note]),
            pointer=pointer,
            credential=_llm_credential(node),
        )

    def _oci_genai(self, node: dict, ctype: str, name: str, pointer: str) -> None:
        client = node.get("client_config")
        if _is_ref(client):
            return
        endpoint = client.get("service_endpoint") if isinstance(client, dict) else None
        if _is_ref(endpoint):
            return
        if not isinstance(endpoint, str) or not endpoint.strip():
            self._open_destination(ctype, name, pointer, "it has no client_config.service_endpoint")
            return
        try:
            url = _parse_url(endpoint, schemes=_HTTP, default_scheme="https")
        except _BadUrl as exc:
            self._open_destination(ctype, name, pointer, str(exc))
            return
        notes = []
        if isinstance(client, dict) and client.get("auth_type") in {"INSTANCE_PRINCIPAL", "RESOURCE_PRINCIPAL"}:
            notes.append("principal authentication also contacts OCI identity endpoints, which are not included")
        self._add_need(
            subject=name,
            endpoint=RawEndpoint(host=url.host, port=url.port, scheme=url.scheme),
            rationale=_compose(_text(node.get("description")) or _llm_label(ctype, name, node), notes),
            pointer=pointer,
        )

    def _gemini(self, node: dict, ctype: str, name: str, pointer: str) -> None:
        auth = node.get("auth")
        if isinstance(auth, dict) and auth.get("component_type") == "GeminiVertexAIAuthConfig":
            location = auth.get("location") if isinstance(auth.get("location"), str) else "global"
            host = "aiplatform.googleapis.com" if location == "global" else f"{location}-aiplatform.googleapis.com"
            how = "Vertex AI host inferred from auth.location; Google OAuth token endpoints are not included"
        else:
            host = "generativelanguage.googleapis.com"
            how = "Gemini API host inferred, because GeminiConfig has no url field"
            if _is_ref(auth):
                how += "; auth is an unresolved reference, so AI Studio (not Vertex AI) was assumed"
        if not _valid_host(host):
            self._open_destination(ctype, name, pointer, f"the inferred host {host!r} is not valid")
            return
        self._add_need(
            subject=name,
            endpoint=RawEndpoint(host=host, port=443),
            rationale=_compose(_text(node.get("description")) or _llm_label(ctype, name, node), [how]),
            pointer=pointer,
            evidence_kind=EvidenceKind.INFERRED,
            evidence_note=how,
        )

    # -- MCP --

    def _mcp(self, node: dict, ctype: str, name: str, pointer: str) -> None:
        transport = node.get("client_transport")
        if _is_ref(transport):
            return
        if not isinstance(transport, dict):
            self._open_destination(ctype, name, pointer, "it has no client_transport")
            return
        self._consumed.add(id(transport))  # this handler reports on the transport
        transport_type = transport.get("component_type")
        if transport_type == "StdioTransport" and not transport.get("component_plugin_name"):
            parts = [transport.get("command"), *(transport.get("args") or [])]
            command = " ".join(str(p) for p in parts if isinstance(p, (str, int, float)))
            self._unknown(
                UnknownKind.OPAQUE_IMPL,
                name,
                pointer,
                f"{ctype} '{name}' starts a local MCP server over stdio with `{command}`;"
                " the server's own network access is not visible to the compiler.",
            )
            return
        if transport_type not in _REMOTE_MCP_TRANSPORTS or transport.get("component_plugin_name"):
            self._unknown(
                UnknownKind.UNSUPPORTED_COMPONENT,
                name,
                pointer,
                f"{ctype} '{name}' uses MCP transport type {transport_type!r}, which the compiler"
                " does not support, so the MCP server is unknown.",
            )
            return
        raw_url = transport.get("url")
        if _is_ref(raw_url):
            return
        if not isinstance(raw_url, str):
            self._open_destination(ctype, name, pointer, "its transport has no url")
            return
        try:
            url = _parse_url(raw_url, schemes=_HTTP)
        except _BadUrl as exc:
            self._open_destination(ctype, name, pointer, str(exc))
            return

        notes: list[str] = []
        tool_filter = node.get("tool_filter")
        if ctype == "MCPToolBox":
            specs = tool_filter if isinstance(tool_filter, list) else []
            tools = tuple(_mcp_tool_name(item) for item in specs if _mcp_tool_name(item))
            confirm = node.get("requires_confirmation") is True or any(
                isinstance(item, dict) and item.get("requires_confirmation") is True for item in specs
            )
        else:
            tools = (name,)
            confirm = node.get("requires_confirmation") is True
        if transport_type in _MTLS_MCP_TRANSPORTS:
            credential: Credential | None = Credential("mtls", reference="key_file, cert_file")
        else:
            credential = _header_credential(transport.get("headers"), transport.get("sensitive_headers"))
        if credential is None and isinstance(transport.get("auth"), dict):
            credential = Credential("header:Authorization", reference="auth")
            notes.append("the transport uses OAuth; authorization-server hosts are not included")
        if str(transport_type).startswith("SSE"):
            notes.append(
                "with the SSE transport the server names the path for client messages at run time,"
                " and it can differ from the URL path"
            )
        self._add_need(
            subject=name,
            endpoint=RawEndpoint(
                host=url.host,
                port=url.port,
                scheme=url.scheme,
                protocol=Protocol.MCP,
                path=url.path,
                mcp_tools=tools,
            ),
            rationale=_compose(_text(node.get("description")) or f"{ctype} {name}", notes),
            pointer=pointer,
            approval=Approval.CONFIRM if confirm else Approval.NONE,
            credential=credential,
        )
        if ctype == "MCPToolBox" and not isinstance(tool_filter, list):
            self._unknown(
                UnknownKind.OPEN_TOOLSET,
                name,
                pointer,
                f"MCPToolBox '{name}' has no tool_filter, so it exposes every tool that the MCP"
                f" server at {_redact(raw_url)} offers.",
            )

    # -- remote agents --

    def _a2a(self, node: dict, ctype: str, name: str, pointer: str) -> None:
        raw_url = node.get("agent_url")
        if _is_ref(raw_url):
            return
        if not isinstance(raw_url, str) or not raw_url.strip():
            self._open_destination(ctype, name, pointer, "it has no agent_url")
            return
        try:
            url = _parse_url(raw_url, schemes=_HTTP)
        except _BadUrl as exc:
            self._open_destination(ctype, name, pointer, str(exc))
            return
        connection = node.get("connection_config")
        connection = connection if isinstance(connection, dict) else {}
        if connection.get("key_file") is not None or connection.get("cert_file") is not None:
            credential: Credential | None = Credential("mtls", reference="connection_config.key_file")
        else:
            credential = _header_credential(connection.get("headers"), None)
        notes = [
            "A2A clients also fetch the agent card (GET /.well-known/agent-card.json), and the card"
            " can name another URL"
        ]
        if connection.get("verify") is False:
            notes.append("TLS certificate verification is disabled (connection_config.verify: false)")
        self._add_need(
            subject=name,
            endpoint=RawEndpoint(host=url.host, port=url.port, scheme=url.scheme, method="POST", path=url.path),
            rationale=_compose(_text(node.get("description")) or f"{ctype} {name}", notes),
            pointer=pointer,
            credential=credential,
        )

    # -- tools without a declared destination --

    def _opaque_tool(self, node: dict, ctype: str, name: str, pointer: str) -> None:
        text = (
            f"{ctype} '{name}' runs inside the agent runtime, so the compiler cannot see which"
            " hosts it contacts."
        )
        if ctype == "BuiltinTool":
            details = [f"{field}: {node[field]}" for field in ("tool_type", "executor_name") if node.get(field)]
            if details:
                text += f" Built-in tool ({', '.join(details)})."
        description = _text(node.get("description"))
        if description:
            text += f" Tool description: {description}"
        self._unknown(UnknownKind.OPAQUE_IMPL, name, pointer, text)

    def _client_tool(self, node: dict, ctype: str, name: str, pointer: str) -> None:
        self.notes.append(
            f"ClientTool '{name}' ({pointer}) runs in the client application, outside the sandbox,"
            " so it needs no sandbox network access."
        )

    # -- datastores --

    def _postgres(self, node: dict, ctype: str, name: str, pointer: str) -> None:
        connection = node.get("connection_config")
        if isinstance(connection, dict) and "component_type" in connection:
            self._consumed.add(id(connection))  # reported once, as part of the datastore
        raw_url = node.get("url")
        if raw_url is None and isinstance(connection, dict):
            if _is_ref(connection):
                return
            raw_url = connection.get("url")
        if _is_ref(raw_url):
            return
        if not isinstance(raw_url, str) or not raw_url.strip():
            self._open_destination(ctype, name, pointer, "it has no connection url")
            return
        try:
            url = _parse_url(raw_url, schemes=("postgresql", "postgres"), default_scheme="postgresql")
        except _BadUrl as exc:
            self._open_destination(ctype, name, pointer, str(exc))
            return
        self._add_need(
            subject=name,
            endpoint=RawEndpoint(host=url.host, port=url.port, scheme=url.scheme, protocol=Protocol.TCP),
            rationale=_text(node.get("description")) or f"{ctype} {name}",
            pointer=pointer,
        )

    def _oracle(self, node: dict, ctype: str, name: str, pointer: str) -> None:
        connection = node.get("connection_config")
        if isinstance(connection, dict) and "component_type" in connection:
            self._consumed.add(id(connection))
        self._unknown(
            UnknownKind.UNRESOLVED_REFERENCE,
            name,
            pointer,
            f"{ctype} '{name}' connects to Oracle Database through a DSN, which is a sensitive"
            " field that Agent Spec does not export, so the database host is unknown.",
        )

    # -- unknowns --

    def _unsupported(self, ctype: str, name: str, pointer: str, key: str | None) -> None:
        if key in {"llm_config", "llm"}:
            text = f"LLM configuration type {ctype} ('{name}') is not supported, so the model endpoint is unknown."
        else:
            text = (
                f"Component type {ctype} ('{name}') is not supported by the Agent Spec front end,"
                " so its network access is unknown."
            )
        self._unknown(UnknownKind.UNSUPPORTED_COMPONENT, name, pointer, text + _UNSUPPORTED_HINTS.get(ctype, ""))

    def _unresolved(self, ref: Any, pointer: str, key: str | None) -> None:
        if key in _SENSITIVE_FIELDS:
            return  # a secret value, not a destination
        ref_id = str(ref)
        if ref_id in self._reported_refs:
            return
        self._reported_refs.add(ref_id)
        if ref_id in self.circular:
            text = f"Reference {ref_id!r} at {pointer} is circular: the component contains a reference to itself."
        else:
            text = (
                f"Reference {ref_id!r} at {pointer} is not defined in $referenced_components of this"
                " file or of any reference file; pass the file that defines it as a reference file."
            )
        self._unknown(UnknownKind.UNRESOLVED_REFERENCE, ref_id, pointer, text)

    def _open_destination(self, ctype: str, name: str, pointer: str, reason: str) -> None:
        self._unknown(
            UnknownKind.OPEN_DESTINATION,
            name,
            pointer,
            f"{ctype} '{name}' has no destination the compiler can determine: {reason}.",
        )

    # -- output helpers --

    def _evidence(self, pointer: str, kind: EvidenceKind = EvidenceKind.DECLARED, note: str = "") -> Evidence:
        return Evidence(kind, self.source, pointer, note)

    def _add_need(
        self,
        *,
        subject: str,
        endpoint: RawEndpoint,
        rationale: str,
        pointer: str,
        approval: Approval = Approval.NONE,
        credential: Credential | None = None,
        path_prefixes: list[str] | None = None,
        evidence_kind: EvidenceKind = EvidenceKind.DECLARED,
        evidence_note: str = "",
    ) -> None:
        self.needs.append(
            Need(
                id=f"need-{len(self.needs) + 1:02d}-{_slug(subject)}",
                target=Target(raw=endpoint),
                rationale=rationale,
                evidence=[self._evidence(pointer, evidence_kind, evidence_note)],
                approval=approval,
                credential=credential,
                path_prefixes=list(path_prefixes or []),
                subject=subject,
            )
        )

    def _unknown(self, kind: UnknownKind, subject: str, pointer: str, description: str) -> None:
        self.unknowns.append(
            Unknown(
                id=f"unknown-{len(self.unknowns) + 1:02d}-{_slug(subject)}",
                kind=kind,
                subject=subject,
                description=description,
                evidence=[self._evidence(pointer)],
            )
        )

    def _record_spec(self, value: Any) -> None:
        if not isinstance(value, str) or not value.strip():
            return
        text = value.strip()
        if has_placeholder(text):
            self.notes.append(f"api_spec_uri {text} is templated, so it was not imported.")
            return
        entry = text if _SCHEME_PREFIX.match(text) else os.path.normpath(
            os.path.join(os.path.abspath(self.base_dir), text)
        )
        if entry not in self.specs:
            self.specs.append(entry)


# --- small helpers ---------------------------------------------------------------------------


def _is_ref(value: Any) -> bool:
    return isinstance(value, dict) and REF in value


def _is_structural(ctype: str) -> bool:
    return (
        ctype in _STRUCTURAL_TYPES
        or ctype.endswith(_STRUCTURAL_SUFFIXES)
        or "Message" in ctype
        or ctype.startswith("OciClientConfig")
    )


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _component_name(node: dict, ctype: str) -> str:
    for field in ("name", "id"):
        value = node.get(field)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()
    return ctype


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:40].rstrip("-") or "component"


def _child_pointer(pointer: str, key: Any) -> str:
    text = str(key)
    if _IDENTIFIER.match(text):
        return f"{pointer}.{text}"
    escaped = text.replace("\\", "\\\\").replace("'", "\\'")
    return f"{pointer}['{escaped}']"


def _compose(base: str, notes: list[str]) -> str:
    text = base.strip()
    for note in notes:
        if text and text[-1] not in ".!?":
            text += "."
        text += f" Note: {note}."
    return text


def _redact(url: str) -> str:
    """Hide any user name or password in a URL before it appears in a description."""
    return _USERINFO.sub(lambda m: (m.group(1) or "") + "***@", url, count=1)


def _destination_part(url: str) -> str:
    """Return the scheme, host, and port of a (possibly templated) URL, without user info."""
    separator = url.find("://")
    scheme, rest = (url[:separator], url[separator + 3 :]) if separator != -1 else ("", url)
    ends = [i for i in (rest.find("/"), rest.find("?"), rest.find("#")) if i != -1]
    authority = rest[: min(ends)] if ends else rest
    return f"{scheme}://{authority.rsplit('@', 1)[-1]}"


def _valid_host(host: str) -> bool:
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    labels = host.rstrip(".").split(".")
    return all(_HOST_LABEL.match(label) for label in labels)


def _parse_url(
    value: str,
    *,
    schemes: tuple[str, ...],
    default_scheme: str | None = None,
    declared: str | None = None,
) -> _Url:
    """Parse a URL into scheme, host, port, and path, or raise ``_BadUrl`` with a reason.

    ``declared`` is the form shown in error messages when ``value`` was normalized.
    """
    shown = _redact(declared if declared is not None else value)
    text = value.strip()
    if not _SCHEME_PREFIX.match(text):
        if default_scheme is None:
            raise _BadUrl(f"the URL {shown!r} has no scheme")
        text = f"{default_scheme}://{text}"
    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme not in schemes:
        raise _BadUrl(f"the URL {shown!r} uses unsupported scheme {scheme!r}")
    host = parts.hostname or ""
    if not _valid_host(host):
        raise _BadUrl(f"the URL {shown!r} has no valid host name")
    try:
        port = parts.port
    except ValueError:
        raise _BadUrl(f"the port in {shown!r} is not a valid number") from None
    return _Url(scheme=scheme, host=host, port=port or _DEFAULT_PORTS[scheme], path=parts.path or "/")


def _method(value: Any, notes: list[str]) -> str:
    if isinstance(value, str) and value.strip():
        if has_placeholder(value):
            notes.append(f"http_method is templated ({value.strip()}), so every method is allowed")
            return "*"
        return value.strip().upper()
    notes.append("http_method is missing or not a string, so every method is allowed")
    return "*"


def _header_credential(headers: Any, sensitive_headers: Any) -> Credential | None:
    """Name the header that carries a secret: Authorization first, then any sensitive header."""
    declared: list[tuple[str, str]] = []
    if isinstance(sensitive_headers, dict) and not _is_ref(sensitive_headers):
        declared += [(str(k), "sensitive_headers") for k in sensitive_headers]
    if isinstance(headers, dict) and not _is_ref(headers):
        declared += [(str(k), "headers") for k in headers]
    for header, field in declared:
        if header.lower() == "authorization":
            return Credential(f"header:{header}", reference=field)
    for header, field in declared:
        if field == "sensitive_headers":
            return Credential(f"header:{header}", reference=field)
    if _is_ref(sensitive_headers):
        return Credential("header:*", reference=str(sensitive_headers[REF]))
    return None


def _llm_credential(node: dict) -> Credential | None:
    if node.get("key_file") is not None or node.get("cert_file") is not None:
        return Credential("mtls", reference=_reference_name(node.get("key_file"), "key_file"))
    if node.get("api_key") is not None:
        return Credential("header:Authorization", reference=_reference_name(node.get("api_key"), "api_key"))
    return None


def _reference_name(value: Any, field: str) -> str:
    return str(value[REF]) if _is_ref(value) else field


def _llm_label(ctype: str, name: str, node: dict) -> str:
    model = node.get("model_id")
    return f"{ctype} {name}" + (f" (model {model})" if isinstance(model, str) and model else "")


def _openai_path(path: str, api_type: Any, notes: list[str]) -> str:
    """Return the request path an OpenAI-compatible client uses for a configured base URL.

    The adapters force a ``/v1`` base: most append ``/v1`` unless the URL already
    ends with it, while the LangGraph adapter replaces the whole path with ``/v1``.
    """
    trimmed = path.rstrip("/")
    if trimmed.endswith(("/chat/completions", "/responses")):
        return trimmed
    endpoint = "/responses" if str(api_type) == "responses" else "/chat/completions"
    if trimmed.endswith("/v1"):
        return trimmed + endpoint
    if trimmed:
        notes.append(
            f"the adapters append /v1 to the URL path {trimmed}; the LangGraph adapter replaces the"
            " path with /v1 instead"
        )
    return f"{trimmed}/v1{endpoint}"


def _mcp_tool_name(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict) and not _is_ref(item) and isinstance(item.get("name"), str):
        return item["name"].strip()
    return ""
