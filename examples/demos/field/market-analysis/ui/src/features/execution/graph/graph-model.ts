// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The execution graph: one fixed topology of everything a Hermes run can use
 * (tool control, structured data, unstructured data, market analytics,
 * synthesis). Events only change node and edge states, so the layout never
 * moves during replay and capabilities a run did not use stay visible as
 * such.
 */

import type {
  GraphEvent,
  GraphInvocation,
  GraphInvocationStatus,
  GraphProjection,
} from './graph-events'

export type ExecutionNodeState = 'unobserved' | 'pending' | 'running' | 'completed' | 'failed'

export const isInspectableExecutionNodeState = (state: ExecutionNodeState): boolean =>
  state === 'running' || state === 'completed' || state === 'failed'

export type ExecutionNodeId =
  | 'business-question'
  | 'hermes-agent'
  | 'tool-search'
  | 'tool-describe'
  | 'tool-call'
  | 'skills-list'
  | 'skill-view'
  | 'ontology-tool'
  | 'retriever-tool'
  | 'hermes-tools'
  | 'structured-prediction'
  | 'nvidia-kumo'
  | 'nvidia-ontology'
  | 'structured-database'
  | 'structured-retrieval'
  | 'unstructured-retrieval'
  | 'market-scan'
  | 'market-anomaly-scan'
  | 'price-context'
  | 'sentiment-timeline'
  | 'news-price-relationship'
  | 'market-relationship-analysis'
  | 'synthesis'
  | 'report-generation'
  | 'trusted-answer'

export type InspectableExecutionNodeId =
  | 'hermes-agent'
  | 'nvidia-kumo'
  | 'nvidia-ontology'
  | 'structured-retrieval'
  | 'unstructured-retrieval'
  | 'structured-database'
  | 'market-scan'
  | 'market-anomaly-scan'
  | 'price-context'
  | 'sentiment-timeline'
  | 'news-price-relationship'
  | 'market-relationship-analysis'

export type NodeIcon =
  | 'question'
  | 'router'
  | 'model'
  | 'agent'
  | 'tools'
  | 'prediction'
  | 'ontology'
  | 'database'
  | 'table'
  | 'retrieval'
  | 'analytics'
  | 'synthesis'
  | 'report'
  | 'answer'

type Port = 'top' | 'right' | 'bottom' | 'left'

export type ExecutionGraphGroupId =
  | 'tool-control'
  | 'agent-utilities'
  | 'structured-data'
  | 'unstructured-data'
  | 'market-analytics'

export type ExecutionGraphNodeKind = 'input' | 'agent' | 'tool' | 'stage' | 'resource' | 'output'

export type ExecutionGraphGroupDefinition = {
  id: ExecutionGraphGroupId
  label: string
  x: number
  y: number
  width: number
  height: number
  description?: string
}

export type ExecutionGraphNodeDefinition = {
  id: ExecutionNodeId
  label: string
  subtitle: string
  x: number
  y: number
  width?: number
  height?: number
  icon: NodeIcon
  branch:
    | 'orchestration'
    | 'prediction'
    | 'structured'
    | 'unstructured'
    | 'analytics'
    | 'foundation'
  group?: ExecutionGraphGroupId
  kind?: ExecutionGraphNodeKind
  inspectable?: boolean
}

export type ExecutionGraphEdgeDefinition = {
  id: string
  from: ExecutionNodeId
  to: ExecutionNodeId
  label?: string
  fromPort: Port
  toPort: Port
  fromOffset?: number
  toOffset?: number
  via?: ReadonlyArray<readonly [number, number]>
  labelAt?: readonly [number, number]
}

export type ExecutionNodeView = ExecutionGraphNodeDefinition & {
  state: ExecutionNodeState
  count: number
  current: boolean
}

export type ExecutionEdgeView = ExecutionGraphEdgeDefinition & {
  state: ExecutionNodeState
  current: boolean
}

export type ExecutionGraphViewModel = {
  nodes: ExecutionNodeView[]
  edges: ExecutionEdgeView[]
  groups: ExecutionGraphGroupDefinition[]
  canvasWidth: number
  canvasHeight: number
}

export type ExecutionNodeDetail = {
  id: InspectableExecutionNodeId
  label: string
  subtitle: string
  state: ExecutionNodeState
  observed: boolean
  invocations: Array<{
    invocationId: string
    name: string
    status: GraphInvocationStatus
    artifactRefs: string[]
  }>
  artifactRefs: string[]
}

