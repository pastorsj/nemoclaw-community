# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
import yaml

from policy_compiler.frontends.agentspec import contract_from_agentspec, load_agentspec
from policy_compiler.ir.common import Evidence, EvidenceKind, Protocol
from policy_compiler.ir.contract import Approval, Contract, Need, RawEndpoint, Unknown, UnknownKind

FIXTURES = Path(__file__).parent / "fixtures" / "agentspec"
CLIENTS = ["/usr/bin/python3"]


def compile_fixture(name: str, **kwargs) -> Contract:
    return contract_from_agentspec(FIXTURES / name, default_clients=CLIENTS, **kwargs)


def need(contract: Contract, need_id: str) -> Need:
    matches = [n for n in contract.needs if n.id == need_id]
    assert matches, f"no need {need_id}; have {[n.id for n in contract.needs]}"
    return matches[0]


def unknown(contract: Contract, unknown_id: str) -> Unknown:
    matches = [u for u in contract.unknowns if u.id == unknown_id]
    assert matches, f"no unknown {unknown_id}; have {[u.id for u in contract.unknowns]}"
    return matches[0]


def raw(item: Need) -> RawEndpoint:
    assert item.target.raw is not None
    return item.target.raw


def unknown_summary(contract: Contract) -> list[tuple[str, UnknownKind, str]]:
    return [(u.id, u.kind, u.evidence[0].pointer) for u in contract.unknowns]


# --- GitHub agent: RemoteTool, ServerTool, ClientTool, OpenAI-compatible LLM ---------------


def test_github_contract_metadata():
    contract = compile_fixture("github_agent.yaml")
    assert contract.agent == "GitHub Explorer"
    assert contract.use_case == (
        "Answers questions about the NVIDIA/OpenShell repository on GitHub.\n\n"
        "Read the code in NVIDIA/OpenShell and explain how it works."
    )
    assert contract.sources == [str(FIXTURES / "github_agent.yaml")]
    assert contract.default_clients == CLIENTS
    assert contract.referenced_specs == [
        os.path.normpath(os.path.abspath(FIXTURES / "specs" / "github-subset.yaml")),
        "https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/"
        "api.github.com/api.github.com.yaml",
    ]


def test_github_ids_follow_walk_order():
    contract = compile_fixture("github_agent.yaml")
    assert [n.id for n in contract.needs] == [
        "need-01-nemotron-on-build-nvidia-com",
        "need-02-get-repository",
        "need-03-get-file-contents",
        "need-04-update-issue",
    ]
    assert [u.id for u in contract.unknowns] == ["unknown-01-summarize-code"]


def test_openai_compatible_llm_on_build_nvidia_com():
    contract = compile_fixture("github_agent.yaml")
    item = need(contract, "need-01-nemotron-on-build-nvidia-com")
    assert raw(item) == RawEndpoint(
        host="integrate.api.nvidia.com",
        port=443,
        scheme="https",
        protocol=Protocol.REST,
        method="POST",
        path="/v1/chat/completions",
    )
    assert item.subject == "Nemotron on build.nvidia.com"
    assert item.evidence == [
        Evidence(EvidenceKind.DECLARED, "agentspec:github_agent.yaml", "$.llm_config")
    ]
    assert item.credential is not None
    assert item.credential.placement == "header:Authorization"


def test_remote_tool_with_literal_url():
    contract = compile_fixture("github_agent.yaml")
    item = need(contract, "need-02-get-repository")
    assert raw(item) == RawEndpoint(
        host="api.github.com",
        port=443,
        scheme="https",
        protocol=Protocol.REST,
        method="GET",
        path="/repos/NVIDIA/OpenShell",
    )
    assert item.rationale == "Fetch metadata for the NVIDIA/OpenShell repository."
    assert item.subject == "get_repository"
    assert item.evidence[0].pointer == "$.tools[0]"
    assert item.approval is Approval.NONE
    assert item.path_prefixes == []
    assert item.credential is not None
    assert item.credential.placement == "header:authorization"
    assert "github_token" not in repr(item.credential)


def test_remote_tool_template_path_and_allow_list_prefixes():
    contract = compile_fixture("github_agent.yaml")
    item = need(contract, "need-03-get-file-contents")
    assert raw(item).method == "GET"
    assert raw(item).path == "/repos/NVIDIA/OpenShell/contents/{path}"
    # Only the entry with the same scheme and host becomes a prefix.
    assert item.path_prefixes == ["/repos/NVIDIA/OpenShell/"]
    # Sensitive headers were exported as a reference, so the header name is unknown.
    assert item.credential is not None
    assert item.credential.placement == "header:*"
    assert item.evidence[0].pointer == "$.tools[1]"


