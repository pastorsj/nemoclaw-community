// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * One market analytics call: its tool receipt (engine, input, timing, return,
 * source) and its result drawn for the operation: ranked bars, the price
 * chart, the sentiment stacks, aligned returns or the relationship network.
 */

'use client'

import type { ReactNode } from 'react'
import type { AnalyticsResult, AnalyticsResultReceipt } from '../contract'
import { gpuAccelerationForReceipt } from '../graph'
import styles from './market-tool-inspector.module.css'

export interface MarketToolInspectorProps {
  toolTitle: string
  receipt: AnalyticsResultReceipt | null
}

type Primitive = string | number | boolean
type RecordValue = Record<string, unknown>

const record = (value: unknown): RecordValue | null =>
  value && typeof value === 'object' && !Array.isArray(value) ? (value as RecordValue) : null

const records = (value: unknown): RecordValue[] =>
  Array.isArray(value) ? value.filter((item): item is RecordValue => Boolean(record(item))) : []

const text = (value: unknown): string | null =>
  typeof value === 'string' && value.trim() ? value.trim() : null

const number = (value: unknown): number | null =>
  typeof value === 'number' && Number.isFinite(value) ? value : null

const integer = (value: unknown): number | null => {
  const parsed = number(value)
  return parsed === null ? null : Math.round(parsed)
}

const display = (value: Primitive | null | undefined): string => {
  if (value === null || value === undefined || value === '') return '—'
  if (typeof value === 'boolean') return value ? 'Yes' : 'No'
  return String(value)
}

const formatCompact = (value: number | null): string =>
  value === null ? '—' : new Intl.NumberFormat('en-US', { maximumFractionDigits: 2 }).format(value)

const formatSigned = (value: number | null, percentage = false): string => {
  if (value === null) return '—'
  const scaled = percentage ? value * 100 : value
  const rendered = new Intl.NumberFormat('en-US', {
    maximumFractionDigits: percentage ? 2 : 4,
  }).format(Math.abs(scaled))
  return `${scaled > 0 ? '+' : scaled < 0 ? '−' : ''}${rendered}${percentage ? '%' : ''}`
}

const formatPercentage = (value: number | null): string =>
  value === null
    ? '—'
    : `${new Intl.NumberFormat('en-US', { maximumFractionDigits: 2 }).format(value * 100)}%`

const formatDuration = (value: number | null): string =>
  value === null ? 'Not retained' : `${formatCompact(value)} ms`

const humanize = (value: string | null): string =>
  value
    ? value.replaceAll('_', ' ').replace(/\b\w/g, (character) => character.toUpperCase())
    : 'Recorded result'

/** How the receipt's library (`cudf.pandas`, `scikit-learn`, …) is named on the engine chip. */
const LIBRARY_LABELS: Readonly<Record<string, string>> = {
  cudf: 'cuDF',
  'cudf.pandas': 'cuDF',
  cugraph: 'cuGraph',
  'nx-cugraph': 'cuGraph',
  cuml: 'cuML',
  'cuml.accel': 'cuML',
  networkx: 'NetworkX',
  numpy: 'NumPy',
  pandas: 'pandas',
  'scikit-learn': 'scikit-learn',
}

const engineLibraryLabel = (value: string): string =>
  LIBRARY_LABELS[value.toLowerCase()] || humanize(value)

const boundedRows = (rows: RecordValue[], maximum = 12): RecordValue[] => rows.slice(0, maximum)

const stringList = (value: unknown): string[] =>
  Array.isArray(value) ? value.flatMap((item) => (text(item) ? [text(item)!] : [])) : []

const dateOnly = (value: unknown): string | null => text(value)?.slice(0, 10) || null

const dateRange = (start: unknown, end: unknown): string =>
  [dateOnly(start), dateOnly(end)].filter(Boolean).join(' → ')

