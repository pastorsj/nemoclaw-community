// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

'use client'

import {
  type FC,
  type FocusEvent as ReactFocusEvent,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type WheelEvent as ReactWheelEvent,
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from 'react'
import Image from 'next/image'
import type { NodeLogo } from '../registry'
import styles from '../execution-workspace.module.css'
import { ExecutionNodeIcon } from './ExecutionNodeIcon'
import type { ExecutionNodeGpuAcceleration } from './gpu-acceleration'
import {
  HERMES_EXECUTION_CANVAS_HEIGHT,
  HERMES_EXECUTION_CANVAS_WIDTH,
  EXECUTION_NODE_HEIGHT,
  EXECUTION_NODE_WIDTH,
  isInspectableNode,
  isInspectableExecutionNodeState,
  type ExecutionGraphEdgeDefinition,
  type ExecutionGraphNodeDefinition,
  type ExecutionGraphViewModel,
  type InspectableExecutionNodeId,
} from './graph-model'

export type ExecutionGraphPoint = { x: number; y: number }

type Point = ExecutionGraphPoint
type Axis = 'horizontal' | 'vertical'

export type OrthogonalEdgeGeometry = {
  path: string
  points: ExecutionGraphPoint[]
  labelPosition: ExecutionGraphPoint | null
  labelPlacement: 'automatic' | 'override' | null
}

export type ExecutionGraphProps = {
  model: ExecutionGraphViewModel
  selectedNodeId?: InspectableExecutionNodeId | null
  /**
   * Optional replay-scoped allowlist. When supplied, an observed node remains
   * visible but is interactive only while its canonical event is current.
   */
  interactiveNodeIds?: ReadonlySet<InspectableExecutionNodeId>
  /** Terminal runs can say "not used"; unfinished live runs can only say "not observed yet". */
  unobservedLegendLabel?: string
  initialZoom?: number
  structuredDatabaseProviderMark?: { src: string; alt: string }
  gpuAccelerations?: ReadonlyMap<string, ExecutionNodeGpuAcceleration>
  /** Logos of the libraries a node's tool is built on, by node id */
  nodeLogos?: ReadonlyMap<string, readonly NodeLogo[]>
  onNodeSelect?: (nodeId: InspectableExecutionNodeId) => void
}

const MIN_ZOOM = 0.28
const MAX_ZOOM = 1.4
const ZOOM_STEP = 0.1
const FIT_VIEWPORT_PADDING = 16
const FIT_GRAPH_MARGIN = 44
const MIN_MARKET_FOCUS_ZOOM = 0.58
const GPU_TOOLTIP_WIDTH = 292
const GPU_TOOLTIP_MARGIN = 12
const GPU_TOOLTIP_ESTIMATED_HEIGHT = 150

// Chromium renders these uppercase, 14px, 750-weight labels at roughly
// 9.5px/character. Preserve visible breathing room on both sides rather than
// allowing the text to touch or overflow its readability pill.
const edgeLabelWidth = (label: string): number => Math.max(52, label.length * 9.7 + 22)

const clampZoom = (value: number): number => Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, value))

