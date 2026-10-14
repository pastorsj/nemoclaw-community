# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Process entry points: the server (`market-analytics-server`) and the analytics worker it spawns.

RAPIDS' zero-code-change accelerators only take effect if they are installed before pandas, scikit-learn or
NetworkX is imported. So this module imports only the standard library, and the worker installs them before
anything else. A spawned process re-imports this entry module and unpickles `worker_main` by reference, so
neither step imports pandas early. Only the worker uses the GPU; the server process stays on the CPU.
"""

import os
from multiprocessing.connection import Connection
from pathlib import Path


def main() -> None:
    from .server import main as serve

    serve()


def worker_main(connection: Connection, root: Path, engine: str | None = None) -> None:
    """The worker process. `engine` overrides MARKET_ANALYTICS_ENGINE, e.g. a CPU worker next to the GPU one."""
    if engine is not None:
        os.environ["MARKET_ANALYTICS_ENGINE"] = engine  # tools.engine() reads it too
    if os.environ.get("MARKET_ANALYTICS_ENGINE", "cpu") == "gpu":
        install_gpu_accelerators()

    from .worker import serve

    serve(connection, root)


def install_gpu_accelerators() -> None:
    """cudf.pandas, cuml.accel and nx-cugraph. Call it before pandas, scikit-learn or NetworkX is imported."""
    os.environ.setdefault("NX_CUGRAPH_AUTOCONFIG", "True")  # read when NetworkX is imported

    # nx-cugraph's libcugraph links libcuvs, which needs NVRTC, and the wheels do not load it: until a cudf
    # kernel happens to be compiled first, `import nx_cugraph` fails ("libcugraph.so: cannot open shared
    # object file"). Load it up front, from the CUDA wheels RAPIDS installs.
    from cuda.pathfinder import load_nvidia_dynamic_lib

    load_nvidia_dynamic_lib("nvrtc")

    import cudf.pandas

    cudf.pandas.install()

    import cuml.accel

    cuml.accel.install()
