// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/** Small technology pills (pills.ts): RAPIDS purple for the market tools' libraries, NVIDIA green for the rest. */

import { Fragment, type FC } from 'react'
import { Tooltip } from '@/adapters/ui'
import { cn } from '@/shared/lib/cn'
import { PILLS, orderPills, pillLabel, pillToolLabels, type ToolPillUse } from './pills'

export const ToolPills: FC<{ pills: readonly ToolPillUse[]; className?: string }> = ({
  pills,
  className,
}) => {
  if (!pills.length) return null
  return (
    <span className={cn('tool-pills', className)} data-testid="tool-pills">
      {orderPills(pills).map((use) => {
        const label = pillLabel(use)
        const tools = pillToolLabels(use)
        const pill = (
          <span className="tool-pill" data-family={PILLS[use.pill].family} data-pill={use.pill}>
            {label}
          </span>
        )
        const key = `${use.pill}:${use.device ?? ''}`
        return tools.length ? (
          <Tooltip key={key} side="top" openDelayDuration={200} slotContent={tools.join(', ')}>
            {pill}
          </Tooltip>
        ) : (
          <Fragment key={key}>{pill}</Fragment>
        )
      })}
    </span>
  )
}