export const fitObservedExecutionNodes = ({
  nodes,
  viewportWidth,
  viewportHeight,
  maxZoom,
}: {
  nodes: ExecutionGraphViewModel['nodes']
  viewportWidth: number
  viewportHeight: number
  maxZoom: number
}): { zoom: number; pan: Point } | null => {
  if (viewportWidth <= 0 || viewportHeight <= 0 || !nodes.length) return null
  const observedNodes = nodes.filter((node) => node.state !== 'unobserved')
  const observedMarketNodes = observedNodes.filter((node) => node.group === 'market-analytics')
  const observedMarketReturn = observedMarketNodes.length
    ? observedNodes.filter((node) => node.id === 'synthesis')
    : []
  // A completed market run can span the entire static manifest from intake to
  // answer. Fitting every observed node makes the operation labels unreadable
  // on a demo display. Focus the observed market cluster and its return point;
  // the complete static topology remains available through pan and zoom.
  const targets = observedMarketNodes.length
    ? [...observedMarketNodes, ...observedMarketReturn]
    : observedNodes.length
      ? observedNodes
      : nodes
  const left = Math.min(...targets.map((node) => node.x)) - FIT_GRAPH_MARGIN
  const top = Math.min(...targets.map((node) => node.y)) - FIT_GRAPH_MARGIN
  const right =
    Math.max(...targets.map((node) => node.x + (node.width || EXECUTION_NODE_WIDTH))) +
    FIT_GRAPH_MARGIN
  const bottom =
    Math.max(...targets.map((node) => node.y + (node.height || EXECUTION_NODE_HEIGHT))) +
    FIT_GRAPH_MARGIN
  const contentWidth = Math.max(1, right - left)
  const contentHeight = Math.max(1, bottom - top)
  const availableWidth = Math.max(1, viewportWidth - FIT_VIEWPORT_PADDING * 2)
  const availableHeight = Math.max(1, viewportHeight - FIT_VIEWPORT_PADDING * 2)
  const fittedZoom = clampZoom(
    Math.min(clampZoom(maxZoom), availableWidth / contentWidth, availableHeight / contentHeight)
  )
  const zoom = observedMarketNodes.length
    ? Math.min(clampZoom(maxZoom), Math.max(MIN_MARKET_FOCUS_ZOOM, fittedZoom))
    : fittedZoom

  return {
    zoom,
    pan: {
      x: FIT_VIEWPORT_PADDING + (availableWidth - contentWidth * zoom) / 2 - left * zoom,
      y: FIT_VIEWPORT_PADDING + (availableHeight - contentHeight * zoom) / 2 - top * zoom,
    },
  }
}

export const isGraphPinchZoomGesture = (event: { ctrlKey: boolean; metaKey: boolean }): boolean =>
  event.ctrlKey || event.metaKey

const portPoint = (
  node: ExecutionGraphNodeDefinition,
  port: ExecutionGraphEdgeDefinition['fromPort'],
  offset = 0
): ExecutionGraphPoint => {
  const width = node.width || EXECUTION_NODE_WIDTH
  const height = node.height || EXECUTION_NODE_HEIGHT
  if (port === 'top') return { x: node.x + width / 2 + offset, y: node.y }
  if (port === 'bottom') return { x: node.x + width / 2 + offset, y: node.y + height }
  if (port === 'left') return { x: node.x, y: node.y + height / 2 + offset }
  return { x: node.x + width, y: node.y + height / 2 + offset }
}

const portAxis = (port: ExecutionGraphEdgeDefinition['fromPort']): Axis =>
  port === 'left' || port === 'right' ? 'horizontal' : 'vertical'

const segmentAxis = (from: ExecutionGraphPoint, to: ExecutionGraphPoint): Axis | null => {
  if (from.y === to.y && from.x !== to.x) return 'horizontal'
  if (from.x === to.x && from.y !== to.y) return 'vertical'
  return null
}

const oppositeAxis = (axis: Axis): Axis => (axis === 'horizontal' ? 'vertical' : 'horizontal')

const appendPoint = (points: ExecutionGraphPoint[], point: ExecutionGraphPoint): void => {
  const previous = points.at(-1)
  if (previous?.x === point.x && previous.y === point.y) return

  const beforePrevious = points.at(-2)
  if (
    beforePrevious &&
    previous &&
    ((beforePrevious.x === previous.x && previous.x === point.x) ||
      (beforePrevious.y === previous.y && previous.y === point.y))
  ) {
    points[points.length - 1] = point
    return
  }

  points.push(point)
}

/**
 * Converts author-provided control points into a deterministic Manhattan route.
 * A control point can therefore never introduce a diagonal SVG segment. The
 * first and last diagonal pairs respect the source and target port directions;
 * intermediate pairs alternate away from the preceding segment.
 */