def test_remote_tool_templated_method_and_confirmation():
    contract = compile_fixture("github_agent.yaml")
    item = need(contract, "need-04-update-issue")
    assert raw(item).method == "*"
    assert raw(item).path == "/repos/NVIDIA/OpenShell/issues/{number}"
    assert item.rationale.startswith("RemoteTool Update Issue")
    assert "http_method" in item.rationale
    assert item.approval is Approval.CONFIRM
    assert item.credential is not None
    assert item.credential.placement == "header:Authorization"


def test_server_tool_is_opaque_unknown():
    contract = compile_fixture("github_agent.yaml")
    item = unknown(contract, "unknown-01-summarize-code")
    assert item.kind is UnknownKind.OPAQUE_IMPL
    assert item.subject == "summarize_code"
    assert "Summarize a source file with an internal code-search service." in item.description
    assert item.evidence == [
        Evidence(EvidenceKind.DECLARED, "agentspec:github_agent.yaml", "$.tools[3]")
    ]


def test_client_tool_produces_only_a_note():
    contract = compile_fixture("github_agent.yaml")
    assert not any(n.subject == "ask_user" for n in contract.needs)
    assert not any(u.subject == "ask_user" for u in contract.unknowns)
    assert any("ask_user" in note and "client application" in note for note in contract.notes)


def test_sensitive_value_references_are_not_unknowns():
    contract = compile_fixture("github_agent.yaml")
    assert all(u.kind is not UnknownKind.UNRESOLVED_REFERENCE for u in contract.unknowns)


# --- Templated destinations ------------------------------------------------------------------


def test_templated_host_with_allow_list_emits_one_need_per_entry():
    contract = compile_fixture("templated_destinations.yaml")
    first = need(contract, "need-02-fetch-doc")
    second = need(contract, "need-03-fetch-doc")
    assert raw(first) == RawEndpoint(
        host="docs.example.com", port=443, scheme="https", method="GET", path="/api/**"
    )
    assert first.path_prefixes == ["/api/"]
    assert raw(second) == RawEndpoint(
        host="intranet.example.com", port=8080, scheme="http", method="GET", path="/**"
    )
    assert second.path_prefixes == ["/"]
    assert first.evidence[0].pointer == second.evidence[0].pointer == "$.tools[0]"
    assert first.rationale.startswith("RemoteTool fetch_doc")


def test_templated_or_unparsable_destination_without_allow_list_is_open():
    contract = compile_fixture("templated_destinations.yaml")
    assert unknown_summary(contract) == [
        ("unknown-01-fetch-any-url", UnknownKind.OPEN_DESTINATION, "$.tools[1]"),
        ("unknown-02-fetch-port", UnknownKind.OPEN_DESTINATION, "$.tools[2]"),
        ("unknown-03-local-benefits", UnknownKind.OPEN_DESTINATION, "$.tools[3]"),
    ]
    assert "HOST_PORT" in unknown(contract, "unknown-03-local-benefits").description
    subjects = {n.subject for n in contract.needs}
    assert not subjects & {"fetch_any_url", "fetch_port", "local_benefits"}


def test_empty_allow_list_grants_nothing():
    contract = compile_fixture("templated_destinations.yaml")
    assert not any(n.subject == "blocked_tool" for n in contract.needs)
    assert any("blocked_tool" in note and "url_allow_list" in note for note in contract.notes)


def test_openai_config_uses_api_openai_com():
    contract = compile_fixture("templated_destinations.yaml")
    item = need(contract, "need-01-gpt-5")
    assert raw(item) == RawEndpoint(
        host="api.openai.com", port=443, scheme="https", method="POST", path="/v1/responses"
    )
    assert "ignore" in item.rationale and "url" in item.rationale
    assert contract.use_case == "Reads documentation pages."


# --- MCP -------------------------------------------------------------------------------------