/** The call's arguments in one line, e.g. "Return, Volatility · reviewed_assets · 2026-08-04 → 2026-08-31 · Top 5". */
const requestSummary = (result: AnalyticsResult): string => {
  const parameters = result.publicParameters
  const assets = stringList(parameters.asset_ids).join(', ')
  const universe = text(parameters.universe_id)
  const top = (value: unknown): string | null =>
    integer(value) === null ? null : `Top ${integer(value)}`
  const parts: Record<AnalyticsResult['operationId'], Array<string | null>> = {
    market_scan: [
      stringList(parameters.metrics).map(humanize).join(', ') || 'Market metrics',
      universe,
      integer(parameters.sessions) === null
        ? dateRange(parameters.start, parameters.end)
        : `${integer(parameters.sessions)} sessions → ${dateOnly(parameters.end)}`,
      top(parameters.limit),
    ],
    market_anomaly_scan: [
      universe,
      `Baseline ${dateRange(parameters.training_start, parameters.training_end)}`,
      `Score ${dateRange(parameters.scoring_start, parameters.scoring_end)}`,
      top(parameters.limit),
    ],
    price_context: [
      assets,
      dateRange(parameters.start, parameters.end),
      humanize(text(parameters.frequency) ?? 'daily'),
    ],
    sentiment_timeline: [
      assets || universe,
      dateRange(parameters.start, parameters.end),
      humanize(text(parameters.frequency) ?? 'weekly'),
    ],
    analyze_news_price_relationship: [
      assets || universe,
      dateRange(parameters.published_from, parameters.published_to),
      integer(parameters.return_horizon_sessions) === null
        ? null
        : `${integer(parameters.return_horizon_sessions)}-session return horizon`,
    ],
    // The original showed the graph's id, recent_return_correlation, humanized
    analyze_market_relationships: ['Recent Return Correlation', top(parameters.top_k)],
    intraday_scan: [
      humanize(text(parameters.rank_by) ?? 'intraday_range'),
      assets || universe,
      dateRange(parameters.start, parameters.end),
      top(parameters.limit),
    ],
  }
  return parts[result.operationId].filter(Boolean).join(' · ')
}

/** A library version as its package names it, as the original showed it: RAPIDS' "26.06.00" is 26.6.0. */
export const packageVersion = (version: string): string => version.replace(/(^|\.)0+(?=\d)/g, '$1')

/** Rows the call returned: its ranked list, series, periods or events. */
const outputRows = (result: AnalyticsResult): number => {
  const payload = result.payload ?? {}
  switch (result.operationId) {
    case 'price_context':
      return records(payload.series).length || records(payload.summaries).length
    case 'sentiment_timeline':
      return records(payload.points).length
    case 'analyze_news_price_relationship':
      // Every aligned event, though the receipt keeps only the first ones
      return integer(payload.aligned_event_count) ?? records(payload.events).length
    case 'analyze_market_relationships':
      return records(payload.central_assets).length
    default:
      return records(payload.observations).length
  }
}

/** The assets the call covered, when the result says. */
const assetCount = (result: AnalyticsResult): number | null => {
  const payload = result.payload ?? {}
  switch (result.operationId) {
    case 'market_scan':
      return integer(payload.assets_ranked)
    case 'price_context':
      return records(payload.summaries).length || null
    case 'analyze_market_relationships':
      return integer(payload.node_count)
    case 'intraday_scan':
      return integer(payload.assets_scanned)
    default:
      return null
  }
}

const EmptyResult = ({
  message = 'No records matched this request.',
}: {
  message?: string
}): ReactNode => (
  <div className={styles.emptyResult} data-testid="market-native-empty">
    {message}
  </div>
)

const RankedBars = ({
  items,
  valueLabel,
  percentage = false,
}: {
  items: Array<{
    id: string
    rank: number | null
    label: string
    value: number | null
    detail: string
  }>
  valueLabel: string
  percentage?: boolean
}): ReactNode => {
  if (!items.length) return <EmptyResult />
  const maximum = Math.max(0.000001, ...items.map((item) => Math.abs(item.value || 0)))
  return (
    <div className={styles.rankedBars} aria-label={`${valueLabel} ranking`}>
      {items.map((item, index) => (
        <div className={styles.rankedBarRow} key={item.id}>
          <span className={styles.rank}>{item.rank || index + 1}</span>
          <strong>{item.label}</strong>
          <span className={styles.barTrack} aria-hidden="true">
            <span
              className={styles.barFill}
              data-negative={item.value !== null && item.value < 0 ? true : undefined}
              style={{ width: `${Math.max(3, (Math.abs(item.value || 0) / maximum) * 100)}%` }}
            />
          </span>
          <span className={styles.barValue}>
            <b>{formatSigned(item.value, percentage)}</b>
            <small>{item.detail}</small>
          </span>
        </div>
      ))}
    </div>
  )
}

