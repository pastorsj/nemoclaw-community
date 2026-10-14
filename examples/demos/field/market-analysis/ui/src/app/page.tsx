// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

import type { ReactNode } from 'react'
import { fetchPack } from '@/adapters/api/pack-client'
import { EcosystemLanding } from '@/features/landing'
import { readUiMode } from '@/shared/config/env'

/** Landing page; in live mode it shows the active data pack's featured questions. */
const HomePage = async (): Promise<ReactNode> => {
  const pack = readUiMode() === 'live' ? await fetchPack() : null
  return (
    <EcosystemLanding
      featuredQuestions={pack?.questions.filter((q) => q.featured) ?? []}
      disclaimer={pack?.disclaimer ?? null}
    />
  )
}

export default HomePage