export const EXECUTION_NODE_WIDTH = 180
export const EXECUTION_NODE_HEIGHT = 80

/**
 * The topology is fixed at run start; events only change node and edge state.
 * Direct Hermes tools are drawn apart from the stages and resources behind them.
 */
export const hermesExecutionGraphNodes: readonly ExecutionGraphNodeDefinition[] = [
  {
    id: 'business-question',
    label: 'Business Question',
    subtitle: 'Requested outcome and context',
    x: 40,
    y: 510,
    icon: 'question',
    branch: 'orchestration',
    kind: 'input',
  },
  {
    id: 'hermes-agent',
    label: 'Hermes Agent',
    subtitle: 'Plans, invokes tools, and evaluates results',
    x: 350,
    y: 510,
    width: 220,
    icon: 'agent',
    branch: 'orchestration',
    kind: 'agent',
    inspectable: true,
  },
  {
    id: 'tool-search',
    label: 'Tool Search',
    subtitle: 'Discovers available tool definitions',
    x: 750,
    y: 120,
    icon: 'tools',
    branch: 'orchestration',
    group: 'tool-control',
    kind: 'tool',
  },
  {
    id: 'tool-describe',
    label: 'Tool Describe',
    subtitle: 'Loads a selected tool schema',
    x: 1030,
    y: 120,
    icon: 'tools',
    branch: 'orchestration',
    group: 'tool-control',
    kind: 'tool',
  },
  {
    id: 'tool-call',
    label: 'Tool Invocation',
    subtitle: 'Invokes a selected or directly exposed tool',
    x: 1310,
    y: 120,
    icon: 'tools',
    branch: 'orchestration',
    group: 'tool-control',
    kind: 'tool',
  },
  {
    id: 'hermes-tools',
    label: 'Other Tools',
    subtitle: 'Observed tools outside this manifest',
    x: 1590,
    y: 120,
    width: 220,
    icon: 'tools',
    branch: 'orchestration',
    group: 'tool-control',
    kind: 'tool',
  },
  {
    id: 'skills-list',
    label: 'Skills List',
    subtitle: 'Discovers reviewed agent guidance',
    x: 750,
    y: 1020,
    icon: 'tools',
    branch: 'orchestration',
    group: 'agent-utilities',
    kind: 'tool',
  },
  {
    id: 'skill-view',
    label: 'Skill View',
    subtitle: 'Loads selected skill instructions',
    x: 1030,
    y: 1020,
    icon: 'tools',
    branch: 'orchestration',
    group: 'agent-utilities',
    kind: 'tool',
  },
  {
    id: 'ontology-tool',
    label: 'Ask Ontology',
    subtitle: 'ask_question · MCP tool',
    x: 750,
    y: 420,
    icon: 'tools',
    branch: 'structured',
    group: 'structured-data',
    kind: 'tool',
  },
  {
    id: 'nvidia-ontology',
    label: 'Auto Ontology',
    subtitle: 'Semantic grounding resource',
    x: 1050,
    y: 420,
    icon: 'ontology',
    branch: 'foundation',
    group: 'structured-data',
    kind: 'resource',
    inspectable: true,
  },
  {
    id: 'structured-retrieval',
    label: 'Structured Retrieval',
    subtitle: 'Ontology-grounded SQL result',
    x: 1370,
    y: 420,
    icon: 'table',
    branch: 'structured',
    group: 'structured-data',
    kind: 'stage',
    inspectable: true,
  },
  {
    id: 'structured-database',
    label: 'Structured Database',
    subtitle: 'Selected read-only data source',
    x: 1690,
    y: 420,
    icon: 'database',
    branch: 'foundation',
    group: 'structured-data',
    kind: 'resource',
    inspectable: true,
  },
  {
    id: 'structured-prediction',
    label: 'Structured Prediction',
    subtitle: 'Ontology-grounded PQL result',
    x: 1370,
    y: 690,
    icon: 'prediction',
    branch: 'prediction',
    group: 'structured-data',
    kind: 'stage',
  },
  {
    id: 'nvidia-kumo',
    label: 'NVIDIA Kumo',
    subtitle: 'Relational foundation model',
    x: 1690,
    y: 690,
    icon: 'model',
    branch: 'foundation',
    group: 'structured-data',
    kind: 'resource',
    inspectable: true,
  },
  {
    id: 'retriever-tool',
    label: 'Retrieve Evidence',
    subtitle: 'Milvus · NVIDIA Nemotron',
    x: 1420,
    y: 1020,
    icon: 'tools',
    branch: 'unstructured',
    group: 'unstructured-data',
    kind: 'tool',
  },
  {
    id: 'unstructured-retrieval',
    label: 'Unstructured Retrieval',
    subtitle: 'Candidate passages · reranking · evidence',
    x: 1740,
    y: 1020,
    width: 220,
    icon: 'retrieval',
    branch: 'unstructured',
    group: 'unstructured-data',
    kind: 'stage',
    inspectable: true,
  },
  // The lower market row is offset by half a column. Its centers sit in the
  // gaps between the upper nodes, giving dispatches a clear vertical path.
  // Result lanes use the same left-to-right order below the nodes before
  // entering Synthesis, so the fixed topology remains legible during replay.
  {
    id: 'market-scan',
    label: 'Market Scan',
    subtitle: 'Ranks observed market signals',
    x: 2050,
    y: 410,
    width: 220,
    icon: 'analytics',
    branch: 'analytics',
    group: 'market-analytics',
    kind: 'stage',
    inspectable: true,
  },
  {
    id: 'price-context',
    label: 'Price Context',
    subtitle: 'Summarizes observed price history',
    x: 2360,
    y: 410,
    width: 220,
    icon: 'analytics',
    branch: 'analytics',
    group: 'market-analytics',
    kind: 'stage',
    inspectable: true,
  },
  {
    id: 'market-anomaly-scan',
    label: 'Market Anomaly Scan',
    subtitle: 'Ranks unusual observed feature combinations',
    x: 2670,
    y: 410,
    width: 220,
    icon: 'analytics',
    branch: 'analytics',
    group: 'market-analytics',
    kind: 'stage',
    inspectable: true,
  },
  {
    id: 'sentiment-timeline',
    label: 'Sentiment Timeline',
    subtitle: 'Aggregates observed news sentiment',
    x: 2205,
    y: 650,
    width: 220,
    icon: 'analytics',
    branch: 'analytics',
    group: 'market-analytics',
    kind: 'stage',
    inspectable: true,
  },
  {
    id: 'news-price-relationship',
    label: 'News and Price Relationship',
    subtitle: 'Aligns news with forward returns',
    x: 2515,
    y: 650,
    width: 220,
    icon: 'analytics',
    branch: 'analytics',
    group: 'market-analytics',
    kind: 'stage',
    inspectable: true,
  },
  {
    id: 'market-relationship-analysis',
    label: 'Market Relationships',
    subtitle: 'Ranks observed relationship centrality',
    x: 2825,
    y: 650,
    width: 220,
    icon: 'analytics',
    branch: 'analytics',
    group: 'market-analytics',
    kind: 'stage',
    inspectable: true,
  },
  {
    id: 'synthesis',
    label: 'Synthesis',
    subtitle: 'Combines observed results and evidence',
    x: 3260,
    y: 710,
    width: 220,
    icon: 'synthesis',
    branch: 'orchestration',
    kind: 'stage',
  },
  {
    id: 'report-generation',
    label: 'Response Formatting',
    subtitle: 'Readable Markdown and citations',
    x: 3600,
    y: 710,
    width: 210,
    icon: 'report',
    branch: 'orchestration',
    kind: 'stage',
  },
  {
    id: 'trusted-answer',
    label: 'Answer',
    subtitle: 'Inspectable final response',
    x: 3940,
    y: 710,
    icon: 'answer',
    branch: 'orchestration',
    kind: 'output',
  },
]

