// SPDX-FileCopyrightText: Copyright (c) 2025-2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * NoSourcesBanner Component
 *
 * Displays a warning banner when no data sources are enabled. This alerts
 * users that responses may be less accurate without external data sources.
 *
 * Dismissable by user. Dismiss state resets when a source is enabled again
 * so the banner can reappear if the user later removes all sources.
 */

'use client'

import { type FC, useState, useEffect, useRef } from 'react'
import { Banner } from '@/adapters/ui'
import { useLayoutStore } from '@/features/layout/store'

const WARNING_MESSAGE =
  'No data sources selected. Responses are more likely to be inaccurate or outdated unless external data sources are added.'

export const NoSourcesBanner: FC = () => {
  const [isDismissedByUser, setIsDismissedByUser] = useState(false)
  const shouldShow = useLayoutStore(
    (state) => state.availableDataSources !== null && state.enabledDataSourceIds.length === 0
  )
  const prevShouldShowRef = useRef(shouldShow)

  useEffect(() => {
    if (prevShouldShowRef.current && !shouldShow) {
      setIsDismissedByUser(false)
    }
    prevShouldShowRef.current = shouldShow
  }, [shouldShow])

  if (!shouldShow || isDismissedByUser) return null

  return (
    <div className="mx-auto w-full max-w-3xl px-4">
      <Banner status="warning" kind="inline" onClose={() => setIsDismissedByUser(true)}>
        {WARNING_MESSAGE}
      </Banner>
    </div>
  )
}
