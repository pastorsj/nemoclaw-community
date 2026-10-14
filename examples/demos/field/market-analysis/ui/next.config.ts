// SPDX-FileCopyrightText: Copyright (c) 2025-2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

import type { NextConfig } from 'next'

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // A self-contained server (.next/standalone/server.js) for the container image.
  output: 'standalone',
  // This directory is the project root, whatever lockfiles exist above it.
  outputFileTracingRoot: process.cwd(),
  turbopack: {
    root: process.cwd(),
  },
}

export default nextConfig
