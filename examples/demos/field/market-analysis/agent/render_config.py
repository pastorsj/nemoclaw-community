# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Render the Hermes config for the features baked into one agent image.

profile/config.yaml declares every optional tool. This keeps only the selected
features, so a disabled feature has no toolset, no MCP server, no tool and no
visible skill:

    python render_config.py --features retrieval,analytics,kumo profile/config.yaml > config.yaml
"""

import argparse
import copy
import sys
from pathlib import Path

import yaml

# feature -> (MCP server, which is also its Hermes toolset; skill that teaches it)
FEATURES = {
    "retrieval": ("retrieval", "searching-documents"),
    "analytics": ("market_analytics", "analyzing-market-data"),
    "kumo": ("market_analytics", "predicting-with-kumo"),
    "ontology": ("auto_ontology", "querying-auto-ontology"),
}
KUMO_TOOL = "predict_asset_outcomes"  # the kumo feature is one tool on the market_analytics server


def parse_features(value: str) -> set[str]:
    features = {name.strip() for name in value.split(",") if name.strip()}
    if unknown := features - FEATURES.keys():
        raise ValueError(f"unknown agent features {sorted(unknown)}; choose from {sorted(FEATURES)}")
    if "kumo" in features and "analytics" not in features:
        raise ValueError("the kumo feature needs the analytics feature")
    return features


def render(config: dict, features: set[str]) -> dict:
    rendered = copy.deepcopy(config)
    servers = {FEATURES[feature][0] for feature in features}
    rendered["mcp_servers"] = {name: spec for name, spec in rendered["mcp_servers"].items() if name in servers}
    toolsets = rendered["platform_toolsets"]["api_server"]
    rendered["platform_toolsets"]["api_server"] = [t for t in toolsets if t == "skills" or t in servers]
    rendered["skills"]["disabled"] = sorted(FEATURES[f][1] for f in FEATURES.keys() - features)
    if "kumo" not in features and "market_analytics" in servers:
        rendered["mcp_servers"]["market_analytics"]["tools"]["include"].remove(KUMO_TOOL)
    return rendered


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--features", required=True, help="comma-separated: " + ",".join(FEATURES))
    parser.add_argument("config", type=Path, help="profile/config.yaml")
    args = parser.parse_args()
    config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    yaml.safe_dump(render(config, parse_features(args.features)), sys.stdout, sort_keys=False)


if __name__ == "__main__":
    main()