export const orthogonalizeEdgePoints = (
  controlPoints: readonly ExecutionGraphPoint[],
  fromPort: ExecutionGraphEdgeDefinition['fromPort'],
  toPort: ExecutionGraphEdgeDefinition['toPort']
): ExecutionGraphPoint[] => {
  if (controlPoints.length <= 1) return [...controlPoints]

  const route: ExecutionGraphPoint[] = [controlPoints[0]]
  const finalIndex = controlPoints.length - 1

  for (let index = 1; index <= finalIndex; index += 1) {
    const target = controlPoints[index]
    const current = route.at(-1)
    if (!current) continue

    if (current.x === target.x || current.y === target.y) {
      appendPoint(route, target)
      continue
    }

    const directConnection = index === 1 && index === finalIndex
    const startAxis = portAxis(fromPort)
    const endAxis = portAxis(toPort)

    if (directConnection && startAxis === endAxis) {
      if (startAxis === 'horizontal') {
        const middleX = current.x + (target.x - current.x) / 2
        appendPoint(route, { x: middleX, y: current.y })
        appendPoint(route, { x: middleX, y: target.y })
      } else {
        const middleY = current.y + (target.y - current.y) / 2
        appendPoint(route, { x: current.x, y: middleY })
        appendPoint(route, { x: target.x, y: middleY })
      }
      appendPoint(route, target)
      continue
    }

    const priorAxis = route.length > 1 ? segmentAxis(route[route.length - 2], current) : null
    const firstAxis =
      index === 1
        ? startAxis
        : index === finalIndex
          ? oppositeAxis(endAxis)
          : priorAxis
            ? oppositeAxis(priorAxis)
            : Math.abs(target.x - current.x) >= Math.abs(target.y - current.y)
              ? 'horizontal'
              : 'vertical'

    appendPoint(
      route,
      firstAxis === 'horizontal' ? { x: target.x, y: current.y } : { x: current.x, y: target.y }
    )
    appendPoint(route, target)
  }

  return route
}

const automaticLabelPosition = (
  points: readonly ExecutionGraphPoint[],
  minimumSegmentWidth = 0,
  labelWidth = 0,
  nodes: readonly ExecutionGraphNodeDefinition[] = []
): ExecutionGraphPoint | null => {
  const horizontalSegments = points
    .slice(0, -1)
    .map((point, index) => ({ from: point, to: points[index + 1] }))
    .filter(({ from, to }) => segmentAxis(from, to) === 'horizontal')
    .sort((left, right) => Math.abs(right.to.x - right.from.x) - Math.abs(left.to.x - left.from.x))

  for (const segment of horizontalSegments) {
    if (Math.abs(segment.to.x - segment.from.x) < minimumSegmentWidth) continue
    const lineY = segment.from.y
    const x = segment.from.x + (segment.to.x - segment.from.x) / 2
    // Prefer a compact offset. The wider fallback handles the useful special
    // case where an edge occupies the gap between two aligned nodes.
    const offsets = lineY < 32 ? [15, -15, 56, -56] : [-15, 15, -56, 56]
    for (const offset of offsets) {
      const position = { x, y: lineY + offset }
      if (!labelOverlapsNodes(position, labelWidth, nodes)) return position
    }
  }

  return null
}

const labelOverlapsNodes = (
  position: ExecutionGraphPoint,
  labelWidth: number,
  nodes: readonly ExecutionGraphNodeDefinition[]
): boolean => {
  const halfWidth = labelWidth / 2
  const halfHeight = 12
  return nodes.some(
    (node) =>
      position.x - halfWidth < node.x + (node.width || EXECUTION_NODE_WIDTH) &&
      position.x + halfWidth > node.x &&
      position.y - halfHeight < node.y + (node.height || EXECUTION_NODE_HEIGHT) &&
      position.y + halfHeight > node.y
  )
}

