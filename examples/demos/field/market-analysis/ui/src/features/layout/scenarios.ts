// SPDX-FileCopyrightText: Copyright (c) 2025-2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Demo scenarios: the active data pack's examples, offered by the composer's
 * "Demo scenario" picker. Choosing one stages its question with exactly the
 * data sources it needs.
 */

import type { PackQuestion } from '@/adapters/api/pack-client'
import type { Pill } from '@/shared/components/ToolPills'

export interface DemoScenario {
  id: string
  label: string
  /** The tools it is expected to use, shown beside the label as pills */
  tools: Pill[]
  description: string
  question: string
  sourceIds: string[]
}

/** The most examples the picker offers (`MAX_EXAMPLES` in the API) */
export const MAX_EXAMPLES = 12

const toDemoScenario = (question: PackQuestion): DemoScenario => ({
  id: question.id,
  label: question.label,
  tools: [...question.tools],
  description: question.description ?? question.question,
  question: question.question,
  sourceIds: [...question.sources],
})

/**
 * The pack's examples, in their order: the questions `examples` names. Without the list (an API from
 * before it), the featured questions and then the others, as the API's default. At most 12.
 */
export const toDemoScenarios = (
  questions: readonly PackQuestion[],
  examples?: readonly string[] | null
): DemoScenario[] => {
  const byId = new Map(questions.map((question) => [question.id, question]))
  const ids =
    examples ??
    [
      ...questions.filter((question) => question.featured),
      ...questions.filter((question) => !question.featured),
    ].map((question) => question.id)
  return ids
    .flatMap((id) => {
      const question = byId.get(id)
      return question ? [toDemoScenario(question)] : []
    })
    .slice(0, MAX_EXAMPLES)
}

/** The scenarios whose data sources are all available. */
export const getAvailableDemoScenarios = (
  scenarios: readonly DemoScenario[],
  availableSourceIds: Iterable<string>
): DemoScenario[] => {
  const available = new Set(availableSourceIds)
  return scenarios.filter((scenario) =>
    scenario.sourceIds.every((sourceId) => available.has(sourceId))
  )
}

/** The scenario staged in the composer: its question with exactly its data sources. */
export const getActiveDemoScenario = (
  question: string,
  enabledSourceIds: readonly string[],
  scenarios: readonly DemoScenario[]
): DemoScenario | undefined => {
  const selected = new Set(enabledSourceIds)
  return scenarios.find(
    (scenario) =>
      scenario.question === question &&
      scenario.sourceIds.length === selected.size &&
      scenario.sourceIds.every((sourceId) => selected.has(sourceId))
  )
}