def test_mcp_needs():
    contract = compile_fixture("mcp_agent.yaml")
    assert [n.id for n in contract.needs] == [
        "need-01-local-llama",
        "need-02-get-weather",
        "need-03-github-mcp",
        "need-04-payslip-mcp-toolbox",
        "need-05-secure-mcp",
    ]
    assert raw(need(contract, "need-01-local-llama")) == RawEndpoint(
        host="localhost", port=11434, scheme="http", method="POST", path="/v1/chat/completions"
    )
    weather = need(contract, "need-02-get-weather")
    assert raw(weather) == RawEndpoint(
        host="weather.example.com",
        port=443,
        scheme="https",
        protocol=Protocol.MCP,
        path="/mcp",
        mcp_tools=("get_weather",),
    )
    assert weather.evidence[0].pointer == "$.tools[0]"

    github = need(contract, "need-03-github-mcp")
    assert raw(github) == RawEndpoint(
        host="api.githubcopilot.com",
        port=443,
        scheme="https",
        protocol=Protocol.MCP,
        path="/mcp/",
        mcp_tools=("get_file_contents", "search_code"),
    )
    assert github.approval is Approval.CONFIRM
    assert github.credential is not None and github.credential.placement == "header:*"
    assert github.rationale.startswith("Read code through the GitHub MCP server.")

    payslip = need(contract, "need-04-payslip-mcp-toolbox")
    assert raw(payslip) == RawEndpoint(
        host="localhost", port=8080, scheme="http", protocol=Protocol.MCP, path="/sse"
    )
    assert payslip.evidence[0].pointer == "$.toolboxes[1]"

    secure = need(contract, "need-05-secure-mcp")
    assert raw(secure) == RawEndpoint(
        host="mcp.secure.example.com",
        port=8443,
        scheme="https",
        protocol=Protocol.MCP,
        path="/mcp",
        mcp_tools=("lookup",),
    )
    assert secure.credential is not None and secure.credential.placement == "mtls"


def test_mcp_unknowns():
    contract = compile_fixture("mcp_agent.yaml")
    assert unknown_summary(contract) == [
        ("unknown-01-payslip-mcp-toolbox", UnknownKind.OPEN_TOOLSET, "$.toolboxes[1]"),
        ("unknown-02-filesystem-mcp", UnknownKind.OPAQUE_IMPL, "$.toolboxes[2]"),
    ]
    stdio = unknown(contract, "unknown-02-filesystem-mcp")
    assert "npx" in stdio.description
    assert "@modelcontextprotocol/server-filesystem" in stdio.description
    assert not any(n.subject == "Filesystem MCP" for n in contract.needs)


# --- LLM configurations ----------------------------------------------------------------------


def test_llm_config_needs():
    contract = compile_fixture("llm_configs.yaml")
    assert contract.agent == "Model Zoo"
    assert contract.use_case == "Routes questions to agents that use different model providers."
    assert [n.id for n in contract.needs] == [
        "need-01-vllm-local",
        "need-02-oci-genai",
        "need-03-gemini-studio",
        "need-04-gemini-vertex",
        "need-05-gateway",
    ]
    vllm = need(contract, "need-01-vllm-local")
    assert raw(vllm) == RawEndpoint(
        host="vllm.internal", port=8000, scheme="http", method="POST", path="/v1/chat/completions"
    )
    assert vllm.evidence[0].pointer == "$.group_manager.llm_config"

    oci = need(contract, "need-02-oci-genai")
    assert raw(oci) == RawEndpoint(
        host="inference.generativeai.us-chicago-1.oci.oraclecloud.com", port=443, scheme="https"
    )
    assert raw(oci).method is None and raw(oci).path is None
    assert oci.evidence[0].pointer == "$.workers[0].llm_config"

    studio = need(contract, "need-03-gemini-studio")
    assert raw(studio) == RawEndpoint(host="generativelanguage.googleapis.com", port=443)
    assert studio.evidence[0].kind is EvidenceKind.INFERRED

    vertex = need(contract, "need-04-gemini-vertex")
    assert raw(vertex) == RawEndpoint(host="us-central1-aiplatform.googleapis.com", port=443)
    assert vertex.evidence[0].kind is EvidenceKind.INFERRED

    gateway = need(contract, "need-05-gateway")
    assert raw(gateway) == RawEndpoint(
        host="llm-gateway.example.com",
        port=443,
        scheme="https",
        method="POST",
        path="/api/v2/v1/responses",
    )
    assert gateway.credential is not None and gateway.credential.placement == "mtls"


def test_unsupported_llm_config_is_unknown():
    contract = compile_fixture("llm_configs.yaml")
    assert unknown_summary(contract) == [
        ("unknown-01-in-database-llm", UnknownKind.UNSUPPORTED_COMPONENT, "$.workers[3].llm_config"),
    ]
    assert "DbmsVectorChainLlmConfig" in unknown(contract, "unknown-01-in-database-llm").description