export const hermesExecutionGraphGroups: readonly ExecutionGraphGroupDefinition[] = [
  {
    id: 'tool-control',
    label: 'Tool Control',
    description: 'Runtime discovery and invocation tools',
    x: 700,
    y: 55,
    width: 1140,
    height: 190,
  },
  {
    id: 'structured-data',
    label: 'Structured Data',
    description: 'Ontology-grounded retrieval and prediction',
    x: 700,
    y: 350,
    width: 1200,
    height: 485,
  },
  {
    id: 'agent-utilities',
    label: 'Agent Utilities',
    description: 'Reviewed guidance available to Hermes',
    x: 700,
    y: 955,
    width: 590,
    height: 190,
  },
  {
    id: 'unstructured-data',
    label: 'Unstructured Data',
    description: 'Milvus + NVIDIA Nemotron evidence workflow',
    x: 1370,
    y: 955,
    width: 620,
    height: 190,
  },
  {
    id: 'market-analytics',
    label: 'Market Analytics',
    description: 'Snapshot-backed market and news operations',
    x: 1990,
    y: 300,
    width: 1115,
    height: 540,
  },
]

export const HERMES_EXECUTION_CANVAS_WIDTH = 4180
export const HERMES_EXECUTION_CANVAS_HEIGHT = 1200