const PERCENT_METRICS = new Set(['return', 'peer_relative_return'])

const MarketScanResult = ({ payload }: { payload: RecordValue }): ReactNode => {
  const observations = boundedRows(records(payload.observations), 16)
  const metric = text(payload.primary_metric)
  return (
    <section className={styles.nativeResult} data-native-result="market-scan">
      <header className={styles.nativeResultHeader}>
        <div>
          <span>Ranked market scan</span>
          <h4>{humanize(metric)}</h4>
        </div>
        <small>
          {humanize(text(payload.direction))} · {humanize(text(payload.comparison))}
        </small>
      </header>
      <RankedBars
        valueLabel={humanize(metric)}
        percentage={PERCENT_METRICS.has(metric ?? '') && text(payload.comparison) !== 'zscore'}
        items={observations.map((observation, index) => ({
          id: `${display(text(observation.asset_id))}:${index}`,
          rank: integer(observation.rank),
          label: display(text(observation.asset_id)),
          value: number(observation.score),
          detail: `${formatCompact(integer(observation.observation_count))} observations · ${formatPercentage(number(observation.coverage_ratio))} coverage`,
        }))}
      />
    </section>
  )
}

const AnomalyResult = ({ payload }: { payload: RecordValue }): ReactNode => {
  const observations = boundedRows(records(payload.observations), 25)
  return (
    <section className={styles.nativeResult} data-native-result="market-anomaly-scan">
      <header className={styles.nativeResultHeader}>
        <div>
          <span>Observed multivariate anomalies</span>
          <h4>{formatCompact(integer(payload.flagged_observations))} flagged observations</h4>
        </div>
        {/* The policy id as the original showed it; results recorded before it name the method */}
        <small>
          {text(payload.policy_id) ? humanize(text(payload.policy_id)) : 'PCA reconstruction'}
        </small>
      </header>
      <div className={styles.resultMetadata} aria-label="Anomaly analysis summary">
        <span>
          Baseline <b>{formatCompact(integer(payload.training_observations))} observations</b>
        </span>
        <span>
          Scored <b>{formatCompact(integer(payload.scoring_observations))} observations</b>
        </span>
        <span>
          Features <b>{stringList(payload.feature_names).length}</b>
        </span>
      </div>
      <RankedBars
        valueLabel="Anomaly score"
        items={observations.map((observation, index) => {
          const deviations = record(observation.observed_deviations)
          const strongestDeviation = deviations
            ? Object.entries(deviations)
                .flatMap(([feature, value]) => {
                  const parsed = number(value)
                  return parsed === null ? [] : [{ feature, value: parsed }]
                })
                .sort((left, right) => Math.abs(right.value) - Math.abs(left.value))[0]
            : undefined
          return {
            id: `${display(text(observation.asset_id))}:${display(text(observation.timestamp))}:${index}`,
            rank: integer(observation.rank),
            label: `${display(text(observation.asset_id))} · ${dateOnly(observation.timestamp) || 'Date unavailable'}`,
            value: number(observation.anomaly_score),
            detail: [
              `${formatCompact(number(observation.cohort_percentile))}th percentile`,
              observation.is_anomaly === true ? 'Flagged' : 'Observed',
              strongestDeviation
                ? `${humanize(strongestDeviation.feature)} ${formatSigned(strongestDeviation.value)}`
                : null,
            ]
              .filter(Boolean)
              .join(' · '),
          }
        })}
      />
      <p className={styles.causalityNote}>
        Unusual observed feature combinations; not a forecast, probability, or causal claim.
      </p>
    </section>
  )
}

