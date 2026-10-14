// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

export const ACTIVITY_PANEL_COLLAPSED_WIDTH = 40
export const ACTIVITY_PANEL_MIN_WIDTH = 400
export const ACTIVITY_PANEL_REMAINDER_MIN = 360
export const ACTIVITY_PANEL_KEYBOARD_STEP = 16
export const ACTIVITY_PANEL_DEFAULT_RATIO = 0.6
export const ACTIVITY_PANEL_DEFAULT_OFFSET = 40
export const ACTIVITY_PANEL_FALLBACK_CONTAINER_WIDTH = 1440

export type ActivityPanelWidthBounds = {
  min: number
  max: number
}

/** Keep both the activity pane and the remaining answer workspace usable. */
export const activityPanelWidthBounds = (containerWidth: number): ActivityPanelWidthBounds => {
  const safeContainerWidth = Number.isFinite(containerWidth)
    ? Math.max(0, Math.floor(containerWidth))
    : ACTIVITY_PANEL_FALLBACK_CONTAINER_WIDTH
  const max = Math.max(
    ACTIVITY_PANEL_COLLAPSED_WIDTH,
    safeContainerWidth - ACTIVITY_PANEL_REMAINDER_MIN
  )
  return { min: Math.min(ACTIVITY_PANEL_MIN_WIDTH, max), max }
}

export const clampActivityPanelWidth = (width: number, bounds: ActivityPanelWidthBounds): number =>
  Math.max(bounds.min, Math.min(bounds.max, Math.round(width)))

/** Preserve the existing 60% + rail presentation until the user resizes it. */
export const defaultActivityPanelWidth = (containerWidth: number): number => {
  const bounds = activityPanelWidthBounds(containerWidth)
  return clampActivityPanelWidth(
    containerWidth * ACTIVITY_PANEL_DEFAULT_RATIO + ACTIVITY_PANEL_DEFAULT_OFFSET,
    bounds
  )
}