export const hermesExecutionGraphEdges: readonly ExecutionGraphEdgeDefinition[] = [
  {
    id: 'question-hermes',
    from: 'business-question',
    to: 'hermes-agent',
    label: 'REQUEST',
    fromPort: 'right',
    toPort: 'left',
    labelAt: [285, 542],
  },
  {
    id: 'hermes-tool-search',
    from: 'hermes-agent',
    to: 'tool-search',
    label: 'DISCOVERS TOOLS',
    fromPort: 'right',
    fromOffset: -20,
    toPort: 'bottom',
    via: [
      [600, 530],
      [600, 280],
      [840, 280],
    ],
    labelAt: [720, 272],
  },
  {
    id: 'tool-search-describe',
    from: 'tool-search',
    to: 'tool-describe',
    label: 'SELECTS',
    fromPort: 'right',
    toPort: 'left',
    labelAt: [980, 152],
  },
  {
    id: 'tool-describe-call',
    from: 'tool-describe',
    to: 'tool-call',
    label: 'SCHEMA',
    fromPort: 'right',
    toPort: 'left',
    labelAt: [1260, 152],
  },
  {
    id: 'hermes-tools',
    from: 'hermes-agent',
    to: 'hermes-tools',
    label: 'OTHER TOOL',
    fromPort: 'top',
    fromOffset: 30,
    toPort: 'right',
    via: [
      [490, 40],
      [1880, 40],
      [1880, 160],
    ],
    labelAt: [1185, 32],
  },
  {
    id: 'hermes-skills',
    from: 'hermes-agent',
    to: 'skills-list',
    label: 'DISCOVERS GUIDANCE',
    fromPort: 'bottom',
    fromOffset: -30,
    toPort: 'top',
    via: [
      [430, 910],
      [840, 910],
    ],
    labelAt: [635, 902],
  },
  {
    id: 'skills-list-view',
    from: 'skills-list',
    to: 'skill-view',
    label: 'OPENS',
    fromPort: 'right',
    toPort: 'left',
    labelAt: [980, 1052],
  },
  {
    id: 'tool-call-ontology',
    from: 'tool-call',
    to: 'ontology-tool',
    label: 'DISPATCHES',
    fromPort: 'bottom',
    toPort: 'left',
    via: [
      [1400, 300],
      [620, 300],
      [620, 460],
    ],
    labelAt: [685, 452],
  },
  {
    id: 'ontology-tool-resource',
    from: 'ontology-tool',
    to: 'nvidia-ontology',
    label: 'USES',
    fromPort: 'right',
    toPort: 'left',
    labelAt: [990, 452],
  },
  {
    id: 'ontology-query',
    from: 'nvidia-ontology',
    to: 'structured-retrieval',
    label: 'GROUNDS SQL',
    fromPort: 'right',
    toPort: 'left',
    labelAt: [1300, 452],
  },
  {
    id: 'ontology-predict',
    from: 'nvidia-ontology',
    to: 'structured-prediction',
    label: 'GROUNDS PQL',
    fromPort: 'bottom',
    toPort: 'left',
    via: [[1140, 730]],
    labelAt: [1255, 722],
  },
  {
    id: 'query-source',
    from: 'structured-retrieval',
    to: 'structured-database',
    label: 'READS',
    fromPort: 'right',
    toPort: 'left',
    labelAt: [1630, 452],
  },
  {
    id: 'predict-kumo',
    from: 'structured-prediction',
    to: 'nvidia-kumo',
    label: 'INFERENCE',
    fromPort: 'right',
    toPort: 'left',
    labelAt: [1630, 722],
  },
  {
    id: 'source-kumo',
    from: 'structured-database',
    to: 'nvidia-kumo',
    label: 'HISTORICAL GRAPH',
    fromPort: 'bottom',
    toPort: 'top',
    labelAt: [1840, 600],
  },
  {
    id: 'tool-call-retriever',
    from: 'tool-call',
    to: 'retriever-tool',
    label: 'DISPATCHES',
    fromPort: 'bottom',
    toPort: 'top',
    via: [
      [1400, 300],
      [620, 300],
      [620, 900],
      [1510, 900],
    ],
    labelAt: [1065, 892],
  },
  {
    id: 'tool-call-market-scan',
    from: 'tool-call',
    to: 'market-scan',
    fromPort: 'bottom',
    toPort: 'top',
    via: [
      [1400, 275],
      [2160, 275],
    ],
  },
  {
    id: 'tool-call-price-context',
    from: 'tool-call',
    to: 'price-context',
    fromPort: 'bottom',
    toPort: 'top',
    via: [
      [1400, 275],
      [2470, 275],
    ],
  },
  {
    id: 'tool-call-market-anomaly-scan',
    from: 'tool-call',
    to: 'market-anomaly-scan',
    fromPort: 'bottom',
    toPort: 'top',
    via: [
      [1400, 275],
      [2780, 275],
    ],
  },
  {
    id: 'tool-call-sentiment-timeline',
    from: 'tool-call',
    to: 'sentiment-timeline',
    fromPort: 'bottom',
    toPort: 'top',
    via: [
      [1400, 275],
      [2315, 275],
    ],
  },
  {
    id: 'tool-call-news-price-relationship',
    from: 'tool-call',
    to: 'news-price-relationship',
    fromPort: 'bottom',
    toPort: 'top',
    via: [
      [1400, 275],
      [2625, 275],
    ],
  },
  {
    id: 'tool-call-market-relationship-analysis',
    from: 'tool-call',
    to: 'market-relationship-analysis',
    fromPort: 'bottom',
    toPort: 'top',
    via: [
      [1400, 275],
      [2935, 275],
    ],
  },
  {
    id: 'retriever-tool-result',
    from: 'retriever-tool',
    to: 'unstructured-retrieval',
    label: 'EVIDENCE',
    fromPort: 'right',
    toPort: 'left',
    labelAt: [1680, 1052],
  },
  {
    id: 'query-synthesis',
    from: 'structured-retrieval',
    to: 'synthesis',
    label: 'SQL ROWS',
    fromPort: 'top',
    fromOffset: 80,
    toPort: 'top',
    toOffset: -60,
    via: [
      [1540, 320],
      [1980, 320],
      [1980, 860],
      [3310, 860],
      [3310, 710],
    ],
    labelAt: [2645, 852],
  },
  {
    id: 'predict-synthesis',
    from: 'nvidia-kumo',
    to: 'synthesis',
    label: 'PREDICTION RESULT',
    fromPort: 'right',
    toPort: 'bottom',
    toOffset: -40,
    via: [
      [1940, 730],
      [1940, 920],
      [3330, 920],
    ],
    labelAt: [2635, 905],
  },
  {
    id: 'retrieve-synthesis',
    from: 'unstructured-retrieval',
    to: 'synthesis',
    label: 'SELECTED EVIDENCE',
    fromPort: 'right',
    toPort: 'bottom',
    toOffset: 40,
    via: [[3410, 1060]],
    labelAt: [2685, 1052],
  },
  {
    id: 'market-scan-synthesis',
    from: 'market-scan',
    to: 'synthesis',
    label: 'RESULTS',
    fromPort: 'bottom',
    toPort: 'left',
    toOffset: 35,
    via: [[2160, 785]],
  },
  {
    id: 'price-context-synthesis',
    from: 'price-context',
    to: 'synthesis',
    fromPort: 'bottom',
    toPort: 'left',
    toOffset: 15,
    via: [[2470, 765]],
  },
  {
    id: 'market-anomaly-scan-synthesis',
    from: 'market-anomaly-scan',
    to: 'synthesis',
    fromPort: 'bottom',
    toPort: 'left',
    toOffset: -5,
    via: [[2780, 745]],
  },
  {
    id: 'sentiment-timeline-synthesis',
    from: 'sentiment-timeline',
    to: 'synthesis',
    fromPort: 'bottom',
    toPort: 'left',
    toOffset: 25,
    via: [[2315, 775]],
  },
  {
    id: 'news-price-relationship-synthesis',
    from: 'news-price-relationship',
    to: 'synthesis',
    fromPort: 'bottom',
    toPort: 'left',
    toOffset: 5,
    via: [[2625, 755]],
  },
  {
    id: 'market-relationship-analysis-synthesis',
    from: 'market-relationship-analysis',
    to: 'synthesis',
    fromPort: 'bottom',
    toPort: 'left',
    toOffset: -15,
    via: [[2935, 735]],
  },
  {
    id: 'hermes-synthesis',
    from: 'hermes-agent',
    to: 'synthesis',
    label: 'SYNTHESIZES',
    fromPort: 'top',
    toPort: 'top',
    toOffset: 60,
    via: [
      [460, 16],
      [3430, 16],
    ],
    labelAt: [1945, 8],
  },
  {
    id: 'synthesis-report',
    from: 'synthesis',
    to: 'report-generation',
    label: 'COMPILES',
    fromPort: 'right',
    toPort: 'left',
    labelAt: [3540, 742],
  },
  {
    id: 'report-answer',
    from: 'report-generation',
    to: 'trusted-answer',
    label: 'PUBLISHES',
    fromPort: 'right',
    toPort: 'left',
    labelAt: [3875, 742],
  },
]