export const buildOrthogonalEdgeGeometry = (
  edge: ExecutionGraphEdgeDefinition,
  nodes: Map<string, ExecutionGraphNodeDefinition>
): OrthogonalEdgeGeometry => {
  const from = nodes.get(edge.from)
  const to = nodes.get(edge.to)
  if (!from || !to) {
    return { path: '', points: [], labelPosition: null, labelPlacement: null }
  }
  const controlPoints = [
    portPoint(from, edge.fromPort, edge.fromOffset),
    ...(edge.via || []).map(([x, y]) => ({ x, y })),
    portPoint(to, edge.toPort, edge.toOffset),
  ]
  const points = orthogonalizeEdgePoints(controlPoints, edge.fromPort, edge.toPort)
  const allNodes = [...nodes.values()]
  const overridePosition =
    edge.label && edge.labelAt ? { x: edge.labelAt[0], y: edge.labelAt[1] } : null
  const estimatedLabelWidth = edge.label ? edgeLabelWidth(edge.label) : 0
  const clearOverridePosition =
    overridePosition && !labelOverlapsNodes(overridePosition, estimatedLabelWidth, allNodes)
      ? overridePosition
      : null
  const preferredAutomaticPosition = edge.label
    ? automaticLabelPosition(points, estimatedLabelWidth + 16, estimatedLabelWidth, allNodes)
    : null
  const fallbackAutomaticPosition =
    edge.label && !preferredAutomaticPosition && !clearOverridePosition
      ? automaticLabelPosition(points, 0, estimatedLabelWidth, allNodes)
      : null
  const automaticPosition = preferredAutomaticPosition || fallbackAutomaticPosition
  const labelPosition = automaticPosition || clearOverridePosition

  return {
    path: points.map((point, index) => `${index ? 'L' : 'M'} ${point.x} ${point.y}`).join(' '),
    points,
    labelPosition,
    labelPlacement: automaticPosition ? 'automatic' : overridePosition ? 'override' : null,
  }
}

