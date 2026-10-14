// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The execution view of one run: its header, replay controls over its
 * events, the run summary once it ends, and the execution graph. Choosing a
 * node opens its explorer over the graph; the structured database opens the
 * data viewer (in replay, over the bundle's copy of the database). Live runs
 * load their receipts from the job export; recorded runs are already in the
 * store.
 */

'use client'

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { selectPackDatabaseName, useLayoutStore } from '@/features/layout/store'
import { useAppConfig, type ExecutionFocus, type ExecutionWorkspaceProps } from '@/shared/context'
import type {
  AnalyticsResultReceipt,
  ExecutionEventV2,
  ReceiptV2,
  StructuredQueryReceipt,
} from './contract'
import { DatabaseBrowser, type StructuredSource } from './data-viewer/DatabaseBrowser'
import {
  loadReplayDatabase,
  replayPreview,
  replayQuery,
  replaySnapshot,
} from './data-viewer/database-client'
import styles from './execution-workspace.module.css'
import { EvidenceInspector } from './explorers/EvidenceInspector'
import { MarketToolExplorer } from './explorers/MarketToolExplorer'
import { OntologyLineageInspector } from './explorers/OntologyLineageInspector'
import { elapsedMs } from './format'
import {
  buildExecutionGraphViewModel,
  buildExecutionNodeDetail,
  buildGpuAccelerationByNode,
  ExecutionGraph,
  isInspectableExecutionNodeState,
  isInspectableNode,
  nodeIdsForEvent,
  TOOL_NODE_BY_NAME,
  toGraphEvent,
  toGraphProjection,
  type ExecutionNodeDetail,
  type GraphComponent,
  type InspectableExecutionNodeId,
} from './graph'
import {
  projectRun,
  publishedMetrics,
  runEnded,
  type RunProjection,
  type ToolCall,
} from './projection'
import { OPERATION_LABELS } from './receipt-summary'
import { TOOL_LOGOS, toolFor, type NodeLogo } from './registry'
import { ReplayControls } from './replay/ReplayControls'
import { loadJobExport } from './replay/sources'
import { useReplay } from './replay/use-replay'
import { useExecutionRun } from './store'

type ExportState = 'loading' | 'loaded' | 'failed'
type ReplayScopeState = 'future' | 'between' | 'past'

const NO_EVENTS: ExecutionEventV2[] = []
const NO_RECEIPTS: Record<string, ReceiptV2> = {}
/** Every data pack's structured source is a DuckDB database. */
const DUCKDB_MARK = { src: '/capability-assets/provider-duckdb.svg', alt: 'DuckDB' }

/**
 * Live mode: the job export holds the receipts (and the events of a run that
 * streamed before this page loaded). Fetch it on open, when the run reports
 * new evidence and when it finishes.
 */
const useJobExport = (jobId: string, live: boolean, run: RunProjection): ExportState => {
  const [state, setState] = useState<ExportState>('loading')
  const evidence = run.toolCalls.reduce((count, call) => count + call.receiptIds.length, 0)
  const finished = run.status !== 'running'
  useEffect(() => {
    if (!live) return
    let cancelled = false
    loadJobExport(jobId).then(
      () => {
        if (!cancelled) setState('loaded')
      },
      () => {
        if (!cancelled) setState('failed')
      }
    )
    return () => {
      cancelled = true
    }
  }, [jobId, live, evidence, finished])
  return live ? state : 'loaded'
}

/** The tool call a citation points at: by invocation id, else by evidence (receipt) id. */
const focusedCall = (run: RunProjection, focus: ExecutionFocus | null): ToolCall | undefined =>
  focus
    ? run.toolCalls.find(
        (call) =>
          call.invocationId === focus.invocationId ||
          (focus.referenceId !== undefined && call.receiptIds.includes(focus.referenceId))
      )
    : undefined

/** The first inspectable node a tool call lights, to open it from a citation. */
const nodeOfCall = (
  call: ToolCall,
  events: readonly ExecutionEventV2[]
): InspectableExecutionNodeId | null =>
  events
    .filter((event) => event.invocationId === call.invocationId)
    .flatMap((event) => nodeIdsForEvent(toGraphEvent(event)))
    .find((nodeId): nodeId is InspectableExecutionNodeId => isInspectableNode(nodeId)) ?? null

/** Structured sources the run's receipts name. */
const structuredSourcesOf = (receipts: Record<string, ReceiptV2>): string[] => [
  ...new Set(
    Object.values(receipts).flatMap((receipt) =>
      receipt.artifactKind === 'analytics_result' && receipt.content
        ? [receipt.content.sourceId]
        : []
    )
  ),
]

/** The market analytics nodes, each explored one call at a time. */
const MARKET_NODES: ReadonlySet<InspectableExecutionNodeId> = new Set([
  'market-scan',
  'market-anomaly-scan',
  'price-context',
  'sentiment-timeline',
  'news-price-relationship',
  'market-relationship-analysis',
])

const isAnalytics = (receipt: ReceiptV2): receipt is AnalyticsResultReceipt =>
  receipt.artifactKind === 'analytics_result'

const isStructuredQuery = (receipt: ReceiptV2): receipt is StructuredQueryReceipt =>
  receipt.artifactKind === 'structured_query'

/**
 * The structured sources the data viewer can browse: the pack's sources with a
 * database that the question selected or a receipt read.
 */
const browsableSources = (
  available: ReadonlyArray<{ id: string; name: string; database_name?: string | null }>,
  sourceIds: readonly string[],
  receipts: readonly ReceiptV2[]
): StructuredSource[] => {
  const used = new Set([
    ...sourceIds,
    ...receipts.flatMap((receipt) =>
      isAnalytics(receipt) && receipt.content ? [receipt.content.sourceId] : []
    ),
  ])
  const databases = new Set(
    receipts.flatMap((receipt) =>
      isStructuredQuery(receipt) && receipt.content ? [receipt.content.databaseName] : []
    )
  )
  return available.flatMap((source) =>
    source.database_name && (used.has(source.id) || databases.has(source.database_name))
      ? [{ id: source.id, name: source.name, databaseName: source.database_name }]
      : []
  )
}

/** The logos of each node whose tool is built on a library, from the registry. */
const NODE_LOGOS: ReadonlyMap<string, readonly NodeLogo[]> = new Map(
  Object.entries(TOOL_LOGOS).flatMap(([toolId, logos]) => {
    const nodeId = TOOL_NODE_BY_NAME[toolId]
    return nodeId && logos ? [[nodeId, logos] as const] : []
  })
)

const STEP_TOOL_LABELS: Readonly<Record<string, string>> = {
  tool_search: 'Tool Search',
  tool_describe: 'Tool Describe',
  tool_call: 'Tool Call',
  skills_list: 'Skills List',
  skill_view: 'Skill View',
  ask_question: 'Auto Ontology query',
  retrieve_evidence: 'Unstructured Retrieval',
}

const RESULT_LABELS: Readonly<Partial<Record<GraphComponent, string>>> = {
  structured_retrieval: 'Structured result available',
  structured_prediction: 'Prediction result available',
  unstructured_retrieval: 'Retrieved evidence available',
  market_analytics: 'Market analytics result available',
}

/** What the event at the replay cursor did, in the words of the replay bar. */
const stepLabel = (event: ExecutionEventV2 | undefined): string | undefined => {
  if (!event) return undefined
  switch (event.eventKind) {
    case 'run.created':
      return 'Hermes Agent started'
    case 'reasoning.available':
      return 'Evaluating results'
    case 'run.heartbeat':
      return 'Hermes Agent working'
    case 'run.completed':
      return 'Answer complete'
    case 'run.failed':
      return 'Run failed'
    case 'report.completed':
      return 'Response formatted'
    case 'report.reference_resolution':
      return 'Citations resolved'
    case 'report.metrics':
      return 'Run metrics available'
  }
  const graphEvent = toGraphEvent(event)
  if (graphEvent.kind === 'artifact.available') {
    return RESULT_LABELS[graphEvent.component] ?? event.display.label
  }
  if (!graphEvent.kind.startsWith('invocation.') || !graphEvent.toolName) {
    return event.display.label
  }
  const label =
    STEP_TOOL_LABELS[graphEvent.toolName] ?? toolFor(event.toolName)?.label ?? graphEvent.toolName
  return `${label} ${graphEvent.kind.slice('invocation.'.length)}`
}

const LIVE_OWNERS: Readonly<Record<GraphComponent, string>> = {
  agent: 'Hermes Agent',
  tool: 'Tool',
  ontology: 'Ontology Grounding',
  structured_retrieval: 'Structured Retrieval',
  structured_prediction: 'Structured Prediction',
  unstructured_retrieval: 'Unstructured Retrieval',
  market_analytics: 'Market Analytics',
}

const liveActivityDescription = (call: ToolCall | undefined): string => {
  if (!call) return 'Planning, invoking tools, or synthesizing the response'
  if (call.tool?.id === 'retrieve_evidence') {
    return 'Retrieving evidence with Milvus and NVIDIA Nemotron'
  }
  if (call.tool?.id === 'ask_question') return 'Generating and executing SQL with Auto Ontology'
  return `${call.label} is running`
}

const formatElapsed = (elapsedSeconds: number): string => {
  const minutes = Math.floor(elapsedSeconds / 60)
  const seconds = elapsedSeconds % 60
  return minutes ? `${minutes}m ${String(seconds).padStart(2, '0')}s` : `${seconds}s`
}

const formatDurationMs = (durationMs: number | null): string => {
  if (durationMs === null) return 'Duration unavailable'
  if (durationMs < 1_000) return `${durationMs} ms`
  const seconds = durationMs / 1_000
  return seconds < 60
    ? `${seconds.toFixed(Number.isInteger(seconds) ? 0 : 1)} s`
    : formatElapsed(Math.round(seconds))
}

const formatMetricCount = (count: number | null): string =>
  count === null ? 'Unavailable' : new Intl.NumberFormat('en-US').format(count)

const count = (value: unknown): number =>
  typeof value === 'number' && Number.isInteger(value) && value >= 0 ? value : 0

/** The display attributes of the run's last event of this kind. */
const lastAttributes = (
  events: readonly ExecutionEventV2[],
  kind: string
): Record<string, unknown> | null =>
  [...events].reverse().find((event) => event.eventKind === kind)?.display.attributes ?? null

/**
 * Publication, runtime and token usage of a run that has ended. The API records the citation
 * resolution and the run's metrics as the run's last events; a run recorded before them gets its
 * publication from the answer's citations and the receipts.
 */
const RunSummary = ({
  run,
  events,
  receipts,
  citedEvidenceIds,
}: {
  run: RunProjection
  events: readonly ExecutionEventV2[]
  receipts: Record<string, ReceiptV2>
  citedEvidenceIds: string[] | null
}): ReactNode => {
  const cited = new Set(citedEvidenceIds ?? [])
  const resolution = lastAttributes(events, 'report.reference_resolution')
  const metrics = publishedMetrics(events)
  // Evidence the answer could have cited: the run's completed receipts
  const available = new Set(
    run.toolCalls
      .flatMap((call) => call.receiptIds)
      .filter((id) => receipts[id]?.status === 'completed')
  )
  const citedCount = resolution ? count(resolution.total_citations) : cited.size
  const uncited = resolution
    ? count(resolution.uncited_evidence_count)
    : [...available].filter((id) => !cited.has(id)).length
  const invalid = resolution ? count(resolution.invalid_evidence_count) : 0
  const status =
    typeof resolution?.status === 'string'
      ? resolution.status
      : citedEvidenceIds === null
        ? null
        : cited.size
          ? 'reference_ids_resolved'
          : available.size
            ? 'evidence_uncited'
            : 'no_evidence'
  const runtimeProfile = metrics?.runtimeProfile ?? null
  const wallMs =
    metrics?.wallDurationMs ??
    (run.startedAt && run.endedAt
      ? Math.max(0, Math.round(elapsedMs(run.startedAt, run.endedAt)))
      : null)
  const toolMs =
    metrics?.knownToolDurationMs ??
    run.toolCalls.reduce(
      (total, call) =>
        total + (call.endedAt ? Math.max(0, elapsedMs(call.startedAt, call.endedAt)) : 0),
      0
    )
  const toolCallCount = metrics?.toolCallCount ?? run.toolCalls.length
  const tokens = run.inputTokens + run.outputTokens

  return (
    <section className={styles.runSummary} aria-label="Hermes run summary">
      <div>
        <small>Publication</small>
        <strong>
          {status === null
            ? 'Answer published'
            : status === 'reference_ids_resolved'
              ? 'Citation/reference IDs resolved'
              : status.replaceAll('_', ' ')}
        </strong>
        <span>
          {citedCount} cited evidence item(s)
          {invalid ? ` · ${invalid} unresolved` : ''}
          {uncited ? ` · ${uncited} available but uncited` : ''}
        </span>
      </div>
      <div>
        <small>Runtime</small>
        <strong>{formatDurationMs(wallMs)}</strong>
        <span>
          {toolCallCount} tool call(s) · {formatDurationMs(Math.round(toolMs))} observed tool time
        </span>
      </div>
      <div>
        <small>Token Usage</small>
        <strong>{formatMetricCount(tokens || null)} tokens</strong>
        <span>
          {formatMetricCount(run.inputTokens)} input · {formatMetricCount(run.outputTokens)} output
          {runtimeProfile ? ` · Runtime profile: ${runtimeProfile}` : ''}
        </span>
      </div>
    </section>
  )
}

/** Shown instead of an explorer while replay is at a step where the chosen node is not active. */
const ReplayScopedInspectorNotice = ({
  detail,
  currentStep,
  currentLabel,
  scopeState,
  onClose,
}: {
  detail: ExecutionNodeDetail
  currentStep: number
  currentLabel?: string
  scopeState: ReplayScopeState
  onClose: () => void
}): ReactNode => {
  const closeButtonRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    closeButtonRef.current?.focus()
    const handleKeyDown = (event: KeyboardEvent): void => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      onClose()
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [onClose])

  const title =
    scopeState === 'future'
      ? `${detail.label} has not been reached yet`
      : scopeState === 'between'
        ? `${detail.label} is between observed calls`
        : `${detail.label} is not active at this replay step`
  const guidance =
    scopeState === 'future'
      ? `Move forward to a step where ${detail.label} is active to inspect its evidence.`
      : `Move to a step where ${detail.label} is active, or finish the replay, to inspect its evidence.`

  return (
    <section
      className={styles.capabilityExplorer}
      role="dialog"
      aria-modal="true"
      tabIndex={-1}
      data-testid="capability-explorer"
      data-presentation="replay-scope-notice"
      data-capability={detail.id}
      aria-label={`${detail.label} replay status`}
    >
      <header className={styles.capabilityExplorerHeader}>
        <div>
          <span className={styles.eyebrow}>Replay-scoped evidence</span>
          <h3>{detail.label}</h3>
          <p>Inspector evidence follows the active replay step.</p>
        </div>
        <div className={styles.capabilityExplorerHeaderActions}>
          <span className={styles.capabilityCursor}>Replay step {currentStep}</span>
          <button
            ref={closeButtonRef}
            type="button"
            className={styles.iconButton}
            onClick={onClose}
            aria-label={`Close ${detail.label} replay status`}
          >
            ×
          </button>
        </div>
      </header>
      <div
        className={styles.replayScopeNotice}
        data-testid="replay-scope-notice"
        aria-live="polite"
      >
        <span className={styles.replayScopeNoticeIcon} aria-hidden="true">
          ↔
        </span>
        <div>
          <small>Step {currentStep || 0}</small>
          <h4>{title}</h4>
          {currentLabel ? <p>This step is showing {currentLabel}.</p> : null}
          <p>{guidance}</p>
        </div>
      </div>
    </section>
  )
}