const hermesComponentNodeMap: Partial<Record<GraphEvent['component'], ExecutionNodeId[]>> = {
  agent: ['hermes-agent'],
  ontology: ['nvidia-ontology'],
  structured_retrieval: ['structured-retrieval'],
  structured_prediction: ['structured-prediction'],
  unstructured_retrieval: ['unstructured-retrieval'],
}

/**
 * The node of each tool drawn as its own node, by registered tool id (and the
 * Hermes utility tools by name). Any other tool is drawn as Other Tools.
 */
export const TOOL_NODE_BY_NAME: Readonly<Partial<Record<string, ExecutionNodeId>>> = {
  tool_search: 'tool-search',
  tool_describe: 'tool-describe',
  tool_call: 'tool-call',
  skills_list: 'skills-list',
  skill_view: 'skill-view',
  ask_question: 'ontology-tool',
  retrieve_evidence: 'retriever-tool',
  market_scan: 'market-scan',
  market_anomaly_scan: 'market-anomaly-scan',
  price_context: 'price-context',
  sentiment_timeline: 'sentiment-timeline',
  analyze_news_price_relationship: 'news-price-relationship',
  analyze_market_relationships: 'market-relationship-analysis',
}

/** Tools the agent dispatches through Tool Invocation. */
const DISPATCHED_TOOL_NODES: ReadonlySet<ExecutionNodeId> = new Set([
  'ontology-tool',
  'retriever-tool',
  'market-scan',
  'market-anomaly-scan',
  'price-context',
  'sentiment-timeline',
  'news-price-relationship',
  'market-relationship-analysis',
])

