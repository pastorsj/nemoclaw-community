// SPDX-FileCopyrightText: Copyright (c) 2025-2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * DeepResearchBanner Component
 *
 * Status banner for a job in the chat: shown while it runs, and after it
 * ends without an answer. A successful job shows its answer instead.
 */

'use client'

import { type FC } from 'react'
import { Banner, Flex, Text } from '@/adapters/ui'
import { formatTime } from '@/shared/utils/format-time'
import type { DeepResearchBannerType } from '../types'

export interface DeepResearchBannerProps {
  bannerType: DeepResearchBannerType
  jobId: string
  timestamp?: Date | string
}

type BannerStatus = 'info' | 'warning' | 'error'

const BANNERS: Record<
  DeepResearchBannerType,
  { heading: string; subheading: string; status: BannerStatus }
> = {
  starting: {
    heading: 'Run started',
    subheading: 'Open Thinking to follow live agent activity.',
    status: 'info',
  },
  failure: {
    heading: 'Run failed',
    subheading: 'The agent stopped before completing the answer.',
    status: 'error',
  },
  cancelled: {
    heading: 'Run stopped',
    subheading: 'The run was stopped by the user.',
    status: 'warning',
  },
  expired: {
    heading: 'Run unavailable',
    subheading: 'The stored result is no longer available.',
    status: 'warning',
  },
}

export const DeepResearchBanner: FC<DeepResearchBannerProps> = ({
  bannerType,
  jobId,
  timestamp,
}) => {
  const { heading, subheading, status } = BANNERS[bannerType]

  return (
    <Flex direction="col" gap="1" className="w-full">
      <Banner slotSubheading={`${subheading} Run ID: ${jobId}`} kind="header" status={status}>
        {heading}
      </Banner>
      {timestamp ? (
        <Text kind="body/regular/xs" className="text-subtle mr-3 self-end">
          {formatTime(timestamp)}
        </Text>
      ) : null}
    </Flex>
  )
}
