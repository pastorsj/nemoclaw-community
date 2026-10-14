# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""retrieve_evidence: NVIDIA Nemotron embed + rerank over Milvus, served over MCP."""

import os

# langchain-nvidia-ai-endpoints usage telemetry stays off unless the operator opts in. The pinned 1.4.3
# sends none; the next release enables it by default. Set here so it precedes any import of the package.
os.environ.setdefault("NVIDIA_USAGE_TELEMETRY_ENABLED", "false")