type PriceSeries = {
  asset: string
  points: Array<{ timestamp: number; label: string; value: number }>
}

const sampled = <T,>(items: T[], maximum: number): T[] => {
  if (items.length <= maximum) return items
  const step = (items.length - 1) / (maximum - 1)
  return Array.from({ length: maximum }, (_, index) => items[Math.round(index * step)]!)
}

const PriceSummaries = ({ summaries }: { summaries: RecordValue[] }): ReactNode =>
  summaries.map((summary, index) => (
    <article key={`${display(text(summary.asset_id))}:${index}`}>
      <strong>{display(text(summary.asset_id))}</strong>
      <span>{formatSigned(number(summary.total_return), true)} return</span>
      <small>
        {formatCompact(number(summary.start_price))} → {formatCompact(number(summary.end_price))}
      </small>
    </article>
  ))

const PriceContextResult = ({ payload }: { payload: RecordValue }): ReactNode => {
  const grouped = new Map<string, PriceSeries['points']>()
  records(payload.series).forEach((point) => {
    const asset = text(point.asset_id)
    const timestampText = text(point.timestamp)
    const value = number(point.adjusted_close)
    const timestamp = timestampText ? Date.parse(timestampText) : Number.NaN
    if (!asset || value === null || !Number.isFinite(timestamp)) return
    const points = grouped.get(asset) || []
    points.push({ timestamp, label: timestampText!, value })
    grouped.set(asset, points)
  })
  const series: PriceSeries[] = [...grouped.entries()].slice(0, 6).map(([asset, points]) => ({
    asset,
    points: sampled(
      [...points].sort((left, right) => left.timestamp - right.timestamp),
      160
    ),
  }))
  const chartSeries = series.filter((item) => item.points.length > 1)
  const allPoints = chartSeries.flatMap((item) => item.points)
  const minTime = allPoints.length ? Math.min(...allPoints.map((point) => point.timestamp)) : 0
  const maxTime = allPoints.length ? Math.max(...allPoints.map((point) => point.timestamp)) : 0
  const minValue = allPoints.length ? Math.min(...allPoints.map((point) => point.value)) : 0
  const maxValue = allPoints.length ? Math.max(...allPoints.map((point) => point.value)) : 0
  const x = (value: number): number =>
    minTime === maxTime ? 360 : 54 + ((value - minTime) / (maxTime - minTime)) * 630
  const y = (value: number): number =>
    minValue === maxValue ? 110 : 190 - ((value - minValue) / (maxValue - minValue)) * 150
  const summaries = boundedRows(records(payload.summaries), 12)

  return (
    <section className={styles.nativeResult} data-native-result="price-context">
      <header className={styles.nativeResultHeader}>
        <div>
          <span>Observed price context</span>
          <h4>Adjusted close</h4>
        </div>
        {payload.series_truncated === true ? <small>Retained series is truncated</small> : null}
      </header>
      {chartSeries.length ? (
        <>
          <svg
            className={styles.priceChart}
            viewBox="0 0 740 220"
            role="img"
            aria-label="Observed adjusted-close series"
            data-testid="market-price-series"
          >
            <path className={styles.chartAxis} d="M 42 34 V 194 H 702" />
            {[0, 1, 2].map((line) => (
              <path className={styles.chartGrid} d={`M 42 ${44 + line * 72} H 702`} key={line} />
            ))}
            {chartSeries.map((item, index) => {
              const path = item.points
                .map(
                  (point, pointIndex) =>
                    `${pointIndex ? 'L' : 'M'} ${x(point.timestamp)} ${y(point.value)}`
                )
                .join(' ')
              return (
                <path
                  className={styles.priceSeries}
                  data-series={index}
                  d={path}
                  key={item.asset}
                />
              )
            })}
            <text className={styles.chartLabel} x="45" y="24">
              {formatCompact(maxValue)}
            </text>
            <text className={styles.chartLabel} x="45" y="213">
              {formatCompact(minValue)}
            </text>
          </svg>
          <div className={styles.seriesLegend} aria-label="Price series legend">
            {chartSeries.map((item, index) => (
              <span key={item.asset} data-series={index}>
                <i aria-hidden="true" /> {item.asset}
              </span>
            ))}
          </div>
          {summaries.length ? (
            <div className={styles.priceSummaries} aria-label="Price summary">
              <PriceSummaries summaries={summaries} />
            </div>
          ) : null}
        </>
      ) : summaries.length ? (
        <div className={styles.priceSummaries}>
          <PriceSummaries summaries={summaries} />
        </div>
      ) : (
        <EmptyResult />
      )}
    </section>
  )
}