# --- Swarm: A2A, datastores, built-in and plugin components ----------------------------------


def test_swarm_needs():
    contract = compile_fixture("swarm.yaml")
    assert contract.agent == "Support Swarm"
    assert [n.id for n in contract.needs] == [
        "need-01-openai",
        "need-02-summary-cache",
        "need-03-billing-agent",
        "need-04-shipping-agent",
    ]
    # The OpenAI config is shared by the agent and both transforms; it is listed once.
    assert need(contract, "need-01-openai").evidence[0].pointer == "$.first_agent.llm_config"

    postgres = need(contract, "need-02-summary-cache")
    assert raw(postgres) == RawEndpoint(
        host="db.internal.example.com", port=6543, scheme="postgresql", protocol=Protocol.TCP
    )
    assert raw(postgres).method is None and raw(postgres).path is None
    assert postgres.subject == "summary cache"
    assert postgres.evidence[0].pointer == "$.first_agent.transforms[0].datastore"

    billing = need(contract, "need-03-billing-agent")
    assert raw(billing) == RawEndpoint(
        host="agents.example.com", port=443, scheme="https", method="POST", path="/a2a/billing"
    )
    assert billing.rationale.startswith("Handles billing questions.")
    assert billing.credential is not None
    assert billing.credential.placement == "header:Authorization"
    assert billing.evidence[0].pointer == "$.relationships[0][1]"

    shipping = need(contract, "need-04-shipping-agent")
    assert raw(shipping) == RawEndpoint(
        host="10.0.0.5", port=9000, scheme="http", method="POST", path="/"
    )
    assert shipping.evidence[0].pointer == "$.relationships[1][1]"


def test_swarm_unknowns():
    contract = compile_fixture("swarm.yaml")
    assert unknown_summary(contract) == [
        ("unknown-01-oracle-cache", UnknownKind.UNRESOLVED_REFERENCE, "$.first_agent.transforms[1].datastore"),
        ("unknown-02-code-interpreter", UnknownKind.OPAQUE_IMPL, "$.first_agent.tools[0]"),
        ("unknown-03-my-custom-component", UnknownKind.UNSUPPORTED_COMPONENT, "$.first_agent.tools[1]"),
        ("unknown-04-oci-agent", UnknownKind.UNSUPPORTED_COMPONENT, "$.relationships[2][1]"),
    ]
    assert "Runs Python code." in unknown(contract, "unknown-02-code-interpreter").description
    assert "MyCustomComponentPlugin" in unknown(contract, "unknown-03-my-custom-component").description


# --- Flow with in-file references ------------------------------------------------------------


def test_flow_with_shared_references():
    contract = compile_fixture("flow_with_refs.yaml")
    assert [n.id for n in contract.needs] == [
        "need-01-lookup-order",
        "need-02-send-email",
        "need-03-helper-llm",
    ]
    lookup = need(contract, "need-01-lookup-order")
    assert raw(lookup) == RawEndpoint(
        host="orders.internal", port=8080, scheme="http", method="POST", path="/orders/{order_id}"
    )
    assert lookup.rationale == "ApiNode lookup_order"
    assert lookup.evidence[0].pointer == "$.nodes[1]"
    assert lookup.credential is not None and lookup.credential.placement == "header:X-Api-Key"

    # The tool is used by a ToolNode and by an agent; it is listed once, at first use.
    send = need(contract, "need-02-send-email")
    assert send.evidence[0].pointer == "$.nodes[2].tool"
    assert raw(send) == RawEndpoint(
        host="mail.example.com", port=443, scheme="https", method="POST", path="/v1/messages"
    )

    helper = need(contract, "need-03-helper-llm")
    assert raw(helper) == RawEndpoint(
        host="vllm.internal", port=8000, scheme="http", method="POST", path="/v1/chat/completions"
    )
    assert helper.evidence[0].pointer == "$.nodes[3].agent.llm_config"

    # A plugin node is never treated as structural.
    assert unknown_summary(contract) == [
        ("unknown-01-regex", UnknownKind.UNSUPPORTED_COMPONENT, "$.nodes[4]"),
    ]


# --- Disaggregated configurations ------------------------------------------------------------


