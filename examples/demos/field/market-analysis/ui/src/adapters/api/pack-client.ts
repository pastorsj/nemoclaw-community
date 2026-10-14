// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Data Pack Client (server-side)
 *
 * Reads the active data pack's public view (`GET /v1/pack`, the generated
 * `PackView` contract): its title, disclaimer, demo questions and the examples
 * of the composer's picker. Used by server components, so it calls the API
 * directly at `API_URL` instead of going through the browser proxy.
 */

import { z } from 'zod'
import { isPill } from '@/shared/components/ToolPills'
import { readApiUrl } from '@/shared/config/env'

const PackQuestionSchema = z.object({
  id: z.string(),
  label: z.string(),
  /** The tools it is expected to use, as pills (the composer's demo scenario list shows them) */
  tools: z
    .array(z.string())
    .nullish()
    .transform((tools) => (tools ?? []).filter(isPill)),
  description: z.string().nullish(),
  question: z.string(),
  sources: z.array(z.string()),
  featured: z.boolean().default(false),
})

/** `PackView` (`@/generated/pack`) as the UI reads it; its spec checks that it accepts every view. */
export const PackSchema = z.object({
  id: z.string(),
  title: z.string(),
  disclaimer: z.string().nullish(),
  questions: z.array(PackQuestionSchema),
  /** The ids of the questions the composer's example picker offers, in order (none from an older API) */
  examples: z.array(z.string()).nullish(),
})

export type PackQuestion = z.infer<typeof PackQuestionSchema>
export type Pack = z.infer<typeof PackSchema>

/** Fetch the active pack, or null when the API is unavailable or returns an unexpected shape. */
export const fetchPack = async (): Promise<Pack | null> => {
  try {
    const response = await fetch(`${readApiUrl()}/v1/pack`, {
      cache: 'no-store',
      signal: AbortSignal.timeout(3000),
    })
    if (!response.ok) return null
    const parsed = PackSchema.safeParse(await response.json())
    return parsed.success ? parsed.data : null
  } catch {
    return null
  }
}