const SentimentTimelineResult = ({ payload }: { payload: RecordValue }): ReactNode => {
  const points = boundedRows(records(payload.points), 16)
  return (
    <section className={styles.nativeResult} data-native-result="sentiment-timeline">
      <header className={styles.nativeResultHeader}>
        <div>
          <span>Observed sentiment timeline</span>
          <h4>{formatCompact(integer(payload.articles_considered))} articles considered</h4>
        </div>
        <small>{humanize(text(payload.frequency))}</small>
      </header>
      {payload.points_truncated === true ? (
        <p className={styles.resultNotice}>The retained timeline is truncated.</p>
      ) : null}
      {points.length ? (
        <div className={styles.sentimentTimeline}>
          <div className={styles.sentimentLegend} aria-label="Sentiment legend">
            <span data-sentiment="positive">Positive</span>
            <span data-sentiment="neutral">Neutral</span>
            <span data-sentiment="negative">Negative</span>
          </div>
          {points.map((point, index) => {
            const positive = integer(point.positive_count) || 0
            const neutral = integer(point.neutral_count) || 0
            const negative = integer(point.negative_count) || 0
            const total = Math.max(1, positive + neutral + negative)
            return (
              <div
                className={styles.sentimentRow}
                key={`${display(text(point.period_start))}:${index}`}
              >
                <strong>{text(point.period_start)?.slice(0, 10) || `Period ${index + 1}`}</strong>
                <span className={styles.sentimentStack} aria-label={`${total} observed articles`}>
                  <i data-sentiment="positive" style={{ width: `${(positive / total) * 100}%` }} />
                  <i data-sentiment="neutral" style={{ width: `${(neutral / total) * 100}%` }} />
                  <i data-sentiment="negative" style={{ width: `${(negative / total) * 100}%` }} />
                </span>
                <span>
                  Mean <b>{formatSigned(number(point.mean_sentiment))}</b>
                </span>
              </div>
            )
          })}
        </div>
      ) : (
        <EmptyResult />
      )}
    </section>
  )
}

const NewsPriceResult = ({ payload }: { payload: RecordValue }): ReactNode => {
  const events = boundedRows(records(payload.events), 14)
  const maximum = Math.max(
    0.000001,
    ...events.map((event) => Math.abs(number(event.forward_return) || 0))
  )
  const correlation = number(payload.sentiment_return_correlation)
  return (
    <section className={styles.nativeResult} data-native-result="news-price-relationship">
      <header className={styles.nativeResultHeader}>
        <div>
          <span>Aligned news and returns</span>
          <h4>{formatCompact(integer(payload.aligned_event_count))} aligned events</h4>
        </div>
        <small>
          {correlation === null
            ? 'Correlation unavailable'
            : `Descriptive correlation ${formatSigned(correlation)}`}
        </small>
      </header>
      <div className={styles.resultMetadata} aria-label="Alignment summary">
        <span>
          Coverage <b>{formatPercentage(number(payload.coverage_ratio))}</b>
        </span>
        <span>
          Horizon <b>{formatCompact(integer(payload.return_horizon_sessions))} sessions</b>
        </span>
        {payload.events_truncated === true ? <span>Retained events truncated</span> : null}
      </div>
      {events.length ? (
        <div className={styles.returnEvents}>
          {events.map((event, index) => {
            const value = number(event.forward_return)
            return (
              <article key={`${display(text(event.news_id))}:${index}`}>
                <div>
                  <strong>{display(text(event.asset_id))}</strong>
                  <small>{text(event.published_at)?.slice(0, 10) || 'Date unavailable'}</small>
                </div>
                <span>{humanize(text(event.sentiment_label))}</span>
                <span className={styles.returnTrack} aria-hidden="true">
                  <i
                    data-negative={value !== null && value < 0 ? true : undefined}
                    style={{ width: `${Math.max(3, (Math.abs(value || 0) / maximum) * 100)}%` }}
                  />
                </span>
                <strong>{formatSigned(value, true)}</strong>
              </article>
            )
          })}
        </div>
      ) : (
        <EmptyResult />
      )}
      <p className={styles.causalityNote}>Observed descriptive relationship; no causal claim.</p>
    </section>
  )
}

