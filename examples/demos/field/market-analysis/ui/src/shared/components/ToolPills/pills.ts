// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Technology pills: which tools a demo question is expected to use, or a
 * recorded run actually used. The vocabulary is the tool registry's `Pill`
 * (contracts/tool-registry.schema.json). The market tools' libraries are RAPIDS
 * on the GPU; a run on the CPU is labelled with the CPU library it used, in the
 * same color. Hermes's own tools never get a pill.
 */

import { TOOL_REGISTRY, type Pill } from '@/generated/tool-registry'

export type { Pill }

/** One pill: its kind, the engine that ran it (market tools only), and the tools that brought it. */
export interface ToolPillUse {
  pill: Pill
  device?: 'gpu' | 'cpu' | null
  tools?: string[]
}

/** Display order */
export const PILL_ORDER: readonly Pill[] = [
  'cudf',
  'cuml',
  'cugraph',
  'kumo',
  'retrieval',
  'ontology',
]

export const PILLS: Readonly<
  Record<Pill, { label: string; cpuLabel?: string; family: 'rapids' | 'nvidia' }>
> = {
  cudf: { label: 'cuDF', cpuLabel: 'pandas', family: 'rapids' },
  cuml: { label: 'cuML', cpuLabel: 'scikit-learn', family: 'rapids' },
  cugraph: { label: 'cuGraph', cpuLabel: 'NetworkX', family: 'rapids' },
  kumo: { label: 'Kumo', family: 'nvidia' },
  retrieval: { label: 'Retrieval', family: 'nvidia' },
  ontology: { label: 'Ontology', family: 'nvidia' },
}

const TOOLS = new Map(TOOL_REGISTRY.tools.map((tool) => [tool.id, tool]))

export const isPill = (value: unknown): value is Pill =>
  typeof value === 'string' && (PILL_ORDER as readonly string[]).includes(value)

/** A pill's label: the CPU library when the run reported the CPU, else the GPU library or tool name. */
export const pillLabel = ({ pill, device }: ToolPillUse): string =>
  device === 'cpu' ? (PILLS[pill].cpuLabel ?? PILLS[pill].label) : PILLS[pill].label

/** The display names of the tools behind a pill, for its tooltip. */
export const pillToolLabels = ({ tools = [] }: ToolPillUse): string[] =>
  tools.map((id) => TOOLS.get(id)?.label ?? id)

/**
 * The pills as words, for assistive technology: "cuDF (Market Scan), Retrieval (Unstructured Retrieval)".
 * A pill's tooltip opens on hover only, and a recorded session's button names the session alone.
 */
export const describePills = (pills: readonly ToolPillUse[]): string =>
  orderPills(pills)
    .map((use) => {
      const tools = pillToolLabels(use)
      return tools.length ? `${pillLabel(use)} (${tools.join(', ')})` : pillLabel(use)
    })
    .join(', ')

/** Pills in display order: by kind, a GPU run before a CPU run of the same kind. */
export const orderPills = (pills: readonly ToolPillUse[]): ToolPillUse[] =>
  [...pills].sort(
    (a, b) =>
      PILL_ORDER.indexOf(a.pill) - PILL_ORDER.indexOf(b.pill) ||
      Number(a.device === 'cpu') - Number(b.device === 'cpu')
  )

/**
 * The pills of a recorded session's turns, as `demo-api record` computes them
 * (api/src/demo_api/pills.py): each completed registered tool call (an
 * `artifact.available` event) brings its tool's pills, with the engine its
 * receipt reports for the market tools. For bundles recorded before the index
 * carried them.
 */
export const sessionPills = (
  turns: ReadonlyArray<{ events?: unknown[]; receipts?: unknown[] }>
): ToolPillUse[] => {
  const found = new Map<string, ToolPillUse & { tools: string[] }>()
  for (const turn of turns) {
    const receipts = new Map(
      (turn.receipts ?? []).flatMap((receipt) =>
        isObject(receipt) && typeof receipt.receiptId === 'string'
          ? [[receipt.receiptId, receipt] as const]
          : []
      )
    )
    for (const event of turn.events ?? []) {
      if (!isObject(event) || event.eventKind !== 'artifact.available') continue
      const tool = TOOLS.get(String(event.toolName))
      if (!tool) continue
      const refs = Array.isArray(event.artifactRefs) ? event.artifactRefs : []
      const device = refs
        .map((ref) => engineDevice(receipts.get(String(ref))))
        .find((value) => value !== null)
      for (const pill of tool.pills) {
        const runDevice = PILLS[pill].family === 'rapids' ? (device ?? null) : null
        const key = `${pill}:${runDevice ?? ''}`
        const use = found.get(key) ?? { pill, device: runDevice, tools: [] }
        if (!use.tools.includes(tool.id)) use.tools.push(tool.id)
        found.set(key, use)
      }
    }
  }
  return orderPills([...found.values()])
}

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value)

const engineDevice = (receipt: unknown): 'gpu' | 'cpu' | null => {
  const content = isObject(receipt) && isObject(receipt.content) ? receipt.content : null
  const device = content && isObject(content.engine) ? content.engine.device : null
  return device === 'gpu' || device === 'cpu' ? device : null
}
