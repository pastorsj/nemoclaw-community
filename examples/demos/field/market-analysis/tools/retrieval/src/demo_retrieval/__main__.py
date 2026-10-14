# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""demo-retrieval serve | ingest | benchmark"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from . import benchmark
from . import ingest
from . import server
from .settings import Settings


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="demo-retrieval", description=__doc__)
    parser.add_argument(
        "command",
        choices=["serve", "ingest", "benchmark"],
        help="serve retrieve_evidence, build the index, or compare it with a GPU index (analytics-gpu)",
    )
    parser.add_argument("--data-dir", type=Path, default=Path("/data/active"), help="the active data pack")
    parser.add_argument(
        "--guard",
        action="store_true",
        help="benchmark: measure the build once more for the GPU guard into retrieval-benchmark-guard.json, never "
        "the file the API serves, and exit 1 if that fails",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    try:
        settings = Settings.from_env()
    except ValueError as error:
        parser.error(str(error))
    if args.command == "ingest":
        ingest.run(settings, args.data_dir)
    elif args.command == "benchmark":
        _benchmark(settings, args.data_dir, guard=args.guard)
    else:
        _export_traces()
        server.serve(settings, args.data_dir)


def _benchmark(settings: Settings, data_dir: Path, *, guard: bool) -> None:
    """The one-shot of `up` never fails the stack: the comparison is optional. ``guard`` (a GPU guard run by
    hand) fails when nothing was measured, so the guard never reads an earlier measurement as new."""
    log = logging.getLogger(__name__)
    try:
        measured = benchmark.run(settings, data_dir, guard=guard)
    except Exception:
        missing = "the GPU guard has no new measurement" if guard else "the Benchmark tab will lack it"
        log.exception("the CPU/GPU index comparison failed; %s", missing)
        measured = None
    if guard and measured is None:
        sys.exit(1)


def _export_traces() -> None:
    """Send the embed/search/rerank spans to OTEL_EXPORTER_OTLP_TRACES_ENDPOINT (Phoenix), when it is set."""
    if not os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"):
        return
    provider = TracerProvider(resource=Resource.create({"service.name": "retrieval"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)


if __name__ == "__main__":
    main()
