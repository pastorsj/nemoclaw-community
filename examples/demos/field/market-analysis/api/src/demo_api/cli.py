# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""``demo-api record``: ask the pack's questions on a running stack and write the replay bundle.

The v2 bundle, which the UI replays from ``data/packs/<pack>/recordings/``::

    index.json             {schemaVersion: 2, pack: {id, version}, recordedAt,
                            sessions: [{id, title, featured, turns: [{jobId, question}], tools}]}
                           tools: the pills of the tools its runs used (pills.py), for the replays list
    pack.json              a copy of /data/active/pack.json, so replay needs no API
    sessions/<id>.json     {schemaVersion: 2, id, title, turns: [<GET .../job/{id}/export>]}
    database.json          {schemaVersion: 1, sources: [{id, name, databaseName, schema, previews, queries}]}:
                           each structured source's schema (GET .../schema), the first rows of each table
                           (GET .../preview), and the result (POST .../query) of each SQL query the recorded
                           answers ran and of the data viewer's starting query for each table, so the data
                           viewer works in replay

Each pack question becomes one single-turn session, asked one at a time. Each pack conversation
(questions.yaml ``conversations``, recorded with ``--all`` or by id) becomes one session of several
turns, asked in order in one conversation, so a later turn sees the earlier answers. After an
answer that called market analytics tools, the recorder asks for its CPU/GPU comparison
(POST .../benchmark), which the export then carries; on a CPU-only stack there is none. A
question or conversation that does not succeed is left out of the bundle and makes the command
exit 1. Recording named ids (``--question``) updates those sessions of an existing bundle and
keeps its other sessions, naming them when they come from another build; recording a whole set replaces the
bundle. An id the pack does not offer, or a bundle of another version of the pack, stops the command before
anything is asked (exit 2).

Served model ids are written without a gateway's provider prefix. An OpenAI-compatible gateway may serve a model
under a provider-prefixed id (``<provider>/<publisher>/<model>``); the bundle keeps the last segment, the
model's own name, wherever the id appears.

``demo-api snapshot-database --out <recordings>`` rewrites only ``database.json`` of a bundle.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
import uuid
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from .pills import session_pills
from .registry import ToolRegistry
from .settings import Settings

POLL_SECONDS = 2.0
PREVIEW_ROWS = 8  # the rows the data viewer previews (ui/src/features/execution/data-viewer)
# The query the data viewer's SQL tab starts from for a table (DatabaseBrowser.tsx), so replay can run it too
DEFAULT_TABLE_SQL = 'SELECT * FROM "{schema}"."{table}" LIMIT 25'
BENCHMARK_TIMEOUT_SECONDS = 900.0
USAGE_ERROR = 2  # a request the pack or the bundle cannot take; nothing was asked
# A gateway's provider-prefixed model id: <provider>/<publisher>/<model>, e.g. vertex/google/example-model-1
PREFIXED_MODEL = re.compile(
    r"\b(?:nvidia|openai|azure|anthropic|aws|bedrock|gcp|google|vertex|meta)/(?:nvidia|openai|anthropic|google|meta)"
    r"/(?=[A-Za-z0-9])([A-Za-z0-9._:-]+)"
)


def public_model_ids(value: Any) -> Any:
    """``value`` with every provider-prefixed model id replaced by the model's own name (the last segment)."""
    if isinstance(value, str):
        return PREFIXED_MODEL.sub(r"\1", value)
    if isinstance(value, dict):
        return {key: public_model_ids(item) for key, item in value.items()}
    if isinstance(value, list):
        return [public_model_ids(item) for item in value]
    return value


