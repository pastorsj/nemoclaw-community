// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The height of a list that shows only its first rows, for the composer's example picker: it shows
 * five, and the others scroll underneath.
 */

/** The most rows the example picker shows at once */
export const VISIBLE_EXAMPLE_ROWS = 5

/**
 * The height, in pixels, at which `list` shows exactly its first `rows` options: from the top of the
 * first to the bottom of the last, plus the list's borders and top padding, so the next option
 * starts at its bottom edge. Measured, so it follows the rows' fonts and pills. Null when every
 * option fits, or when the list is not laid out.
 */
export const visibleRowsHeight = (list: HTMLElement, rows: number): number | null => {
  const options = list.querySelectorAll<HTMLElement>('[role="option"]')
  if (options.length <= rows) return null
  const height =
    options[rows - 1].getBoundingClientRect().bottom - options[0].getBoundingClientRect().top
  if (height <= 0) return null
  const style = getComputedStyle(list)
  const edges = [style.borderTopWidth, style.paddingTop, style.borderBottomWidth]
  return height + edges.reduce((sum, edge) => sum + (parseFloat(edge) || 0), 0)
}
