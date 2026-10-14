// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

import type { ReactElement, SelectHTMLAttributes } from 'react'

export function SelectShell({
  children,
  className = '',
}: {
  children: ReactElement<SelectHTMLAttributes<HTMLSelectElement>>
  className?: string
}) {
  return (
    <span className={`ui-select-shell ${className}`.trim()}>
      {children}
      <span className="ui-select-chevron" aria-hidden="true" />
    </span>
  )
}
