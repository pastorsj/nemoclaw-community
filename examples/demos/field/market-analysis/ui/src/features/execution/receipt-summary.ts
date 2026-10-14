// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * One receipt as the execution inspectors describe it: a title, a one-line
 * summary, detail chips, the statement it ran (SQL, PQL, a search query or
 * its parameters) and its bounded output (rows or passages). The API has
 * already validated and bounded every receipt; this only chooses what to show.
 */

import type {
  AnalyticsResult,
  ReceiptV2,
  RetrievalEvidence,
  StructuredPrediction,
  StructuredQuery,
} from './contract'

export interface ReceiptSummary {
  title: string
  summary: string
  details: string[]
  notices?: string[]
  statement?: {
    label: string
    language: 'sql' | 'pql' | 'json' | 'text'
    value: string
    truncated: boolean
  }
  output?: ReceiptOutput
}

export type ReceiptOutput =
  | {
      kind: 'table'
      label: 'Query result' | 'Prediction result' | 'Analytics result'
      columns: string[]
      rows: string[][]
      displayedCount: number
      sourceCount?: number
      truncated: boolean
    }
  | {
      kind: 'passages'
      label: 'Retrieved passages'
      passages: Array<{ source: string; excerpt: string; metadata: string[] }>
      displayedCount: number
      sourceCount?: number
      truncated: boolean
    }

type Row = Record<string, unknown>

const MAX_ROWS = 25
const MAX_COLUMNS = 40
const MAX_PASSAGES = 10

const boundedText = (value: unknown, maxLength = 420): string | undefined => {
  if (typeof value !== 'string' || !value.trim()) return undefined
  const text = value.replace(/\s+/g, ' ').trim()
  return text.length > maxLength ? `${text.slice(0, maxLength - 1).trimEnd()}…` : text
}

const statementText = (value: string | null | undefined, maxLength: number) => {
  const text = value?.trim()
  return text ? { value: text.slice(0, maxLength), truncated: text.length > maxLength } : undefined
}

const plural = (value: number, singular: string, pluralForm = `${singular}s`): string =>
  `${value} ${value === 1 ? singular : pluralForm}`

const cellText = (value: unknown): string => {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'string') return boundedText(value, 1_000) ?? '—'
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return boundedText(JSON.stringify(value), 1_000) ?? '—'
}

const records = (value: unknown): Row[] =>
  Array.isArray(value)
    ? value.filter((item): item is Row => typeof item === 'object' && item !== null)
    : []

const count = (value: unknown): number | undefined =>
  typeof value === 'number' && Number.isFinite(value) && value >= 0 ? Math.floor(value) : undefined

const detail = (label: string, value: unknown): string[] =>
  value === undefined || value === null || value === '' ? [] : [`${label}: ${value}`]

const tableOutput = (
  rows: readonly Row[],
  label: Extract<ReceiptOutput, { kind: 'table' }>['label'],
  sourceCount: number | undefined,
  truncated: boolean
): ReceiptOutput => {
  const shown = rows.slice(0, MAX_ROWS)
  const columns = [...new Set(shown.flatMap((row) => Object.keys(row)))].slice(0, MAX_COLUMNS)
  return {
    kind: 'table',
    label,
    columns,
    rows: shown.map((row) => columns.map((column) => cellText(row[column]))),
    displayedCount: rows.length,
    sourceCount,
    truncated,
  }
}

const countSummary = (displayed: number, source: number | undefined, noun: string): string => {
  const returned = `${plural(displayed, noun)} returned`
  return source === undefined || source === displayed
    ? `${returned}.`
    : `${returned} from ${plural(source, 'source row')}.`
}

const structuredQuery = (content: StructuredQuery): ReceiptSummary => {
  const sql = statementText(content.sql, 12_000)
  return {
    title: 'Structured result',
    summary: countSummary(content.rows.length, content.sourceRowCount, 'row'),
    details: [`Database: ${content.databaseName}`],
    statement: sql && { label: 'Generated SQL', language: 'sql', ...sql },
    output: tableOutput(
      content.rows,
      'Query result',
      content.sourceRowCount,
      content.truncated || content.sourceRowCount > content.rows.length
    ),
  }
}