def test_disaggregated_without_reference_file():
    contract = compile_fixture("disaggregated_main.yaml")
    assert contract.needs == []
    assert contract.use_case == "You are a helpful weather assistant."
    assert unknown_summary(contract) == [
        ("unknown-01-llm-config", UnknownKind.UNRESOLVED_REFERENCE, "$.llm_config"),
        ("unknown-02-client-weather-tool", UnknownKind.UNRESOLVED_REFERENCE, "$.tools[0]"),
        ("unknown-03-forecast-url", UnknownKind.UNRESOLVED_REFERENCE, "$.tools[1].url"),
        ("unknown-04-missing-tool", UnknownKind.UNRESOLVED_REFERENCE, "$.tools[2]"),
    ]


def test_disaggregated_with_reference_file():
    reference = FIXTURES / "disaggregated_components.json"
    contract = compile_fixture("disaggregated_main.yaml", reference_files=[reference])
    assert [n.id for n in contract.needs] == ["need-01-llm-dev", "need-02-get-forecast"]
    llm = need(contract, "need-01-llm-dev")
    assert raw(llm) == RawEndpoint(
        host="dev.llm.url", port=80, scheme="http", method="POST", path="/v1/chat/completions"
    )
    assert llm.evidence[0].pointer == "$.llm_config"
    forecast = need(contract, "need-02-get-forecast")
    assert raw(forecast) == RawEndpoint(
        host="api.weather.example.com", port=443, scheme="https", method="GET", path="/v1/forecast/{city}"
    )
    assert forecast.evidence[0].pointer == "$.tools[1]"
    assert unknown_summary(contract) == [
        ("unknown-01-missing-tool", UnknownKind.UNRESOLVED_REFERENCE, "$.tools[2]"),
    ]
    assert "missing_tool" in contract.unknowns[0].description
    assert any("get_weather" in note for note in contract.notes)
    assert contract.sources == [str(FIXTURES / "disaggregated_main.yaml"), str(reference)]


def test_reference_files_override_in_file_definitions(tmp_path):
    main = tmp_path / "main.yaml"
    main.write_text(
        yaml.safe_dump(
            {
                "component_type": "Agent",
                "name": "override",
                "llm_config": {"$component_ref": "llm"},
                "$referenced_components": {
                    "llm": {"component_type": "VllmConfig", "name": "llm", "url": "http://dev.example.com:8000"}
                },
            }
        )
    )
    prod = tmp_path / "prod.yaml"
    prod.write_text(
        yaml.safe_dump(
            {
                "$referenced_components": {
                    "llm": {"component_type": "VllmConfig", "name": "llm", "url": "https://prod.example.com"}
                }
            }
        )
    )
    contract = contract_from_agentspec(main, default_clients=CLIENTS, reference_files=[prod])
    assert [raw(n).host for n in contract.needs] == ["prod.example.com"]


def test_root_without_component_type_is_unknown(tmp_path):
    path = tmp_path / "legacy.yaml"
    path.write_text(yaml.safe_dump({"id": "x", "type": "Agent", "name": "legacy agent"}))
    contract = contract_from_agentspec(path, default_clients=CLIENTS)
    assert contract.agent == "legacy agent"
    assert unknown_summary(contract) == [
        ("unknown-01-legacy-agent", UnknownKind.UNSUPPORTED_COMPONENT, "$"),
    ]


def test_placeholder_in_userinfo_is_not_a_templated_destination(tmp_path):
    path = tmp_path / "userinfo.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "component_type": "Agent",
                "name": "userinfo",
                "tools": [
                    {
                        "component_type": "RemoteTool",
                        "name": "basic_auth",
                        "url": "https://bot:{{password}}@api.example.com/v2/items/{{item}}",
                        "http_method": "DELETE",
                    }
                ],
            }
        )
    )
    contract = contract_from_agentspec(path, default_clients=CLIENTS)
    assert contract.unknowns == []
    assert [raw(n) for n in contract.needs] == [
        RawEndpoint(host="api.example.com", port=443, scheme="https", method="DELETE", path="/v2/items/{item}")
    ]


def test_user_info_is_redacted_from_descriptions(tmp_path):
    path = tmp_path / "secrets.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "component_type": "Agent",
                "name": "secrets",
                "tools": [
                    {
                        "component_type": "RemoteTool",
                        "name": "allow_listed",
                        "url": "https://{{host}}/x",
                        "http_method": "GET",
                        "url_allow_list": ["https://bot:hunter2@docs.example.com/x/", "ftp://bot:hunter2@x"],
                    },
                    {
                        "component_type": "RemoteTool",
                        "name": "open",
                        "url": "https://bot:hunter2@{{host}}/x",
                        "http_method": "GET",
                    },
                ],
            }
        )
    )
    contract = contract_from_agentspec(path, default_clients=CLIENTS)
    assert len(contract.needs) == 1 and len(contract.unknowns) == 1
    text = repr(contract)
    assert "hunter2" not in text
    assert "***@docs.example.com" in contract.needs[0].rationale