type NetworkNode = { id: string; rank: number | null; score: number | null }
type NetworkEdge = { source: string; target: string; correlation: number | null }

const RelationshipResult = ({ payload }: { payload: RecordValue }): ReactNode => {
  const ranked = boundedRows(records(payload.central_assets), 10)
  const edges: NetworkEdge[] = boundedRows(records(payload.strongest_edges), 16).flatMap((edge) => {
    const source = text(edge.source_asset_id)
    const target = text(edge.target_asset_id)
    return source && target ? [{ source, target, correlation: number(edge.correlation) }] : []
  })
  const rankedById = new Map(
    ranked.flatMap((item): Array<[string, NetworkNode]> => {
      const id = text(item.asset_id)
      return id ? [[id, { id, rank: integer(item.rank), score: number(item.centrality) }]] : []
    })
  )
  edges.forEach((edge) => {
    if (!rankedById.has(edge.source)) {
      rankedById.set(edge.source, { id: edge.source, rank: null, score: null })
    }
    if (!rankedById.has(edge.target)) {
      rankedById.set(edge.target, { id: edge.target, rank: null, score: null })
    }
  })
  const nodes = [...rankedById.values()].slice(0, 10)
  const nodeIds = new Set(nodes.map((node) => node.id))
  const retainedEdges = edges.filter((edge) => nodeIds.has(edge.source) && nodeIds.has(edge.target))
  const positions = new Map(
    nodes.map((node, index) => {
      const angle = -Math.PI / 2 + (index / Math.max(1, nodes.length)) * Math.PI * 2
      return [node.id, { x: 350 + Math.cos(angle) * 230, y: 150 + Math.sin(angle) * 98 }] as const
    })
  )
  const maximumScore = Math.max(0.000001, ...nodes.map((node) => node.score || 0))

  return (
    <section className={styles.nativeResult} data-native-result="market-relationships">
      <header className={styles.nativeResultHeader}>
        <div>
          <span>Retained strongest relationships</span>
          <h4>Recent Return Correlation</h4>
        </div>
        <small>
          {formatCompact(integer(payload.node_count))} nodes ·{' '}
          {formatCompact(integer(payload.edge_count))} edges in source graph
        </small>
      </header>
      {nodes.length ? (
        <div className={styles.relationshipLayout}>
          <svg
            className={styles.relationshipGraph}
            viewBox="0 0 700 300"
            role="img"
            aria-label="Retained strongest market relationships"
            data-testid="market-relationship-network"
          >
            <rect x="1" y="1" width="698" height="298" rx="12" />
            {retainedEdges.map((edge, index) => {
              const source = positions.get(edge.source)!
              const target = positions.get(edge.target)!
              return (
                <g key={`${edge.source}:${edge.target}:${index}`}>
                  <line
                    className={styles.relationshipEdge}
                    data-negative={
                      edge.correlation !== null && edge.correlation < 0 ? true : undefined
                    }
                    x1={source.x}
                    y1={source.y}
                    x2={target.x}
                    y2={target.y}
                    style={{ strokeWidth: 2 + Math.abs(edge.correlation || 0) * 3 }}
                  />
                  {edge.correlation !== null ? (
                    <text
                      className={styles.relationshipEdgeLabel}
                      x={(source.x + target.x) / 2}
                      y={(source.y + target.y) / 2 - 7}
                      textAnchor="middle"
                    >
                      {formatSigned(edge.correlation)}
                    </text>
                  ) : null}
                </g>
              )
            })}
            {nodes.map((node) => {
              const position = positions.get(node.id)!
              const radius = 24 + ((node.score || 0) / maximumScore) * 12
              return (
                <g className={styles.relationshipNode} key={node.id}>
                  <title>
                    {node.id}
                    {node.rank ? ` · rank ${node.rank}` : ''}
                    {node.score === null ? '' : ` · centrality ${node.score}`}
                  </title>
                  <circle cx={position.x} cy={position.y} r={radius} />
                  <text x={position.x} y={position.y - 2} textAnchor="middle">
                    {node.id.slice(0, 9)}
                  </text>
                  {node.score !== null ? (
                    <text x={position.x} y={position.y + 15} textAnchor="middle">
                      {formatCompact(node.score)}
                    </text>
                  ) : null}
                </g>
              )
            })}
          </svg>
          <ol className={styles.relationshipRanking} aria-label="Ranked relationship assets">
            {nodes
              .filter((node) => node.rank !== null)
              .sort((left, right) => (left.rank || 0) - (right.rank || 0))
              .map((node) => (
                <li key={node.id}>
                  <span>{node.rank}</span>
                  <strong>{node.id}</strong>
                  <b>{formatCompact(node.score)}</b>
                </li>
              ))}
          </ol>
        </div>
      ) : (
        <EmptyResult />
      )}
      <p className={styles.causalityNote}>
        Partial view of the strongest retained edges, not the complete source graph.
      </p>
    </section>
  )
}