def record(
    client: httpx.Client, *, data_dir: Path, out_dir: Path, question_ids: list[str], featured_only: bool, timeout: float
) -> int:
    pack = client.get("/v1/pack").raise_for_status().json()
    # Every session the pack can record, in pack order: (id, title, featured, sources, the questions of its turns)
    planned = [
        (question["id"], question["label"], question["featured"], question["sources"], [question["question"]])
        for question in pack["questions"]
    ] + [
        (conversation["id"], conversation["label"], False, conversation["sources"], conversation["turns"])
        for conversation in pack.get("conversations", [])
    ]
    order = [session_id for session_id, *_ in planned]
    if unknown := [question_id for question_id in question_ids if question_id not in order]:
        # /v1/pack leaves out a question whose sources this build lacks, so a pack's own id can be missing too
        print(
            f"Not in this build's pack (unknown, or its sources are not in this build): {', '.join(unknown)}. "
            f"It offers: {', '.join(order)}",
            file=sys.stderr,
        )
        return USAGE_ERROR
    wanted = [
        session
        for session in planned
        if (session[0] in question_ids if question_ids else session[2] or not featured_only)
    ]
    if not wanted:
        print("No pack questions match; nothing to record.", file=sys.stderr)
        return 1
    sessions: dict[str, dict[str, Any]] = {}
    index_path = out_dir / "index.json"
    if question_ids and index_path.is_file():
        previous = json.loads(index_path.read_text(encoding="utf-8"))
        named = {session[0] for session in wanted}
        kept = _kept_sessions(previous, pack, out_dir=out_dir, data_dir=data_dir, replaced=named)
        if kept is None:
            return USAGE_ERROR
        sessions = {session["id"]: session for session in kept if session["id"] in order}
    sessions_dir = out_dir / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    failures = 0
    for session_id, title, featured, sources, questions in wanted:
        conversation_id = f"record-{uuid.uuid4()}"
        turns: list[dict[str, Any]] = []
        for number, question in enumerate(questions, 1):
            step = f" (turn {number} of {len(questions)})" if len(questions) > 1 else ""
            print(f"Asking {session_id}{step}: {question}", file=sys.stderr)
            turn = _ask(client, question, sources, conversation_id=conversation_id, timeout=timeout)
            if turn["status"] != "success":
                print(f"  {turn['status']}; left out of the bundle", file=sys.stderr)
                failures += 1
                break
            turns.append(turn)
        else:
            session = {"schemaVersion": 2, "id": session_id, "title": title, "turns": turns}
            _write_json(sessions_dir / f"{session_id}.json", session)
            sessions[session_id] = {
                "id": session_id,
                "title": title,
                "featured": featured,
                "turns": [{"jobId": turn["jobId"], "question": turn["question"]} for turn in turns],
            }
    # The bundle holds only its sessions, so database.json covers only the queries they ran
    for path in sessions_dir.glob("*.json"):
        if path.stem not in sessions:
            path.unlink()
    shutil.copyfile(data_dir / "pack.json", out_dir / "pack.json")
    snapshot_database(client, out_dir)
    # The tools each session's runs used, as the replays list shows them (pills.py)
    registry = ToolRegistry.load(Settings().tool_registry_file)
    for session_id, session in sessions.items():
        turns = json.loads((sessions_dir / f"{session_id}.json").read_text(encoding="utf-8"))["turns"]
        session["tools"] = session_pills(turns, registry)
    index = {
        "schemaVersion": 2,
        "pack": {"id": pack["id"], "version": pack["version"]},
        "recordedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "sessions": sorted(sessions.values(), key=lambda session: order.index(session["id"])),
    }
    _write_json(index_path, index)
    recorded = len(wanted) - failures
    print(f"Recorded {recorded} of {len(wanted)} sessions; {out_dir} holds {len(sessions)}", file=sys.stderr)
    return 1 if failures else 0


def _kept_sessions(
    previous: dict[str, Any], pack: dict[str, Any], *, out_dir: Path, data_dir: Path, replaced: set[str]
) -> list[dict[str, Any]] | None:
    """The sessions of an existing bundle that recording named ids keeps; None when it must keep none of them.

    A bundle of another pack is replaced. One of another version of this pack is not updated piecemeal: its
    questions may have changed, and its index would claim the new version for every session. On another build of
    the same version (after a corpus change, say) the earlier sessions are kept and named, since their evidence and
    answers come from that build.
    """
    recorded = previous.get("pack", {})
    if recorded.get("id") != pack["id"]:
        return []
    if recorded.get("version") != pack["version"]:
        print(
            f"{out_dir} holds {pack['id']} {recorded.get('version')} and this stack serves {pack['version']}: "
            "record every session again (--all, without --question)",
            file=sys.stderr,
        )
        return None
    sessions = list(previous.get("sessions", []))
    earlier, current = _build(out_dir / "pack.json"), _build(data_dir / "pack.json")
    if earlier != current and (others := [session["id"] for session in sessions if session["id"] not in replaced]):
        print(
            f"Keeping {len(others)} sessions recorded on build {earlier or 'unknown'}, not on this build "
            f"({current or 'unknown'}): {', '.join(others)}",
            file=sys.stderr,
        )
    return sessions


def _build(pack_json: Path) -> str | None:
    """The build id of a pack.json: the active build's, or the copy a bundle keeps."""
    try:
        build = json.loads(pack_json.read_text(encoding="utf-8")).get("build")
    except (OSError, ValueError, AttributeError):
        return None
    return build if isinstance(build, str) else None