type AvailableSource = { id: string; name: string; database_name?: string | null }
const NO_SOURCES: AvailableSource[] = []

/** Replay: the structured sources the recordings bundle copied into `database.json`. */
const useReplaySources = (enabled: boolean): AvailableSource[] => {
  const [sources, setSources] = useState<AvailableSource[]>(NO_SOURCES)
  useEffect(() => {
    if (!enabled) return
    let active = true
    void loadReplayDatabase().then((database) => {
      if (!active || !database) return
      setSources(
        database.sources.map((source) => ({
          id: source.id,
          name: source.name,
          database_name: source.databaseName,
        }))
      )
    })
    return () => {
      active = false
    }
  }, [enabled])
  return sources
}

export const ExecutionWorkspace = ({
  jobId,
  focus,
  question = null,
  sourceIds = [],
  onClose,
}: ExecutionWorkspaceProps): ReactNode => {
  const { mode } = useAppConfig()
  const stored = useExecutionRun(jobId)
  // A recorded run (every run in replay mode) has no live job behind it
  const liveMode = mode === 'live' && !stored?.recorded
  // As the original labeled them: a recorded run by its archive and job id, a live one by its job id
  const runKey = stored?.archive ? `recorded:${stored.archive}:${jobId}` : jobId
  const availableDataSources = useLayoutStore((state) => state.availableDataSources)
  const packDatabaseName = useLayoutStore(selectPackDatabaseName)
  const replaySources = useReplaySources(!liveMode)
  const events = stored?.events ?? NO_EVENTS
  const receipts = stored?.receipts ?? NO_RECEIPTS
  const jobStatus = stored?.jobStatus ?? null

  const whole = useMemo(() => projectRun(events, jobStatus), [events, jobStatus])
  const exportState = useJobExport(jobId, liveMode, whole)
  const replay = useReplay(events.length)
  const shownEvents = useMemo(() => events.slice(0, replay.step), [events, replay.step])
  // The job's status only describes the whole run, not a replay prefix
  const atEnd = replay.step === replay.total
  const shown = useMemo(
    () => projectRun(shownEvents, atEnd ? jobStatus : null),
    [shownEvents, atEnd, jobStatus]
  )

  const allGraphEvents = useMemo(() => events.map(toGraphEvent), [events])
  const shownGraphEvents = useMemo(
    () => allGraphEvents.slice(0, replay.step),
    [allGraphEvents, replay.step]
  )
  const shownProjection = useMemo(
    () => toGraphProjection(shown, shownGraphEvents),
    [shown, shownGraphEvents]
  )
  const wholeProjection = useMemo(
    () => toGraphProjection(whole, allGraphEvents),
    [whole, allGraphEvents]
  )
  const graph = useMemo(
    () =>
      buildExecutionGraphViewModel({
        allEvents: allGraphEvents,
        visibleEvents: shownGraphEvents,
        projection: shownProjection,
      }),
    [allGraphEvents, shownGraphEvents, shownProjection]
  )
  const fullGraph = useMemo(
    () =>
      buildExecutionGraphViewModel({
        allEvents: allGraphEvents,
        visibleEvents: allGraphEvents,
        projection: wholeProjection,
      }),
    [allGraphEvents, wholeProjection]
  )
  const gpuAccelerations = useMemo(
    () =>
      buildGpuAccelerationByNode(
        shown.toolCalls.flatMap((call) => call.receiptIds.flatMap((id) => receipts[id] ?? []))
      ),
    [shown, receipts]
  )
  const structuredSources = useMemo(() => structuredSourcesOf(receipts), [receipts])

  const currentEvent = shownEvents.at(-1)
  const currentGraphEvent = shownGraphEvents.at(-1)
  const terminalInspection = atEnd && runEnded(shown.status)
  const currentInspectableNodeIds = useMemo(
    () =>
      new Set<InspectableExecutionNodeId>(
        (currentGraphEvent ? nodeIdsForEvent(currentGraphEvent) : []).filter(
          (nodeId): nodeId is InspectableExecutionNodeId => isInspectableNode(nodeId)
        )
      ),
    [currentGraphEvent]
  )
  const interactiveNodeIds = terminalInspection ? undefined : currentInspectableNodeIds
  const unobservedLegendLabel = runEnded(whole.status) ? 'Never activated' : 'Not activated yet'

  // A cited source opens its node. Choosing or closing a node wins until the next citation click, which
  // brings a new focus object even when it cites the same source again.
  const [selectedNodeId, setSelectedNodeId] = useState<InspectableExecutionNodeId | null>(null)
  // An Auto Ontology call opened in the data viewer from its explorer
  const [queryReceipt, setQueryReceipt] = useState<StructuredQueryReceipt | null>(null)
  const focusCall = focusedCall(whole, focus)
  const focusNodeId = focusCall ? nodeOfCall(focusCall, events) : null
  const [handledFocus, setHandledFocus] = useState<ExecutionFocus | null>(null)
  // Counts handled citations, so an explorer opened by one starts on its cited call
  const [focusCount, setFocusCount] = useState(0)
  if (focus !== handledFocus && focusNodeId) {
    setHandledFocus(focus)
    setFocusCount((count) => count + 1)
    setSelectedNodeId(focusNodeId)
  }

  const selectedInspectorActive = Boolean(
    selectedNodeId && (terminalInspection || currentInspectableNodeIds.has(selectedNodeId))
  )
  const selectedDetail = useMemo(() => {
    if (!selectedNodeId) return null
    const detailGraph = selectedInspectorActive ? graph : fullGraph
    const detailProjection = selectedInspectorActive ? shownProjection : wholeProjection
    const node = detailGraph.nodes.find((candidate) => candidate.id === selectedNodeId)
    return node && isInspectableExecutionNodeState(node.state)
      ? buildExecutionNodeDetail(selectedNodeId, detailGraph, detailProjection)
      : null
  }, [fullGraph, graph, selectedInspectorActive, selectedNodeId, shownProjection, wholeProjection])
  const selectedReplayScopeState = useMemo<ReplayScopeState>(() => {
    if (!selectedNodeId) return 'past'
    const current = replay.step - 1
    const matching = allGraphEvents.flatMap((event, index) =>
      nodeIdsForEvent(event).includes(selectedNodeId) ? [index] : []
    )
    if (!matching.some((index) => index < current)) return 'future'
    return matching.some((index) => index > current) ? 'between' : 'past'
  }, [allGraphEvents, replay.step, selectedNodeId])

  const closeInspector = useCallback((): void => {
    setQueryReceipt(null)
    setSelectedNodeId(null)
  }, [])
  const closeQuery = useCallback((): void => setQueryReceipt(null), [])
  const handleNodeSelect = (nodeId: InspectableExecutionNodeId): void => {
    const node = graph.nodes.find((candidate) => candidate.id === nodeId)
    if (
      !node ||
      !isInspectableExecutionNodeState(node.state) ||
      (interactiveNodeIds && !interactiveNodeIds.has(nodeId))
    ) {
      return
    }
    setQueryReceipt(null)
    setSelectedNodeId(nodeId)
  }

  // Live activity: what the agent is doing now, while the run streams
  const followingLive = liveMode && atEnd && whole.status === 'running'
  const activeCall = shown.toolCalls.findLast((call) => call.state === 'running')
  const activeSince = activeCall?.startedAt ?? whole.startedAt
  const [clockMs, setClockMs] = useState(() => Date.now())
  useEffect(() => {
    if (!followingLive) return
    setClockMs(Date.now())
    const interval = window.setInterval(() => setClockMs(Date.now()), 1_000)
    return () => window.clearInterval(interval)
  }, [followingLive])
  const activeElapsedSeconds = activeSince
    ? Math.max(0, Math.floor((clockMs - Date.parse(activeSince)) / 1_000))
    : 0
  const activeComponent: GraphComponent = activeCall
    ? (shownProjection.invocations.find((call) => call.invocationId === activeCall.invocationId)
        ?.component ?? 'tool')
    : 'agent'

  // The receipts behind the chosen node: all of them once the run has ended, else those of the
  // call at the replay position. The agent's explorer lists every call it made.
  const inspectorReceipts = useMemo(() => {
    if (!selectedDetail) return []
    const refs =
      selectedDetail.id === 'hermes-agent'
        ? shown.toolCalls.flatMap((call) => call.receiptIds)
        : selectedDetail.artifactRefs
    const all = [...new Set(refs)].flatMap((id) => receipts[id] ?? [])
    if (terminalInspection) return all
    const invocationId = currentEvent?.invocationId
    return invocationId ? all.filter((receipt) => receipt.invocationId === invocationId) : []
  }, [currentEvent?.invocationId, receipts, selectedDetail, shown, terminalInspection])
  const packSources = liveMode ? (availableDataSources ?? NO_SOURCES) : replaySources
  const browsable = useMemo(
    () => browsableSources(packSources, sourceIds, Object.values(receipts)),
    [packSources, receipts, sourceIds]
  )
  // The pack's sources with a database, which the explorers name a question's structured source by
  const databaseSources = useMemo(
    () => packSources.filter((source) => source.database_name),
    [packSources]
  )
  // Receipts load from the job export in live mode
  const receiptsLoading =
    exportState === 'loading' && Boolean(selectedDetail?.artifactRefs.some((id) => !receipts[id]))
  const activeCursor = currentEvent?.cursor != null ? String(currentEvent.cursor) : '0'
  const focusedInvocationId = focusCall?.invocationId ?? null

  const graphLayerRef = useRef<HTMLDivElement>(null)
  const inspectorOpen = Boolean(selectedDetail)
  useEffect(() => {
    const layer = graphLayerRef.current
    if (!layer) return
    if (inspectorOpen) layer.setAttribute('inert', '')
    else layer.removeAttribute('inert')
  }, [inspectorOpen])

  const graphStatus = shownProjection.status
  const modeLabel = !events.length
    ? liveMode
      ? 'Awaiting Native Events'
      : 'No Events'
    : followingLive
      ? 'Hermes Live'
      : 'Hermes Recorded'

  let inspector: ReactNode = null
  if (selectedDetail && !selectedInspectorActive) {
    inspector = (
      <ReplayScopedInspectorNotice
        detail={selectedDetail}
        currentStep={replay.step}
        currentLabel={stepLabel(currentEvent)}
        scopeState={selectedReplayScopeState}
        onClose={closeInspector}
      />
    )
  } else if (selectedDetail && (selectedDetail.id === 'structured-database' || queryReceipt)) {
    inspector = (
      <DatabaseBrowser
        detail={selectedDetail}
        cursor={activeCursor}
        sources={browsable}
        receipts={Object.values(receipts).filter(isStructuredQuery)}
        initialReceipt={queryReceipt}
        {...(liveMode
          ? {}
          : {
              snapshotLoader: replaySnapshot,
              previewLoader: replayPreview,
              queryRunner: replayQuery,
            })}
        onClose={queryReceipt ? closeQuery : closeInspector}
      />
    )
  } else if (selectedDetail?.id === 'nvidia-ontology') {
    inspector = (
      <OntologyLineageInspector
        key={focusCount}
        detail={selectedDetail}
        cursor={activeCursor}
        question={question}
        receipts={inspectorReceipts.filter(isStructuredQuery)}
        loading={receiptsLoading}
        preferredInvocationId={focusedInvocationId}
        queryDatabases={browsable.map((source) => source.databaseName)}
        onOpenQuery={(receipt) => {
          replay.pause()
          setQueryReceipt(receipt)
        }}
        onClose={closeInspector}
      />
    )
  } else if (selectedDetail && MARKET_NODES.has(selectedDetail.id)) {
    const analytics = inspectorReceipts.filter(isAnalytics)
    const operation = analytics.find((receipt) => receipt.content)?.content?.operationId
    inspector = (
      <MarketToolExplorer
        key={`${selectedDetail.id}:${focusCount}`}
        nodeId={selectedDetail.id}
        title={operation ? OPERATION_LABELS[operation] : selectedDetail.label}
        cursor={activeCursor}
        receipts={analytics}
        preferredInvocationId={focusedInvocationId}
        loading={receiptsLoading}
        onClose={closeInspector}
      />
    )
  } else if (selectedDetail) {
    inspector = (
      <EvidenceInspector
        detail={selectedDetail}
        cursor={activeCursor}
        question={question}
        receipts={inspectorReceipts}
        databaseName={packDatabaseName}
        sourceIds={sourceIds}
        structuredSources={databaseSources}
        loading={receiptsLoading}
        onClose={closeInspector}
      />
    )
  }

  return (
    <section
      className={styles.workspace}
      aria-label="Execution workspace"
      data-live={followingLive || undefined}
    >
      {!inspectorOpen && (
        <header className={styles.workspaceHeader}>
          <div className={styles.workspaceTitle}>
            <span className={styles.eyebrow}>Inspectable trajectory</span>
            <h2>Execution Graph</h2>
            <div className={styles.runMetadata}>
              <span className={styles.modeBadge} data-live={followingLive || undefined}>
                <span className={styles.statusDot} />
                {modeLabel}
              </span>
              <span className={styles.runStatus} data-state={graphStatus}>
                {graphStatus}
              </span>
              <code title={runKey}>{runKey}</code>
            </div>
          </div>
          <button type="button" className={styles.backButton} onClick={onClose}>
            <span aria-hidden="true">←</span>
            Back to Answer
          </button>
        </header>
      )}

      {followingLive && (
        <section className={styles.liveActivity} data-testid="live-execution-activity">
          <span className={styles.liveActivityPulse} aria-hidden="true" />
          <div className={styles.liveActivityCopy}>
            <small>Live · {LIVE_OWNERS[activeComponent]}</small>
            <strong aria-live="polite">{liveActivityDescription(activeCall)}</strong>
          </div>
          <div className={styles.liveActivityTiming}>
            <strong aria-hidden="true">{formatElapsed(activeElapsedSeconds)} elapsed</strong>
            <small>Run active · waiting for the display-safe result</small>
          </div>
        </section>
      )}

      <ReplayControls replay={replay} currentLabel={stepLabel(currentEvent)} />

      {runEnded(shown.status) && (
        <RunSummary
          run={shown}
          events={events}
          receipts={receipts}
          citedEvidenceIds={stored?.citedEvidenceIds ?? null}
        />
      )}

      <div className={styles.graphRegion}>
        {events.length === 0 ? (
          <p className="text-secondary p-5 text-sm">
            {exportState === 'loading'
              ? 'Loading the run…'
              : 'No execution record is available for this answer.'}
          </p>
        ) : (
          <div
            ref={graphLayerRef}
            className={styles.graphInteractiveLayer}
            aria-hidden={inspectorOpen ? 'true' : undefined}
          >
            <ExecutionGraph
              model={graph}
              selectedNodeId={selectedNodeId}
              interactiveNodeIds={interactiveNodeIds}
              unobservedLegendLabel={unobservedLegendLabel}
              structuredDatabaseProviderMark={
                structuredSources.length === 1 ? DUCKDB_MARK : undefined
              }
              gpuAccelerations={gpuAccelerations}
              nodeLogos={NODE_LOGOS}
              onNodeSelect={handleNodeSelect}
            />
          </div>
        )}
        {inspector}
      </div>
    </section>
  )
}