/**
 * A registered capability tool without a node of its own is drawn by its
 * capability (component and resources), e.g. structured prediction.
 */
const toolNode = (event: GraphEvent): ExecutionNodeId | undefined => {
  if (!event.toolName) return undefined
  const node = TOOL_NODE_BY_NAME[event.toolName]
  if (node) return node
  // A tool with no node of its own and no capability node, e.g. a Hermes
  // utility or a newer registered tool, is drawn as Other Tools.
  return hermesComponentNodeMap[event.component] ? undefined : 'hermes-tools'
}

/**
 * Resources light only from the resources a call is known to have used; a
 * display name is never enough.
 */
const resourceMatches = (event: GraphEvent): ExecutionNodeId[] => {
  const nodes: ExecutionNodeId[] = []
  if (event.observedResources.includes('structured_database')) nodes.push('structured-database')
  if (event.observedResources.includes('nvidia_kumo')) nodes.push('nvidia-kumo')
  if (event.observedResources.includes('nvidia_ontology')) nodes.push('nvidia-ontology')
  return nodes
}

export const nodeIdsForEvent = (event: GraphEvent): ExecutionNodeId[] => {
  const nodeIds = new Set<ExecutionNodeId>(hermesComponentNodeMap[event.component] || [])
  const directToolNode = toolNode(event)
  if (directToolNode) {
    nodeIds.add(directToolNode)
    // Hermes calls a configured MCP tool directly, without a separate
    // `tool_call` event; the call still proves the Tool Invocation stage ran.
    if (DISPATCHED_TOOL_NODES.has(directToolNode)) nodeIds.add('tool-call')
  }
  resourceMatches(event).forEach((nodeId) => nodeIds.add(nodeId))

  if (event.kind === 'run.started') {
    nodeIds.add('business-question')
    nodeIds.add('hermes-agent')
  }
  if (event.kind === 'run.completed') {
    nodeIds.add('report-generation')
    nodeIds.add('synthesis')
    nodeIds.add('trusted-answer')
  }
  if (event.sourceKind === 'reasoning.available') nodeIds.add('synthesis')

  return [...nodeIds]
}

const eventState = (event: GraphEvent): Exclude<ExecutionNodeState, 'pending' | 'unobserved'> => {
  if (event.kind.endsWith('.failed')) return 'failed'
  if (event.kind.endsWith('.started')) return 'running'
  return 'completed'
}