const IntradayResult = ({ payload }: { payload: RecordValue }): ReactNode => {
  const observations = boundedRows(records(payload.observations), 16)
  const metric = text(payload.rank_by)
  return (
    <section className={styles.nativeResult} data-native-result="intraday-scan">
      <header className={styles.nativeResultHeader}>
        <div>
          <span>Ranked intraday sessions</span>
          <h4>{humanize(metric)}</h4>
        </div>
        <small>{humanize(text(payload.direction))}</small>
      </header>
      <RankedBars
        valueLabel={humanize(metric)}
        percentage={metric !== 'volume' && metric !== 'bar_count'}
        items={observations.map((observation, index) => ({
          id: `${display(text(observation.asset_id))}:${display(text(observation.session))}:${index}`,
          rank: integer(observation.rank),
          label: `${display(text(observation.asset_id))} · ${dateOnly(observation.session) || 'Date unavailable'}`,
          value: metric ? number(observation[metric]) : null,
          detail: `${formatCompact(integer(observation.bar_count))} minute bars`,
        }))}
      />
    </section>
  )
}

const NativeMarketResult = ({ result }: { result: AnalyticsResult }): ReactNode => {
  const payload = result.payload
  if (result.status === 'failed') {
    return <EmptyResult message={result.error?.message || 'The tool call failed.'} />
  }
  if (!payload) return <EmptyResult />
  switch (result.operationId) {
    case 'market_scan':
      return <MarketScanResult payload={payload} />
    case 'market_anomaly_scan':
      return <AnomalyResult payload={payload} />
    case 'price_context':
      return <PriceContextResult payload={payload} />
    case 'sentiment_timeline':
      return <SentimentTimelineResult payload={payload} />
    case 'analyze_news_price_relationship':
      return <NewsPriceResult payload={payload} />
    case 'analyze_market_relationships':
      return <RelationshipResult payload={payload} />
    case 'intraday_scan':
      return <IntradayResult payload={payload} />
  }
}

