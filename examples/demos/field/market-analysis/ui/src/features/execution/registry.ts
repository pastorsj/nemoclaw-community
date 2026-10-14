// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The tool registry (`contracts/tool-registry.json`, generated as TOOL_REGISTRY)
 * is the only place the UI learns tool names, labels and explorers. A tool
 * that is not registered still renders, under its event label.
 */

import { TOOL_REGISTRY, type Tool } from '@/generated/tool-registry'

export type { Tool }
export type Family = Tool['family']

const TOOLS_BY_NAME = new Map<string, Tool>(
  TOOL_REGISTRY.tools.flatMap((tool) => [
    [tool.id, tool],
    [tool.hermes_name, tool],
  ])
)

/** The registered tool for an MCP tool name or a Hermes name (`mcp__<server>__<id>`). */
export const toolFor = (name: string | null | undefined): Tool | undefined =>
  name ? TOOLS_BY_NAME.get(name) : undefined

/** The service behind each capability family, drawn as a resource node in the graph. */
export const RESOURCES: Record<Family, { label: string; detail: string }> = {
  unstructured_retrieval: { label: 'Milvus', detail: 'Vector search and rerank' },
  market_analytics: { label: 'Market analytics', detail: 'pandas or RAPIDS workers' },
  structured_retrieval: { label: 'Auto Ontology', detail: 'Ontology-grounded SQL' },
  structured_prediction: { label: 'NVIDIA Kumo', detail: 'Relational predictions' },
}

/** A technology logo drawn on a graph node, served from `public/ecosystem-logos`. */
export interface NodeLogo {
  /** The technology's name, the image's tooltip */
  brand: string
  src: string
}

/**
 * The libraries a registered tool is built on, by tool id, drawn as logos on
 * its tool node. `retrieve_evidence` embeds and reranks through LangChain
 * (langchain-nvidia-ai-endpoints, in tools/retrieval).
 */
export const TOOL_LOGOS: Readonly<Partial<Record<string, readonly NodeLogo[]>>> = {
  retrieve_evidence: [{ brand: 'LangChain', src: '/ecosystem-logos/langchain.svg' }],
}
