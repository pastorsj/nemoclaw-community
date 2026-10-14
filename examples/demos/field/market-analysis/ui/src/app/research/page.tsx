// SPDX-FileCopyrightText: Copyright (c) 2025-2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Research Page
 *
 * The chat experience. In live mode the active data pack's examples are the
 * composer's demo scenarios, and `?question=<id>` (from the landing page)
 * places that question, any of the pack's, in the composer.
 */

import { type ReactNode, Suspense } from 'react'
import { fetchPack } from '@/adapters/api/pack-client'
import { MainLayout, toDemoScenarios, type InitialQuestion } from '@/features/layout'
import { readUiMode } from '@/shared/config/env'

interface ResearchPageProps {
  searchParams: Promise<Record<string, string | string[] | undefined>>
}

const ResearchPage = async ({ searchParams }: ResearchPageProps): Promise<ReactNode> => {
  const pack = readUiMode() === 'live' ? await fetchPack() : null
  const questionId = (await searchParams).question
  const question =
    typeof questionId === 'string' ? pack?.questions.find((q) => q.id === questionId) : undefined
  const initialQuestion: InitialQuestion | null = question
    ? { question: question.question, sourceIds: question.sources }
    : null

  // MainLayout reads the ?session= parameter, which needs a Suspense boundary.
  return (
    <Suspense fallback={null}>
      <MainLayout
        initialQuestion={initialQuestion}
        demoScenarios={toDemoScenarios(pack?.questions ?? [], pack?.examples)}
      />
    </Suspense>
  )
}

export default ResearchPage
