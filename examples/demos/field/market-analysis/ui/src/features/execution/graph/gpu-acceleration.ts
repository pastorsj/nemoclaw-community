// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The GPU badges on market analytics nodes. A node earns one only from a
 * receipt that says the call computed on the GPU with a reviewed NVIDIA
 * library; a GPU-capable tool is not enough.
 */

import type { ReceiptV2 } from '../contract'
import { toolFor } from '../registry'
import { TOOL_NODE_BY_NAME, type ExecutionNodeId } from './graph-model'

export interface GpuAccelerationTechnology {
  id: string
  label: string
  description: string
  library: string
  libraryVersion: string | null
  invocationCount: number
}

export interface ExecutionNodeGpuAcceleration {
  nodeId: ExecutionNodeId
  invocationCount: number
  technologies: GpuAccelerationTechnology[]
}

type Technology = Pick<GpuAccelerationTechnology, 'id' | 'label' | 'description'>

const CUDF: Technology = {
  id: 'cudf',
  label: 'cuDF',
  description:
    'Accelerates DataFrame filtering, joins, aggregation, and tabular analytics on NVIDIA GPUs.',
}
const CUGRAPH: Technology = {
  id: 'cugraph',
  label: 'cuGraph',
  description: 'Accelerates graph construction and algorithms such as PageRank on NVIDIA GPUs.',
}
const CUML: Technology = {
  id: 'cuml',
  label: 'cuML',
  description: 'Accelerates machine-learning preprocessing and algorithms on NVIDIA GPUs.',
}

/** By the library a receipt names (`cudf.pandas`, `cuml.accel`, …), lowercased. */
const TECHNOLOGY_BY_LIBRARY: Readonly<Record<string, Technology>> = {
  cudf: CUDF,
  'cudf.pandas': CUDF,
  'cudf-polars': CUDF,
  cugraph: CUGRAPH,
  'nx-cugraph': CUGRAPH,
  cuml: CUML,
  'cuml.accel': CUML,
}

/** The reviewed NVIDIA technology a completed GPU receipt computed with, else null. */
export const gpuAccelerationForReceipt = (receipt: ReceiptV2): GpuAccelerationTechnology | null => {
  if (receipt.artifactKind !== 'analytics_result' || receipt.status !== 'completed') return null
  const result = receipt.content
  if (!result || result.status === 'failed' || result.engine?.device !== 'gpu') return null
  const technology = TECHNOLOGY_BY_LIBRARY[result.engine.library.toLowerCase()]
  if (!technology) return null
  return {
    ...technology,
    library: result.engine.library,
    libraryVersion: result.engine.version || null,
    invocationCount: 1,
  }
}

/** Node badges from the receipts of the calls shown, one count per call. */
export const buildGpuAccelerationByNode = (
  receipts: readonly ReceiptV2[]
): ReadonlyMap<ExecutionNodeId, ExecutionNodeGpuAcceleration> => {
  const result = new Map<ExecutionNodeId, ExecutionNodeGpuAcceleration>()
  const counted = new Set<string>()

  for (const receipt of receipts) {
    const tool = toolFor(receipt.toolName)
    const nodeId = tool ? TOOL_NODE_BY_NAME[tool.id] : undefined
    const technology = gpuAccelerationForReceipt(receipt)
    if (!nodeId || !technology || counted.has(receipt.invocationId)) continue
    counted.add(receipt.invocationId)

    const current = result.get(nodeId) ?? { nodeId, invocationCount: 0, technologies: [] }
    const existing = current.technologies.find((item) => item.id === technology.id)
    if (existing) {
      existing.invocationCount += 1
      existing.libraryVersion ??= technology.libraryVersion
    } else {
      current.technologies.push(technology)
    }
    current.invocationCount += 1
    result.set(nodeId, current)
  }

  result.forEach((acceleration) => {
    acceleration.technologies.sort((left, right) => left.label.localeCompare(right.label))
  })
  return result
}