/**
 * A prediction in Kumo's binary-classification columns, as the original UI showed them: the most
 * likely entity first. The receipt keeps each entity's TRUE_PROB; ANCHOR_TIMESTAMP is the run's
 * anchor, FALSE_PROB its complement and PREDICTION whether TRUE_PROB is the larger of the two.
 */
const kumoRows = (content: StructuredPrediction): Row[] =>
  [...content.rows]
    .sort((a, b) => b.probability - a.probability)
    .map((row) => ({
      ANCHOR_TIMESTAMP: content.anchor,
      ENTITY: row.assetId,
      FALSE_PROB: 1 - row.probability,
      PREDICTION: row.probability > 0.5,
      TRUE_PROB: row.probability,
    }))

const structuredPrediction = (
  content: StructuredPrediction,
  databaseName: string | undefined
): ReceiptSummary => {
  const pql = statementText(content.pql, 8_000)
  return {
    title: 'Prediction result',
    summary: content.available
      ? countSummary(content.rows.length, undefined, 'prediction')
      : (content.reason ?? 'The prediction could not run.'),
    details: detail('Database', databaseName),
    statement: pql && { label: 'Generated PQL', language: 'pql', ...pql },
    output: content.available
      ? tableOutput(kumoRows(content), 'Prediction result', undefined, false)
      : undefined,
  }
}

export const OPERATION_LABELS: Readonly<Record<AnalyticsResult['operationId'], string>> = {
  market_scan: 'Market Scan',
  market_anomaly_scan: 'Market Anomaly Scan',
  price_context: 'Price Context',
  sentiment_timeline: 'Sentiment Timeline',
  analyze_news_price_relationship: 'News and Price Relationship',
  analyze_market_relationships: 'Market Relationship Analysis',
  intraday_scan: 'Intraday Scan',
}

/** The rows each operation returns, one per ranked asset, session, event or period. */
const analyticsRows = (content: AnalyticsResult): Row[] => {
  const payload = content.payload ?? {}
  switch (content.operationId) {
    case 'market_scan':
      return records(payload.observations).map((observation) => ({
        rank: observation.rank,
        asset_id: observation.asset_id,
        score: observation.score,
        ...(typeof observation.values === 'object' && observation.values !== null
          ? (observation.values as Row)
          : {}),
        observation_count: observation.observation_count,
        coverage_ratio: observation.coverage_ratio,
      }))
    case 'market_anomaly_scan':
      return records(payload.observations).map((observation) => ({
        rank: observation.rank,
        asset_id: observation.asset_id,
        timestamp: observation.timestamp,
        anomaly_score: observation.anomaly_score,
        cohort_percentile: observation.cohort_percentile,
        is_anomaly: observation.is_anomaly,
        observed_deviations: observation.observed_deviations,
      }))
    case 'sentiment_timeline':
      return records(payload.points)
    case 'analyze_news_price_relationship':
      return records(payload.events)
    case 'analyze_market_relationships':
      return records(payload.central_assets)
    case 'intraday_scan':
      return records(payload.observations)
    case 'price_context':
      return records(payload.summaries)
  }
}