export const MarketToolInspector = ({
  toolTitle,
  receipt,
}: MarketToolInspectorProps): ReactNode => {
  const result = receipt?.content
  if (!receipt || !result) {
    return (
      <section className={styles.marketToolInspector} data-testid="market-tool-inspector">
        <EmptyResult message="No display-safe market-tool receipt is available at this replay step." />
      </section>
    )
  }

  const acceleration = gpuAccelerationForReceipt(receipt)
  const engine = result.engine
  const device = engine?.device.toUpperCase() ?? 'CPU'
  const assets = number(result.assetCount) ?? assetCount(result)
  const universe = text(result.publicParameters.universe_id)
  const warnings = [...result.warnings, ...result.limitations]
  const engineLabel = engine
    ? `${acceleration?.label || engineLibraryLabel(engine.library)} ${packageVersion(engine.version)}`
    : 'Engine not started'
  const { computeMs, totalMs } = result.timing
  // The engine's setup and whole time, as the tool recorded them; results recorded before those fields have
  // the service's total and the call as the agent timed it (whole milliseconds), as before
  const setupMs = number(result.timing.setupMs)
  const engineMs = number(result.timing.engineMs)
  const recordedTiming = setupMs !== null && engineMs !== null
  const otherWork = recordedTiming ? setupMs : totalMs - computeMs
  const timingDetails = engine
    ? [
        { label: 'Other engine work', value: otherWork >= 0 ? otherWork : null },
        { label: `${device} compute`, value: computeMs },
        { label: 'Engine total', value: recordedTiming ? engineMs : totalMs },
      ].filter((item): item is { label: string; value: number } => item.value !== null)
    : []
  const toolTotal = recordedTiming ? totalMs : receipt.durationMs

  return (
    <section
      className={styles.marketToolInspector}
      data-testid="market-tool-inspector"
      data-operation={result.operationId}
      data-accelerated={acceleration ? 'true' : 'false'}
    >
      <section className={styles.toolReceipt} data-testid="market-tool-receipt">
        <header className={styles.toolReceiptHeader}>
          <div>
            <span>{acceleration ? 'NVIDIA GPU tool receipt' : 'Tool execution receipt'}</span>
            <h4>{toolTitle}</h4>
            <p>{requestSummary(result)}</p>
          </div>
          <div className={styles.engineIdentity} data-accelerated={acceleration ? true : undefined}>
            <span aria-hidden="true">{device}</span>
            <div>
              <strong>{engineLabel}</strong>
              <small>{text(engine?.engineId) ?? engine?.library ?? 'No engine reported'}</small>
            </div>
          </div>
        </header>

        <dl className={styles.receiptFacts}>
          <div>
            <dt>Input</dt>
            <dd>{formatCompact(result.rowsScanned)} rows</dd>
            <small>
              {assets !== null ? `${formatCompact(assets)} assets` : (universe ?? 'Rows scanned')}
            </small>
          </div>
          <div>
            <dt>Execution</dt>
            {/* As the original: GPU acceleration shows in the receipt; a fallback check is not recorded */}
            <dd>{acceleration ? 'GPU observed' : engine ? `${device} reported` : 'Not run'}</dd>
            <small>No fallback reported</small>
          </div>
          <div>
            <dt>Tool total</dt>
            <dd>{formatDuration(toolTotal)}</dd>
            <small>Measured execution</small>
          </div>
          <div>
            <dt>Return</dt>
            <dd>{formatCompact(outputRows(result))} rows</dd>
            <small>{humanize(result.status)}</small>
          </div>
        </dl>

        {timingDetails.length ? (
          <dl className={styles.timingDetails} aria-label="Engine timing details">
            {timingDetails.map((item) => (
              <div key={item.label}>
                <dt>{item.label}</dt>
                <dd>{formatDuration(item.value)}</dd>
              </div>
            ))}
          </dl>
        ) : null}

        <div className={styles.receiptSource}>
          <span>Source</span>
          <strong>{result.databaseName}</strong>
          <small>{result.sourceId}</small>
        </div>
      </section>

      <NativeMarketResult result={result} />

      {warnings.length ? (
        <section className={styles.toolNotices} aria-label="Tool notices">
          <strong>Recorded notices</strong>
          <ul>
            {warnings.slice(0, 6).map((warning, index) => (
              <li key={`${warning}:${index}`}>{warning}</li>
            ))}
          </ul>
        </section>
      ) : null}
    </section>
  )
}
