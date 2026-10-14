// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/** Display formatting shared by the execution views. */

export const formatCount = (value: number | null): string =>
  value === null ? '–' : new Intl.NumberFormat('en-US').format(value)

/** "1 call", "2 calls" */
export const plural = (count: number, noun: string): string =>
  `${count} ${noun}${count === 1 ? '' : 's'}`

/** Milliseconds between two ISO timestamps. */
export const elapsedMs = (from: string, to: string): number => Date.parse(to) - Date.parse(from)