const analyticsDetails = (content: AnalyticsResult): string[] => {
  const payload = content.payload ?? {}
  switch (content.operationId) {
    case 'price_context':
      return [
        `Series points: ${records(payload.series).length}`,
        `Series truncated: ${payload.series_truncated === true ? 'yes' : 'no'}`,
      ]
    case 'market_anomaly_scan':
      return [
        ...detail('Training observations', count(payload.training_observations)),
        ...detail('Scoring observations', count(payload.scoring_observations)),
        ...detail('Flagged observations', count(payload.flagged_observations)),
      ]
    case 'sentiment_timeline':
      return [
        ...detail('Articles considered', count(payload.articles_considered)),
        ...detail('Frequency', boundedText(payload.frequency, 32)),
      ]
    case 'analyze_news_price_relationship': {
      const coverage = typeof payload.coverage_ratio === 'number' ? payload.coverage_ratio : null
      return [
        ...detail('Eligible events', count(payload.eligible_event_count)),
        ...detail('Aligned events', count(payload.aligned_event_count)),
        ...(count(payload.return_horizon_sessions) === undefined
          ? []
          : [`Forward horizon: ${count(payload.return_horizon_sessions)} sessions`]),
        ...(coverage === null ? [] : [`Alignment coverage: ${Math.round(coverage * 100)}%`]),
        ...detail(
          'Descriptive correlation',
          typeof payload.sentiment_return_correlation === 'number'
            ? payload.sentiment_return_correlation
            : undefined
        ),
      ]
    }
    case 'analyze_market_relationships':
      return [
        ...(boundedText(payload.window_start, 32) && boundedText(payload.window_end, 32)
          ? [`Window: ${payload.window_start} to ${payload.window_end}`]
          : []),
        ...detail('Graph nodes', count(payload.node_count)),
        ...detail('Graph edges', count(payload.edge_count)),
      ]
    case 'intraday_scan':
      return [
        ...detail('Assets scanned', count(payload.assets_scanned)),
        ...detail('Sessions scanned', count(payload.sessions_scanned)),
        ...detail('Files read', count(payload.files_read)),
      ]
    case 'market_scan':
      return []
  }
}

const analytics = (content: AnalyticsResult, durationMs: number): ReceiptSummary => {
  const label = OPERATION_LABELS[content.operationId]
  const rows = analyticsRows(content)
  const payload = content.payload ?? {}
  const assetsRanked = count(payload.assets_ranked)
  const sourceCount = content.operationId === 'market_scan' ? assetsRanked : undefined
  const engine = content.engine
  const summaries: Partial<Record<AnalyticsResult['operationId'], string>> = {
    market_anomaly_scan: `${plural(rows.length, 'ranked anomaly observation')} returned.`,
    sentiment_timeline: `${plural(rows.length, 'timeline period')} returned.`,
    analyze_news_price_relationship: `${plural(rows.length, 'aligned event')} returned.`,
    analyze_market_relationships: `${plural(rows.length, 'ranked asset')} returned.`,
    intraday_scan: `${plural(rows.length, 'ranked session')} returned.`,
  }
  const parameters = statementText(JSON.stringify(content.publicParameters, null, 2), 8_000)
  return {
    title: `${label} result`,
    summary:
      content.status === 'failed'
        ? (content.error?.message ?? 'The analytics operation failed.')
        : (summaries[content.operationId] ?? countSummary(rows.length, sourceCount, 'row')),
    details: [
      `Database: ${content.databaseName}`,
      `Source: ${content.sourceId}`,
      `Status: ${content.status}`,
      ...(engine
        ? [`Engine: ${engine.library} ${engine.version} (${engine.device.toUpperCase()})`]
        : []),
      `Observed duration: ${durationMs} ms`,
      `Input rows: ${content.rowsScanned}`,
      ...analyticsDetails(content),
    ],
    notices: [
      ...content.warnings,
      ...content.limitations,
      ...(content.error ? [content.error.message] : []),
    ].slice(0, 4),
    statement: parameters && { label: 'Public parameters', language: 'json', ...parameters },
    output: tableOutput(
      rows,
      'Analytics result',
      sourceCount,
      payload.series_truncated === true ||
        payload.points_truncated === true ||
        payload.events_truncated === true ||
        (sourceCount !== undefined && sourceCount > rows.length)
    ),
  }
}

