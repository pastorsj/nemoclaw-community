// SPDX-FileCopyrightText: Copyright (c) 2025-2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Root Layout
 *
 * The root layout for the entire application.
 * Sets up the HTML structure, global styles, and providers.
 *
 * Server-side environment variables are read here and passed to client
 * providers, enabling runtime configuration without rebuilding.
 */

import { type ReactNode } from 'react'
import { type Metadata } from 'next'
import { connection } from 'next/server'
import { readAppConfig } from '@/shared/config/env'
import { Providers } from './providers'
import './globals.css'

export const metadata: Metadata = {
  title: 'Enterprise Research',
  description: 'AI-powered research assistant',
  icons: {
    icon: '/favicon.ico',
  },
}

interface RootLayoutProps {
  children: ReactNode
}

const RootLayout = async ({ children }: RootLayoutProps): Promise<ReactNode> => {
  // Read configuration per request, not at build time.
  await connection()
  const config = readAppConfig()

  return (
    <html lang="en" id="style-root" suppressHydrationWarning>
      <head>
        {/* CDN SVG icon loader - inlines <svg data-src="..."> elements */}
        <script src="https://unpkg.com/external-svg-loader@1.6.8/svg-loader.min.js" async />
      </head>
      <body className="bg-surface-base">
        <Providers config={config}>{children}</Providers>
      </body>
    </html>
  )
}

export default RootLayout