export const ExecutionGraph: FC<ExecutionGraphProps> = ({
  model,
  selectedNodeId = null,
  interactiveNodeIds,
  unobservedLegendLabel = 'Never activated',
  initialZoom = 0.86,
  structuredDatabaseProviderMark,
  gpuAccelerations,
  nodeLogos,
  onNodeSelect,
}) => {
  const markerPrefix = useId().replace(/[^a-zA-Z0-9_-]/g, '')
  const shellRef = useRef<HTMLDivElement>(null)
  const viewportRef = useRef<HTMLDivElement>(null)
  const dragRef = useRef<{ pointerId: number; origin: Point; pan: Point } | null>(null)
  const manuallyPositionedRef = useRef(false)
  const [zoom, setZoom] = useState(() => clampZoom(initialZoom))
  const [pan, setPan] = useState<Point>({ x: 18, y: 16 })
  const [gpuTooltip, setGpuTooltip] = useState<{
    nodeId: string
    left: number
    top: number
    placement: 'above' | 'below'
    alignment: 'left' | 'right'
  } | null>(null)
  const canvasWidth = model.canvasWidth || HERMES_EXECUTION_CANVAS_WIDTH
  const canvasHeight = model.canvasHeight || HERMES_EXECUTION_CANVAS_HEIGHT
  const nodeDefinitions = useMemo(
    () => new Map(model.nodes.map((node) => [node.id, node] as const)),
    [model.nodes]
  )

  const fitObservedTopology = useCallback((): void => {
    if (manuallyPositionedRef.current) return
    const viewport = viewportRef.current
    if (!viewport) return
    const fitted = fitObservedExecutionNodes({
      nodes: model.nodes,
      viewportWidth: viewport.clientWidth,
      viewportHeight: viewport.clientHeight,
      maxZoom: initialZoom,
    })
    if (!fitted) return
    setZoom(fitted.zoom)
    setPan(fitted.pan)
  }, [initialZoom, model.nodes])

  useEffect(() => {
    const viewport = viewportRef.current
    if (!viewport) return
    const frame = window.requestAnimationFrame(fitObservedTopology)
    const observer =
      typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(() => fitObservedTopology())
    observer?.observe(viewport)
    return () => {
      window.cancelAnimationFrame(frame)
      observer?.disconnect()
    }
  }, [fitObservedTopology])

  const zoomAround = useCallback(
    (nextZoom: number, anchor?: Point): void => {
      const clamped = clampZoom(nextZoom)
      if (clamped === zoom) return
      const viewport = viewportRef.current
      const fallbackAnchor = {
        x: (viewport?.clientWidth || 900) / 2,
        y: (viewport?.clientHeight || 600) / 2,
      }
      const target = anchor || fallbackAnchor
      setPan((priorPan) => ({
        x: target.x - ((target.x - priorPan.x) / zoom) * clamped,
        y: target.y - ((target.y - priorPan.y) / zoom) * clamped,
      }))
      setZoom(clamped)
    },
    [zoom]
  )

  const handleWheel = useCallback(
    (event: ReactWheelEvent<HTMLDivElement>): void => {
      // Trackpad scrolling remains page scrolling. Browsers report a pinch as
      // Ctrl/Meta + wheel, which is the only wheel gesture consumed here.
      if (!isGraphPinchZoomGesture(event)) return
      event.preventDefault()
      manuallyPositionedRef.current = true
      const rect = viewportRef.current?.getBoundingClientRect()
      const anchor = {
        x: event.clientX - (rect?.left || 0),
        y: event.clientY - (rect?.top || 0),
      }
      const factor = Math.exp(-event.deltaY * 0.002)
      zoomAround(zoom * factor, anchor)
    },
    [zoom, zoomAround]
  )

  const handlePointerDown = (event: ReactPointerEvent<HTMLDivElement>): void => {
    const target = event.target as HTMLElement
    if (target.closest('[data-execution-node="true"]')) return
    manuallyPositionedRef.current = true
    dragRef.current = {
      pointerId: event.pointerId,
      origin: { x: event.clientX, y: event.clientY },
      pan,
    }
    event.currentTarget.setPointerCapture?.(event.pointerId)
    event.currentTarget.dataset.dragging = 'true'
  }

  const handlePointerMove = (event: ReactPointerEvent<HTMLDivElement>): void => {
    const drag = dragRef.current
    if (!drag || drag.pointerId !== event.pointerId) return
    setPan({
      x: drag.pan.x + event.clientX - drag.origin.x,
      y: drag.pan.y + event.clientY - drag.origin.y,
    })
  }

  const stopDragging = (event: ReactPointerEvent<HTMLDivElement>): void => {
    if (dragRef.current?.pointerId !== event.pointerId) return
    dragRef.current = null
    event.currentTarget.releasePointerCapture?.(event.pointerId)
    delete event.currentTarget.dataset.dragging
  }

  const showGpuTooltip = (nodeId: string, anchor: HTMLElement): void => {
    const shell = shellRef.current
    if (!shell || !gpuAccelerations?.has(nodeId)) return
    const shellBox = shell.getBoundingClientRect()
    const toolNode = anchor.closest<HTMLElement>('[data-execution-node="true"]') || anchor
    const anchorBox = toolNode.getBoundingClientRect()
    const availableLeft = anchorBox.left - shellBox.left
    const availableRight = shellBox.right - anchorBox.right
    const alignment =
      availableLeft >= GPU_TOOLTIP_WIDTH + GPU_TOOLTIP_MARGIN || availableLeft >= availableRight
        ? 'left'
        : 'right'
    const requestedLeft =
      alignment === 'left'
        ? anchorBox.left - shellBox.left - GPU_TOOLTIP_WIDTH
        : anchorBox.right - shellBox.left
    const maximumLeft = Math.max(
      GPU_TOOLTIP_MARGIN,
      shellBox.width - GPU_TOOLTIP_WIDTH - GPU_TOOLTIP_MARGIN
    )
    const left = Math.round(Math.min(maximumLeft, Math.max(GPU_TOOLTIP_MARGIN, requestedLeft)))
    const availableBelow = shellBox.bottom - anchorBox.bottom
    const placement =
      availableBelow >= GPU_TOOLTIP_ESTIMATED_HEIGHT + GPU_TOOLTIP_MARGIN ? 'below' : 'above'
    setGpuTooltip({
      nodeId,
      left,
      top: Math.round(
        placement === 'above' ? anchorBox.top - shellBox.top : anchorBox.bottom - shellBox.top
      ),
      placement,
      alignment,
    })
  }

  const activeGpuAcceleration = gpuTooltip ? gpuAccelerations?.get(gpuTooltip.nodeId) || null : null

  useEffect(() => {
    setGpuTooltip((current) => (current && !gpuAccelerations?.has(current.nodeId) ? null : current))
  }, [gpuAccelerations])

  return (
    <div ref={shellRef} className={styles.graphShell}>
      <div className={styles.graphStateLegend} role="list" aria-label="Execution graph legend">
        <span className={styles.graphStateLegendItem} role="listitem">
          <i data-availability="available-now" aria-hidden="true" />
          Available now
        </span>
        <span className={styles.graphStateLegendItem} role="listitem">
          <i data-availability="used-in-run" aria-hidden="true" />
          Activated in this run
        </span>
        <span className={styles.graphStateLegendItem} role="listitem">
          <i data-availability="not-observed" aria-hidden="true" />
          {unobservedLegendLabel}
        </span>
        {(gpuAccelerations?.size || 0) > 0 ? (
          <span
            className={[styles.graphStateLegendItem, styles.gpuLegendItem].join(' ')}
            role="listitem"
            title="Hover the GPU badge on an accelerated node to see its NVIDIA technology."
          >
            <i
              className={[styles.gpuAccelerationBadge, styles.gpuLegendBadge].join(' ')}
              data-testid="gpu-legend-badge"
              aria-hidden="true"
            >
              GPU
            </i>
            <span className={styles.gpuLegendCopy}>
              <span>NVIDIA GPU accelerated</span>
              <small>Hover node badge for details</small>
            </span>
          </span>
        ) : null}
      </div>
      <div className={styles.zoomControls} aria-label="Execution graph zoom controls">
        <button
          type="button"
          className={styles.iconButton}
          onClick={() => {
            manuallyPositionedRef.current = true
            zoomAround(zoom - ZOOM_STEP)
          }}
          disabled={zoom <= MIN_ZOOM}
          aria-label="Zoom out execution graph"
        >
          −
        </button>
        <output aria-label="Execution graph zoom">{Math.round(zoom * 100)}%</output>
        <button
          type="button"
          className={styles.iconButton}
          onClick={() => {
            manuallyPositionedRef.current = true
            zoomAround(zoom + ZOOM_STEP)
          }}
          disabled={zoom >= MAX_ZOOM}
          aria-label="Zoom in execution graph"
        >
          +
        </button>
      </div>

      <div
        ref={viewportRef}
        className={styles.graphViewport}
        data-testid="execution-graph-viewport"
        data-zoom={zoom}
        onWheel={handleWheel}
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={stopDragging}
        onPointerCancel={stopDragging}
      >
        <div
          className={styles.graphCanvas}
          data-testid="execution-graph-canvas"
          style={{
            width: canvasWidth,
            height: canvasHeight,
            transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`,
          }}
        >
          {model.groups?.map((group) => (
            <div
              key={group.id}
              className={styles.graphGroup}
              data-group-id={group.id}
              aria-hidden="true"
              style={{
                left: group.x,
                top: group.y,
                width: group.width,
                height: group.height,
              }}
            >
              <span>{group.label}</span>
              {group.description && <small>{group.description}</small>}
            </div>
          ))}
          <svg
            className={styles.edgeLayer}
            viewBox={`0 0 ${canvasWidth} ${canvasHeight}`}
            width={canvasWidth}
            height={canvasHeight}
            aria-hidden="true"
          >
            <defs>
              {(['unobserved', 'pending', 'running', 'completed', 'failed'] as const).map(
                (state) => (
                  <marker
                    key={state}
                    id={`${markerPrefix}-${state}`}
                    viewBox="0 0 10 10"
                    refX="9"
                    refY="5"
                    markerWidth="7"
                    markerHeight="7"
                    orient="auto-start-reverse"
                  >
                    <path d="M 0 0 L 10 5 L 0 10 z" data-state={state} />
                  </marker>
                )
              )}
            </defs>

            {model.edges.map((edge) => {
              const geometry = buildOrthogonalEdgeGeometry(edge, nodeDefinitions)
              const labelWidth = edge.label ? edgeLabelWidth(edge.label) : 0
              return (
                <g
                  key={edge.id}
                  className={styles.graphEdge}
                  data-edge-id={edge.id}
                  data-edge-from={edge.from}
                  data-edge-to={edge.to}
                  data-state={edge.state}
                  data-current={edge.current || undefined}
                >
                  <path d={geometry.path} markerEnd={`url(#${markerPrefix}-${edge.state})`} />
                  {edge.label && geometry.labelPosition && (
                    <g
                      className={styles.graphEdgeLabel}
                      data-label-placement={geometry.labelPlacement || undefined}
                      transform={`translate(${geometry.labelPosition.x} ${geometry.labelPosition.y})`}
                    >
                      <rect x={-labelWidth / 2} y={-12} width={labelWidth} height={24} rx={6} />
                      <text x={0} y={0} textAnchor="middle" dominantBaseline="central">
                        {edge.label}
                      </text>
                    </g>
                  )}
                </g>
              )
            })}
          </svg>

          {model.nodes.map((node) => {
            const inspectableNodeId =
              isInspectableNode(node.id) &&
              isInspectableExecutionNodeState(node.state) &&
              (!interactiveNodeIds || interactiveNodeIds.has(node.id))
                ? node.id
                : null
            const availability = inspectableNodeId
              ? 'available-now'
              : node.state === 'unobserved'
                ? 'not-observed'
                : 'used-in-run'
            const stateDescriptionId = `${markerPrefix}-${node.id}-state`
            const accelerationDescriptionId = `${markerPrefix}-${node.id}-gpu`
            const acceleration = gpuAccelerations?.get(node.id)
            const nvidiaBranded = node.id === 'nvidia-kumo' || node.id === 'nvidia-ontology'
            const logos = nodeLogos?.get(node.id) ?? []
            const nodeContents = (
              <>
                {node.id === 'structured-database' && structuredDatabaseProviderMark ? (
                  <span
                    className={[styles.nodeIcon, styles.nodeProviderMark].join(' ')}
                    role="img"
                    aria-label={structuredDatabaseProviderMark.alt}
                    style={{ backgroundImage: `url(${structuredDatabaseProviderMark.src})` }}
                  />
                ) : (
                  <span className={styles.nodeIcon} aria-hidden="true">
                    <ExecutionNodeIcon kind={node.icon} />
                  </span>
                )}
                <span className={styles.nodeCopy}>
                  <strong>{node.label}</strong>
                  <small>{node.subtitle}</small>
                </span>
                {nvidiaBranded && (
                  <span className={styles.nodeNvidiaMark} role="img" aria-label="NVIDIA" />
                )}
                {logos.map((logo) => (
                  <Image
                    key={logo.brand}
                    className={styles.nodeBrandLogo}
                    src={logo.src}
                    alt={logo.brand}
                    title={logo.brand}
                    data-brand={logo.brand}
                    width={18}
                    height={18}
                    unoptimized
                  />
                ))}
                {node.count > 1 && (
                  <span
                    className={styles.callCount}
                    aria-label={
                      node.id === 'nvidia-ontology'
                        ? `${node.count} ontology interactions`
                        : `${node.count} calls`
                    }
                    title={
                      node.id === 'nvidia-ontology'
                        ? `${node.count} ontology interactions; open the explorer for the catalog and text-to-query breakdown`
                        : `${node.count} calls`
                    }
                  >
                    ×{node.count}
                  </span>
                )}
                {acceleration && (
                  <>
                    <span
                      className={styles.gpuAccelerationBadge}
                      data-testid="gpu-acceleration-badge"
                      aria-hidden="true"
                      onMouseEnter={(event) => showGpuTooltip(node.id, event.currentTarget)}
                      onMouseLeave={() => setGpuTooltip(null)}
                    >
                      GPU
                    </span>
                    <span id={accelerationDescriptionId} className={styles.visuallyHidden}>
                      NVIDIA GPU accelerated. {acceleration.invocationCount} observed GPU{' '}
                      {acceleration.invocationCount === 1 ? 'call' : 'calls'}.{' '}
                      {acceleration.technologies
                        .map(
                          (technology) =>
                            `${technology.label}${technology.libraryVersion ? ` ${technology.libraryVersion}` : ''}: ${technology.description}`
                        )
                        .join(' ')}
                    </span>
                  </>
                )}
                <span id={stateDescriptionId} className={styles.visuallyHidden}>
                  {node.state}; {availability.replaceAll('-', ' ')}
                </span>
              </>
            )
            const nodeStyle = {
              left: node.x,
              top: node.y,
              width: node.width || EXECUTION_NODE_WIDTH,
              height: node.height || EXECUTION_NODE_HEIGHT,
            }
            const sharedProps = {
              className: styles.graphNode,
              style: nodeStyle,
              'data-execution-node': 'true',
              'data-node-id': node.id,
              'data-state': node.state,
              'data-current': node.current || undefined,
              'data-branch': node.branch,
              'data-node-kind': node.kind,
              'data-node-group': node.group,
              'data-selected': selectedNodeId === node.id || undefined,
              'data-availability': availability,
              'data-interactive': inspectableNodeId ? 'true' : 'false',
              'data-gpu-accelerated': acceleration ? 'true' : undefined,
              onFocus: acceleration
                ? (event: ReactFocusEvent<HTMLElement>) =>
                    showGpuTooltip(node.id, event.currentTarget)
                : undefined,
              onBlur: acceleration ? () => setGpuTooltip(null) : undefined,
              onKeyDown: acceleration
                ? (event: ReactKeyboardEvent<HTMLElement>) => {
                    if (event.key === 'Escape') setGpuTooltip(null)
                  }
                : undefined,
            } as const
            const describedBy = acceleration
              ? `${stateDescriptionId} ${accelerationDescriptionId}`
              : stateDescriptionId

            return inspectableNodeId ? (
              <button
                key={node.id}
                type="button"
                {...sharedProps}
                onClick={() => onNodeSelect?.(inspectableNodeId)}
                aria-label={`Inspect ${node.label}`}
                aria-describedby={describedBy}
              >
                {nodeContents}
              </button>
            ) : (
              <div
                key={node.id}
                {...sharedProps}
                tabIndex={acceleration ? 0 : undefined}
                aria-describedby={describedBy}
              >
                {nodeContents}
              </div>
            )
          })}
        </div>
      </div>
      {gpuTooltip && activeGpuAcceleration ? (
        <aside
          className={styles.gpuAccelerationTooltip}
          role="tooltip"
          data-testid="gpu-acceleration-tooltip"
          data-node-id={activeGpuAcceleration.nodeId}
          data-placement={gpuTooltip.placement}
          data-alignment={gpuTooltip.alignment}
          style={{ left: gpuTooltip.left, top: gpuTooltip.top }}
        >
          <header>
            <span aria-hidden="true">GPU</span>
            <div>
              <strong>NVIDIA GPU accelerated</strong>
              <small>
                {activeGpuAcceleration.invocationCount} observed GPU{' '}
                {activeGpuAcceleration.invocationCount === 1 ? 'call' : 'calls'}
              </small>
            </div>
          </header>
          <div className={styles.gpuAccelerationTechnologies}>
            {activeGpuAcceleration.technologies.map((technology) => (
              <section key={technology.id} data-technology-id={technology.id}>
                <div>
                  <strong>{technology.label}</strong>
                  {technology.libraryVersion ? <code>{technology.libraryVersion}</code> : null}
                </div>
                <p>{technology.description}</p>
              </section>
            ))}
          </div>
        </aside>
      ) : null}
    </div>
  )
}