def _ask(
    client: httpx.Client, question: str, sources: list[str], *, conversation_id: str, timeout: float
) -> dict[str, Any]:
    job_id = str(uuid.uuid4())
    body = {"agent_type": "hermes", "input": question, "data_sources": sources, "job_id": job_id}
    headers = {"conversation-id": conversation_id}
    client.post("/v1/jobs/async/submit", json=body, headers=headers).raise_for_status()
    deadline = time.monotonic() + timeout
    while True:
        status = client.get(f"/v1/jobs/async/job/{job_id}").raise_for_status().json()
        if status["status"] not in {"submitted", "running"}:
            break
        if time.monotonic() > deadline:
            client.post(f"/v1/jobs/async/job/{job_id}/cancel")
            break
        time.sleep(POLL_SECONDS)
    if status.get("error"):
        print(f"  error: {status['error']}", file=sys.stderr)
    if status["status"] == "success":
        _benchmark(client, job_id)
    return public_model_ids(client.get(f"/v1/jobs/async/job/{job_id}/export").raise_for_status().json())


def _benchmark(client: httpx.Client, job_id: str) -> None:
    """Compare the answer's market calls on the CPU and the GPU; a run without any, or a CPU-only stack, has none."""
    response = client.post(f"/v1/jobs/async/job/{job_id}/benchmark", timeout=BENCHMARK_TIMEOUT_SECONDS)
    if response.status_code == 422:
        return
    body = response.json() if response.status_code == 200 else {}
    print(f"  benchmark: {body.get('status', response.status_code)}", file=sys.stderr)


def snapshot_database(client: httpx.Client, out_dir: Path) -> None:
    """Write ``database.json``: what the data viewer shows of each structured source, from the running API."""
    queries: set[tuple[str, str]] = set()
    for path in sorted((out_dir / "sessions").glob("*.json")):
        for turn in json.loads(path.read_text(encoding="utf-8"))["turns"]:
            for receipt in turn["receipts"]:
                content = receipt.get("content") or {}
                if receipt.get("artifactKind") == "structured_query" and content.get("sql"):
                    queries.add((content["databaseName"], content["sql"]))
    sources = []
    for source in client.get("/v1/data_sources").raise_for_status().json():
        if not source.get("database_name"):
            continue
        base = f"/v1/data_sources/{source['id']}"
        schema = client.get(f"{base}/schema").raise_for_status().json()
        previews = {
            table["name"]: client.get(f"{base}/preview", params={"table": table["name"], "limit": PREVIEW_ROWS})
            .raise_for_status()
            .json()
            for table in schema["tables"]
        }
        results = []
        defaults = [
            DEFAULT_TABLE_SQL.format(schema=table["schema"], table=table["name"].rsplit(".", 1)[-1])
            for table in schema["tables"]
        ]
        recorded = sorted(sql for database, sql in queries if database == source["database_name"])
        for sql in dict.fromkeys([*recorded, *defaults]):
            response = client.post(f"{base}/query", json={"sql": sql})
            if response.status_code == 200:
                results.append({"sql": sql, "result": response.json()})
            else:
                print(
                    f"  a recorded query did not run ({response.status_code}); replay cannot rerun it", file=sys.stderr
                )
        sources.append(
            {
                "id": source["id"],
                "name": source["name"],
                "databaseName": source["database_name"],
                "schema": schema,
                "previews": previews,
                "queries": results,
            }
        )
    _write_json(out_dir / "database.json", {"schemaVersion": 1, "sources": sources})


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="demo-api", description="Job API tools.")
    commands = parser.add_subparsers(dest="command", required=True)
    record_parser = commands.add_parser("record", help="record the pack's questions as a v2 replay bundle")
    record_parser.add_argument("--api-url", default="http://api:8000", help="the running API (default: on the stack)")
    record_parser.add_argument("--out", type=Path, required=True, help="the pack's recordings directory")
    record_parser.add_argument("--data-dir", type=Path, help="the active pack (default: DATA_ACTIVE_DIR)")
    record_parser.add_argument(
        "--question",
        action="append",
        default=[],
        help="a question or conversation id (repeatable); updates only those sessions of the bundle",
    )
    record_parser.add_argument(
        "--all", action="store_true", help="every question and conversation, not only the featured questions"
    )
    record_parser.add_argument("--timeout", type=float, default=1_500, help="seconds to wait for one answer")
    snapshot_parser = commands.add_parser("snapshot-database", help="rewrite a bundle's database.json")
    snapshot_parser.add_argument("--api-url", default="http://api:8000", help="the running API (default: on the stack)")
    snapshot_parser.add_argument("--out", type=Path, required=True, help="the pack's recordings directory")
    args = parser.parse_args(argv)

    with httpx.Client(base_url=args.api_url, timeout=30.0) as client:
        if args.command == "snapshot-database":
            snapshot_database(client, args.out)
            return 0
        return record(
            client,
            data_dir=args.data_dir or Settings().data_active_dir,
            out_dir=args.out,
            question_ids=args.question,
            featured_only=not args.all,
            timeout=args.timeout,
        )


if __name__ == "__main__":
    sys.exit(main())