const retrieval = (content: RetrievalEvidence): ReceiptSummary => {
  const candidates = Object.values(content.candidateCounts).reduce((sum, n) => sum + n, 0)
  const searchParameters = Object.entries({
    ...content.index.params,
    ...content.index.searchParams,
  })
  const timings: Array<[string, number]> = [
    ['Embedding', content.timings.embedMs],
    ['Vector search', content.timings.searchMs],
    ['Reranking', content.timings.rerankMs],
    ['Total retrieval', content.timings.totalMs],
  ]
  const query = statementText(content.query, 1_000)
  return {
    title: 'Unstructured retrieval result',
    summary: `${plural(content.hits.length, 'passage')} displayed from ${plural(candidates, 'candidate')}.`,
    details: [
      `Sources: ${content.sourceIds.join(', ')}`,
      `Collection: ${content.collection}`,
      `Collection version: ${content.collectionVersion}`,
      // Milvus names its GPU indexes GPU_*; the rest run on the CPU
      `Vector index: ${content.index.type} (${content.index.type.startsWith('GPU_') ? 'GPU' : 'CPU'})`,
      `Vector metric: ${content.index.metric}`,
      ...(searchParameters.length
        ? [
            `Search parameters: ${searchParameters.map(([name, value]) => `${name} ${value}`).join(' · ')}`,
          ]
        : []),
      ...(content.models.rerank ? ['Ranking: reranker'] : []),
      `Embedding model: ${content.models.embed}`,
      ...(content.models.rerank ? [`Reranker model: ${content.models.rerank}`] : []),
      ...timings.map(([label, ms]) => `${label}: ${ms.toFixed(1)} ms`),
    ],
    statement: query && { label: 'Search query', language: 'text', ...query },
    output: {
      kind: 'passages',
      label: 'Retrieved passages',
      passages: content.hits.slice(0, MAX_PASSAGES).map((hit) => ({
        source: boundedText(hit.title, 320) ?? hit.documentId,
        excerpt: boundedText(hit.snippet, 1_500) ?? '',
        metadata: [
          `Rank ${hit.rank}`,
          `Source: ${hit.sourceId}`,
          `Document: ${hit.documentId}`,
          `Chunk: ${hit.chunkId}`,
          // As the original listed it: a rerank logit only when it is not negative
          ...(hit.score >= 0 ? [`Rerank score: ${hit.score}`] : []),
          `Vector score: ${hit.vectorScore}`,
          ...(hit.publishedAt ? [`Published: ${hit.publishedAt}`] : []),
          ...(typeof hit.metadata?.citation === 'string' && hit.metadata.citation.trim()
            ? [`Citation: ${boundedText(hit.metadata.citation, 400)}`]
            : []),
          ...(hit.url ? [`Source URL: ${hit.url}`] : []),
        ],
      })),
      displayedCount: content.hits.length,
      sourceCount: candidates,
      truncated: candidates > content.hits.length,
    },
  }
}

/**
 * Summarizes one receipt for the inspectors (and the timeline). A prediction receipt does not name
 * its database; `databaseName` is the pack's, which Kumo's graph is read from.
 */
export const summarizeReceipt = (
  receipt: ReceiptV2,
  { databaseName }: { databaseName?: string } = {}
): ReceiptSummary => {
  if (receipt.status === 'failed' || !receipt.content) {
    return {
      title: 'Tool result',
      summary: 'The tool call ended with a failure.',
      details:
        receipt.artifactKind === 'structured_prediction' ? detail('Database', databaseName) : [],
      notices: receipt.errorSummary ? [receipt.errorSummary] : [],
    }
  }
  switch (receipt.artifactKind) {
    case 'structured_query':
      return structuredQuery(receipt.content)
    case 'structured_prediction':
      return structuredPrediction(receipt.content, databaseName)
    case 'analytics_result':
      return analytics(receipt.content, receipt.durationMs)
    case 'retrieval_evidence':
      return retrieval(receipt.content)
  }
}

/** "8 rows", "3 passages of 32" */
export const receiptOutputCount = (output: ReceiptOutput): string => {
  const noun = output.kind === 'table' ? 'row' : 'passage'
  const displayed = `${output.displayedCount} ${noun}${output.displayedCount === 1 ? '' : 's'}`
  return output.sourceCount === undefined || output.sourceCount === output.displayedCount
    ? displayed
    : `${displayed} of ${output.sourceCount}`
}
