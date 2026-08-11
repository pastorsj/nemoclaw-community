<!--
  SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
  SPDX-License-Identifier: Apache-2.0
-->

# certs/

Optional drop point for additional CA certificates used by the Hermes image
build and trusted inside the final sandbox image. Files placed here are
installed before the pinned Hermes environment is built and retained so
`curl`, Python, native NeMo Relay, and other TLS clients inside the sandbox
trust the corresponding roots.

## When you need this

If the network running this example performs TLS interception (for example, a
proxy that re-signs HTTPS traffic with its own CA), agent calls to the public
internet will fail with errors like `SSL certificate problem: self-signed
certificate in certificate chain`. Place the interception CA(s) here to
register them as trusted roots.

If TLS traffic is not being intercepted, leave this directory empty. The
Dockerfile's `update-ca-certificates` step becomes a no-op and the build
succeeds as-is.

## Usage

1. From the recipe root, copy your CA certificate(s) into `certs/`.
   PEM-encoded certificates need a `.crt` extension because
   `update-ca-certificates` only picks up `*.crt`.

   ```bash
   cp /path/to/corp-proxy-ca.pem certs/corp-proxy-ca.crt
   ```

2. Rebuild the Hermes sandbox by re-running bring-up:

   ```bash
   bash scripts/bring-up.sh
   ```

The Dockerfile copies everything in this directory into
`/usr/local/share/ca-certificates/` and runs `update-ca-certificates` to
register the new trust roots.

Certificate files in this directory are ignored by the recipe's `.gitignore`.
Review each root before copying it; adding a CA expands which certificates the
image trusts.