def test_user_info_is_redacted_from_a_url_without_scheme(tmp_path):
    path = tmp_path / "llm.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "component_type": "Agent",
                "name": "llm",
                "llm_config": {"component_type": "VllmConfig", "name": "v", "url": "bot:hunter2@vllm:port"},
            }
        )
    )
    contract = contract_from_agentspec(path, default_clients=CLIENTS)
    assert [u.kind for u in contract.unknowns] == [UnknownKind.OPEN_DESTINATION]
    assert "hunter2" not in contract.unknowns[0].description


def test_unparsable_url_description_shows_the_declared_url():
    contract = compile_fixture("templated_destinations.yaml")
    description = unknown(contract, "unknown-03-local-benefits").description
    assert "http://127.0.0.1:HOST_PORT/benefits/{{task}}" in description


def test_circular_reference_becomes_unknown(tmp_path):
    main = tmp_path / "loop.yaml"
    main.write_text(
        yaml.safe_dump(
            {
                "component_type": "Agent",
                "name": "loop",
                "tools": [{"$component_ref": "a"}],
                "$referenced_components": {
                    "a": {"component_type": "Agent", "name": "inner", "tools": [{"$component_ref": "a"}]}
                },
            }
        )
    )
    contract = contract_from_agentspec(main, default_clients=CLIENTS)
    assert unknown_summary(contract) == [
        ("unknown-01-a", UnknownKind.UNRESOLVED_REFERENCE, "$.tools[0].tools[0]"),
    ]
    assert "circular" in contract.unknowns[0].description


# --- load_agentspec --------------------------------------------------------------------------


def test_load_agentspec_resolves_references_and_drops_definitions():
    data = load_agentspec(
        FIXTURES / "disaggregated_main.yaml",
        reference_files=[FIXTURES / "disaggregated_components.json"],
    )
    assert "$referenced_components" not in data
    assert data["llm_config"]["url"] == "http://dev.llm.url"
    assert data["tools"][0]["component_type"] == "ClientTool"
    assert data["tools"][1]["url"] == "https://api.weather.example.com/v1/forecast/{{city}}"
    assert data["tools"][2] == {"$component_ref": "missing_tool"}


def test_load_agentspec_shares_reused_components():
    data = load_agentspec(FIXTURES / "flow_with_refs.yaml")
    assert data["start_node"] is data["nodes"][0]
    assert data["nodes"][2]["tool"] is data["nodes"][3]["agent"]["tools"][0]


def test_load_agentspec_reads_json(tmp_path):
    path = tmp_path / "agent.json"
    path.write_text(json.dumps({"component_type": "Agent", "name": "json agent", "tools": []}))
    assert load_agentspec(path) == {"component_type": "Agent", "name": "json agent", "tools": []}


def test_load_agentspec_rejects_unknown_suffix(tmp_path):
    path = tmp_path / "agent.toml"
    path.write_text("name = 'x'\n")
    with pytest.raises(ValueError, match="agent.toml"):
        load_agentspec(path)


def test_load_agentspec_rejects_a_components_only_file():
    with pytest.raises(ValueError, match="reference"):
        load_agentspec(FIXTURES / "disaggregated_components.json")


# --- Invariants across every fixture ---------------------------------------------------------

_YAML_FIXTURES = sorted(p.name for p in FIXTURES.glob("*.yaml"))


@pytest.mark.parametrize("name", _YAML_FIXTURES)
def test_every_item_has_evidence_subject_and_sequential_id(name):
    contract = compile_fixture(name)
    for prefix, items in (("need", contract.needs), ("unknown", contract.unknowns)):
        for index, item in enumerate(items, start=1):
            assert re.fullmatch(rf"{prefix}-{index:02d}-[a-z0-9]+(-[a-z0-9]+)*", item.id), item.id
            assert item.subject
            assert item.evidence and item.evidence[0].source == f"agentspec:{name}"
            assert item.evidence[0].pointer.startswith("$")
    for item in contract.needs:
        assert item.clients == []
        assert item.rationale
