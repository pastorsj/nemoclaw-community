# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Run ``phoenix serve`` with idle HTTP connections kept open longer than its clients keep them.

NeMo Relay in the sandbox exports to Phoenix through OpenShell's L7 proxy and reuses the connection.
uvicorn closes an idle connection after 5 s; the proxy does not pass that close on, so the export
after an idle gap (the one after every long model call) fails, and Relay drops the batch without
retrying: those model calls and the job's turn span never reached Phoenix. Relay's HTTP client
closes idle connections after 90 s, so Phoenix waits longer than that. Phoenix has no setting for it.
"""

import runpy
import sys

import uvicorn

KEEP_ALIVE_SECONDS = 120

_init = uvicorn.Config.__init__


def _init_with_keep_alive(self, *args, **kwargs):
    kwargs.setdefault("timeout_keep_alive", KEEP_ALIVE_SECONDS)
    _init(self, *args, **kwargs)


uvicorn.Config.__init__ = _init_with_keep_alive
sys.argv = ["phoenix", "serve"]
runpy.run_module("phoenix.server.main", run_name="__main__", alter_sys=True)