const matchingEvents = (nodeId: ExecutionNodeId, events: readonly GraphEvent[]): GraphEvent[] =>
  events.filter((event) => nodeIdsForEvent(event).includes(nodeId))

const uniqueInvocationCount = (events: readonly GraphEvent[]): number => {
  const invocationIds = new Set(
    events.flatMap((event) => (event.invocationId ? [event.invocationId] : []))
  )
  return invocationIds.size
}

/** An invocation as the event that settled it, to find its nodes. */
const invocationEvent = (invocation: GraphInvocation): GraphEvent => ({
  eventId: invocation.invocationId,
  kind:
    invocation.status === 'running'
      ? 'invocation.started'
      : invocation.status === 'failed'
        ? 'invocation.failed'
        : 'invocation.completed',
  sourceKind: 'tool.completed',
  component: invocation.component,
  toolName: invocation.toolName,
  invocationId: invocation.invocationId,
  parentInvocationId: null,
  observedResources: invocation.observedResources,
})

const invocationMatchesNode = (nodeId: ExecutionNodeId, invocation: GraphInvocation): boolean =>
  nodeIdsForEvent(invocationEvent(invocation)).includes(nodeId)

const nodeState = (
  nodeId: ExecutionNodeId,
  visibleEvents: readonly GraphEvent[],
  allEvents: readonly GraphEvent[],
  projection: GraphProjection
): ExecutionNodeState => {
  const visible = matchingEvents(nodeId, visibleEvents)
  const relatedInvocations = projection.invocations.filter((invocation) =>
    invocationMatchesNode(nodeId, invocation)
  )

  // A capability can be retried after a failed attempt. While replay is in
  // progress, summarize the latest observed attempt so the graph reflects the
  // action at the current cursor. Once the run is terminal, a completed attempt
  // proves that the capability succeeded even if a later optional retry failed.
  // Every attempt remains available in the node detail and timeline.
  if (relatedInvocations.length) {
    if (projection.status === 'completed' || projection.status === 'failed') {
      if (relatedInvocations.some((invocation) => invocation.status === 'completed')) {
        return 'completed'
      }
      if (relatedInvocations.some((invocation) => invocation.status === 'failed')) return 'failed'
    }
    const latestInvocationId = [...visible]
      .reverse()
      .find((event) => event.invocationId)?.invocationId
    const latestInvocation = relatedInvocations.find(
      (invocation) => invocation.invocationId === latestInvocationId
    )
    if (latestInvocation) return latestInvocation.status
  }

  if (relatedInvocations.some((invocation) => invocation.status === 'failed')) return 'failed'
  if (relatedInvocations.some((invocation) => invocation.status === 'running')) return 'running'
  if (relatedInvocations.length) return 'completed'
  // The question has no completion event of its own. Once the run ends it has
  // necessarily been handed to the agent.
  if (
    nodeId === 'business-question' &&
    projection.status !== 'idle' &&
    projection.status !== 'running'
  ) {
    return 'completed'
  }
  if (!visible.length) {
    if (nodeId === 'business-question' && projection.status !== 'idle') return 'completed'
    if (nodeId === 'trusted-answer' && projection.answerAvailable) return 'completed'
    if (nodeId === 'report-generation' && projection.status === 'completed') return 'completed'
    if (nodeId === 'synthesis' && projection.answerAvailable) return 'completed'
    return matchingEvents(nodeId, allEvents).length ? 'pending' : 'unobserved'
  }
  return eventState(visible[visible.length - 1])
}

const edgeState = (from: ExecutionNodeState, to: ExecutionNodeState): ExecutionNodeState => {
  if (from === 'unobserved' || to === 'unobserved') return 'unobserved'
  if (from === 'pending' || to === 'pending') return 'pending'
  if (from === 'failed' || to === 'failed') return 'failed'
  if (from === 'running' || to === 'running') return 'running'
  return 'completed'
}

/**
 * The events that prove an edge: one event that lights both ends, or both
 * ends observed in an order that the edge implies.
 */
const edgeEvidence = (
  edge: ExecutionGraphEdgeDefinition,
  events: readonly GraphEvent[]
): Set<string> | null => {
  const eventIds = new Set<string>()
  const fromEvents = matchingEvents(edge.from, events)
  const toEvents = matchingEvents(edge.to, events)

  // A single typed event may prove both a capability and its observed resource,
  // or a terminal transition and the output stages it completes.
  events.forEach((event) => {
    const nodes = nodeIdsForEvent(event)
    if (nodes.includes(edge.from) && nodes.includes(edge.to)) eventIds.add(event.eventId)
  })

  const from = fromEvents[0]
  const to = toEvents[0]
  if (!from || !to) return eventIds.size ? eventIds : null

  const fromIndex = events.indexOf(from)
  const toIndex = events.indexOf(to)
  const agentOwnsTarget = edge.from === 'hermes-agent' && edge.to !== 'synthesis'
  const returnsToSynthesis = edge.to === 'synthesis' && edge.from !== 'hermes-agent'
  const isLinearOutputEdge =
    edge.id === 'hermes-synthesis' || edge.id === 'synthesis-report' || edge.id === 'report-answer'
  const isOrderedToolEdge =
    edge.id === 'tool-search-describe' ||
    edge.id === 'tool-describe-call' ||
    edge.id === 'tool-call-ontology' ||
    edge.id === 'tool-call-retriever' ||
    edge.id === 'skills-list-view'

  if (
    edge.id === 'question-hermes' ||
    agentOwnsTarget ||
    (returnsToSynthesis && fromIndex <= toIndex) ||
    (isOrderedToolEdge && fromIndex <= toIndex) ||
    (isLinearOutputEdge && fromIndex <= toIndex)
  ) {
    eventIds.add(from.eventId)
    eventIds.add(to.eventId)
  }

  return eventIds.size ? eventIds : null
}

export const buildExecutionGraphViewModel = ({
  allEvents,
  visibleEvents,
  projection,
}: {
  allEvents: readonly GraphEvent[]
  visibleEvents: readonly GraphEvent[]
  projection: GraphProjection
}): ExecutionGraphViewModel => {
  const currentEvent = visibleEvents[visibleEvents.length - 1]
  const currentNodeIds = new Set(currentEvent ? nodeIdsForEvent(currentEvent) : [])
  const states = new Map<ExecutionNodeId, ExecutionNodeState>()

  const nodes = hermesExecutionGraphNodes.map((node) => {
    const state = nodeState(node.id, visibleEvents, allEvents, projection)
    states.set(node.id, state)
    return {
      ...node,
      state,
      current: currentNodeIds.has(node.id),
      count: uniqueInvocationCount(matchingEvents(node.id, visibleEvents)),
    }
  })

  const edges = hermesExecutionGraphEdges.map((edge) => {
    const visibleEvidence = edgeEvidence(edge, visibleEvents)
    const futureEvidence = visibleEvidence ? null : edgeEvidence(edge, allEvents)
    const state = visibleEvidence
      ? edgeState(states.get(edge.from) || 'unobserved', states.get(edge.to) || 'unobserved')
      : futureEvidence
        ? 'pending'
        : 'unobserved'
    return {
      ...edge,
      state,
      current: Boolean(currentEvent && visibleEvidence?.has(currentEvent.eventId)),
    }
  })

  return {
    nodes,
    edges,
    groups: [...hermesExecutionGraphGroups],
    canvasWidth: HERMES_EXECUTION_CANVAS_WIDTH,
    canvasHeight: HERMES_EXECUTION_CANVAS_HEIGHT,
  }
}

export const buildExecutionNodeDetail = (
  nodeId: InspectableExecutionNodeId,
  graph: ExecutionGraphViewModel,
  projection: GraphProjection
): ExecutionNodeDetail => {
  const node = graph.nodes.find((candidate) => candidate.id === nodeId)
  if (!node) throw new Error(`Unknown execution node: ${nodeId}`)

  const invocations = projection.invocations
    .filter((invocation) => invocationMatchesNode(nodeId, invocation))
    .map((invocation) => ({
      invocationId: invocation.invocationId,
      name: invocation.toolName || 'Observed invocation',
      status: invocation.status,
      artifactRefs: invocation.artifactRefs,
    }))
  const artifactRefs = [...new Set(invocations.flatMap((invocation) => invocation.artifactRefs))]

  return {
    id: nodeId,
    label: node.label,
    subtitle: node.subtitle,
    state: node.state,
    observed: node.state !== 'unobserved' && node.state !== 'pending',
    invocations,
    artifactRefs,
  }
}

const INSPECTABLE_NODE_IDS: ReadonlySet<ExecutionNodeId> = new Set(
  hermesExecutionGraphNodes.filter((node) => node.inspectable).map((node) => node.id)
)

export const isInspectableNode = (nodeId: ExecutionNodeId): nodeId is InspectableExecutionNodeId =>
  INSPECTABLE_NODE_IDS.has(nodeId)
