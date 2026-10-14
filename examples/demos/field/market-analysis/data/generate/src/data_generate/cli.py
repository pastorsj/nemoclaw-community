# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""demo-data-generate: write the synthetic-market pack's text with NeMo Data Designer and Nemotron.

    demo-data-generate [--profile standard] [--out DIR] [--sec-dir DIR] [--fresh]

Writes companies.jsonl, headlines.jsonl, stories.jsonl and checks.json to --out (default: the pack's text/).
Rows already in --out (or, for a new --out, in the pack's text/) are kept while they still pass the checks, so a
run extends the text to a larger profile, resumes an interrupted run, and regenerates only what fails.

Environment:
  DATA_DESIGNER_API_KEY   key for DATA_DESIGNER_BASE_URL (default: INFERENCE_API_KEY, only when
                          INFERENCE_BASE_URL is the same endpoint, or it is an nvapi- key and the
                          endpoint is build.nvidia.com)
  DATA_DESIGNER_BASE_URL  OpenAI-compatible endpoint (default: https://integrate.api.nvidia.com/v1)
  DATA_DESIGNER_MODEL     default nvidia/nemotron-3-super-120b-a12b, with thinking off
  DATA_DESIGNER_PARALLEL  concurrent requests (default 8)
  SEC_USER_AGENT          a name and an email, required by SEC for its ticker files
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
import sys
import time
from collections.abc import Mapping
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from data_generate import jobs
from data_generate import roster
from data_generate import sec

PACK_DIR = Path(__file__).resolve().parents[3] / "packs" / "synthetic-market"
TEXT_FILES = ("companies.jsonl", "headlines.jsonl", "stories.jsonl")
BANNED_NAME_WORDS = {"inc", "corp", "corporation", "ltd", "llc", "plc"}
Rows = list[dict[str, Any]]


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    pack = yaml.safe_load((args.pack_dir / "pack.yaml").read_text(encoding="utf-8"))
    model = yaml.safe_load((args.pack_dir / "generator" / "model.yaml").read_text(encoding="utf-8"))
    profiles = pack["generator"]["profiles"]
    profile = args.profile or pack["generator"]["default_profile"]
    if profile not in profiles:
        raise SystemExit(f"unknown profile {profile!r}; choose one of {sorted(profiles)}")
    os.environ["DATA_DESIGNER_API_KEY"] = designer_key(os.environ)

    out = args.out or args.pack_dir / "text"
    existing = {} if args.fresh else read_text(out) or read_text(args.pack_dir / "text")
    names = sec.load(os.environ.get("SEC_USER_AGENT"), args.sec_dir)
    designer = jobs.Designer(out / ".work")
    text, record = generate(designer, model, profiles[profile]["params"]["issuers"], names, existing)
    checks = {"profile": profile} | record | {"files": write_text(out, text)}
    (out / "checks.json").write_text(json.dumps(checks, indent=2) + "\n", encoding="utf-8")
    shutil.rmtree(out / ".work", ignore_errors=True)
    usage = checks["usage"]
    print(f"wrote {out}: {checks['counts']}")
    print(
        f"  {usage['calls']} model calls {usage['by_job']}, {usage['input_tokens']:,} input and "
        f"{usage['output_tokens']:,} output tokens, {checks['seconds']} s"
    )
    return 0


def designer_key(environment: Mapping[str, str]) -> str:
    """DATA_DESIGNER_API_KEY, or the inference key where it belongs: never sent to another host.

    INFERENCE_API_KEY stands in only when DATA_DESIGNER_BASE_URL (default build.nvidia.com) is INFERENCE_BASE_URL,
    or when it is an nvapi- key, which build.nvidia.com issued, and the endpoint is build.nvidia.com.
    """
    if key := environment.get("DATA_DESIGNER_API_KEY"):
        return key
    endpoint = (environment.get("DATA_DESIGNER_BASE_URL") or jobs.DEFAULT_BASE_URL).rstrip("/")
    inference = environment.get("INFERENCE_API_KEY", "")
    same_endpoint = endpoint == (environment.get("INFERENCE_BASE_URL") or "").rstrip("/")
    issued_there = inference.startswith("nvapi-") and endpoint == jobs.DEFAULT_BASE_URL
    if inference and (same_endpoint or issued_there):
        return inference
    raise SystemExit(
        f"set DATA_DESIGNER_API_KEY to the key for {endpoint}: INFERENCE_API_KEY is used only when "
        "INFERENCE_BASE_URL is that endpoint, or for an nvapi- key on build.nvidia.com"
    )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="demo-data-generate", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    root.add_argument("--profile", help="the pack profile whose issuer count to cover (default: the pack's)")
    root.add_argument("--pack-dir", type=Path, default=PACK_DIR)
    root.add_argument("--out", type=Path, help="where to write the text (default: the pack's text/)")
    root.add_argument("--sec-dir", type=Path, help="read SEC's ticker files from here instead of sec.gov")
    root.add_argument("--fresh", action="store_true", help="ignore existing text and generate all of it")
    return root


def read_text(directory: Path) -> dict[str, Rows]:
    if not all((directory / name).is_file() for name in TEXT_FILES):
        return {}
    return {
        name: [json.loads(line) for line in (directory / name).read_text(encoding="utf-8").splitlines() if line]
        for name in TEXT_FILES
    }


def write_text(out: Path, text: dict[str, Rows]) -> dict[str, str]:
    """One JSON object per line, so a review sees each generated row; returns each file's SHA-256."""
    out.mkdir(parents=True, exist_ok=True)
    digests = {}
    for name, rows in text.items():
        body = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
        (out / name).write_text(body, encoding="utf-8")
        digests[name] = hashlib.sha256(body.encode()).hexdigest()
    return digests


def generate(
    designer: Any, model: dict[str, Any], count: int, names: sec.SecNames, existing: dict[str, Rows]
) -> tuple[dict[str, Rows], dict[str, Any]]:
    """Run the three jobs and check the result; returns the text files' rows and the record for checks.json."""
    if count < len(model["story"]):
        raise SystemExit(f"a profile needs at least the {len(model['story'])} story issuers")
    started = time.monotonic()
    companies, company_rounds = generate_companies(designer, model, count, names, existing.get("companies.jsonl", []))
    headlines, headline_rounds = generate_headlines(designer, model, existing.get("headlines.jsonl", []))
    stories, story_rounds = generate_stories(designer, model, companies, existing.get("stories.jsonl", []))
    check_unique(companies)
    text = dict(zip(TEXT_FILES, (companies, headlines, stories), strict=True))
    return text, {
        "issuers": count,
        "seed": model["seed"],
        "model": designer.model,
        "data_designer": importlib.metadata.version("data-designer"),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "seconds": round(time.monotonic() - started, 1),
        "sec": names.record,
        "counts": {"companies": len(companies), "headline_templates": len(headlines), "stories": len(stories)},
        "rounds": {"companies": company_rounds, "headlines": headline_rounds, "stories": story_rounds},
        "usage": designer.usage.as_dict(),
    }


def generate_companies(
    designer: Any, model: dict[str, Any], count: int, names: sec.SecNames, existing: Rows
) -> tuple[Rows, Rows]:
    """One company per slot. A name that matches an SEC title costs its slot the root; the slot then retries."""
    text = {row["slot"]: row for row in existing}
    refused: dict[int, set[str]] = {}
    rounds: Rows = []
    for attempt in range(roster.MAX_ROUNDS):
        slots = roster.build(model, count, names.root_taken, names.ticker_taken, refused)
        pending = [slot for slot in slots if not company_kept(text.get(slot.slot), slot, names)]
        if not pending:
            return [text[slot.slot] for slot in slots], rounds
        began = time.monotonic()
        seed = pd.DataFrame([slot.row() for slot in pending])
        result = designer.run("companies", attempt, seed, jobs.Company, jobs.COMPANY_PROMPT, max_tokens=300)
        by_slot = {slot.slot: slot for slot in pending}
        problems: dict[str, int] = {}
        for record in result.to_dict("records"):
            slot, answer = by_slot[record["slot"]], record["answer"]
            problem = company_problem(answer["company_name"], slot.name_root, names)
            if problem:
                problems[problem] = problems.get(problem, 0) + 1
                if problem == "sec-name":
                    refused.setdefault(slot.slot, set()).add(slot.name_root)
                continue
            text[slot.slot] = slot.row() | {k: answer[k].strip() for k in ("company_name", "profile")}
        rounds.append(round_record(len(pending), len(result), problems, began))
    raise SystemExit(f"companies: slots still failing after {roster.MAX_ROUNDS} rounds: {rounds[-1]}")


def company_kept(row: dict[str, Any] | None, slot: roster.Slot, names: sec.SecNames) -> bool:
    """An existing row is kept when its slot is unchanged and its name still passes the checks."""
    if row is None or any(row.get(key) != value for key, value in slot.row().items()):
        return False
    return company_problem(row["company_name"], slot.name_root, names) is None


def company_problem(name: str, root: str, names: sec.SecNames) -> str | None:
    """Why a generated company name is refused, or None."""
    words = sec.words(name)
    if not words or words[0] != root.casefold() or len(words) > 4:
        return "format"
    if BANNED_NAME_WORDS & set(words):
        return "legal-suffix"
    if names.name_taken(name):
        return "sec-name"
    return None


def generate_headlines(designer: Any, model: dict[str, Any], existing: Rows) -> tuple[Rows, Rows]:
    """`headline_variants` templates for every event type and sentiment."""
    variants = model["headline_variants"]
    keys = [(event, sentiment) for event in model["event_types"] for sentiment in model["sentiments"]]
    sets: dict[tuple[str, str], list[str]] = {}
    for row in existing:
        sets.setdefault((row["event_type"], row["sentiment_label"]), []).append(row["template"])
    sets = {key: templates for key, templates in sets.items() if key in keys and len(templates) == variants}
    rounds: Rows = []
    for attempt in range(roster.MAX_ROUNDS):
        pending = [key for key in keys if key not in sets]
        if not pending:
            rows = [
                {"event_type": event, "sentiment_label": sentiment, "variant": index, "template": template}
                for event, sentiment in keys
                for index, template in enumerate(sets[(event, sentiment)])
            ]
            return rows, rounds
        began = time.monotonic()
        seed = pd.DataFrame(
            [
                {
                    "event_type": event,
                    "about": model["event_types"][event]["about"],
                    "sentiment": sentiment,
                    "tone": jobs.TONES[sentiment],
                }
                for event, sentiment in pending
            ]
        ).assign(count=variants)
        output = jobs.headline_set(variants)
        result = designer.run("headlines", attempt, seed, output, jobs.HEADLINE_PROMPT, max_tokens=1500)
        refused = 0
        for record in result.to_dict("records"):
            templates = [template.strip() for template in record["answer"]["templates"]]
            if len(set(templates)) == variants and all(map(template_ok, templates)):
                sets[(record["event_type"], record["sentiment"])] = templates
            else:
                refused += 1
        rounds.append(round_record(len(pending), len(result), {"template": refused} if refused else {}, began))
    raise SystemExit(f"headlines: sets still failing after {roster.MAX_ROUNDS} rounds: {rounds[-1]}")


def template_ok(template: str) -> bool:
    """Exactly one {company} placeholder, no other braces, and at most 110 characters."""
    rest = template.replace("{company}", "")
    return template.count("{company}") == 1 and "{" not in rest and "}" not in rest and len(template) <= 110


def generate_stories(designer: Any, model: dict[str, Any], companies: Rows, existing: Rows) -> tuple[Rows, Rows]:
    """A headline and a summary for each planted story event, about its story issuer."""
    events = [
        {
            "slot": slot,
            "ticker": companies[slot]["ticker"],
            "name_root": companies[slot]["name_root"],
            "company_name": companies[slot]["company_name"],
            "profile": companies[slot]["profile"],
            "date": event["date"],
            "event_type": event["event_type"],
            "about": model["event_types"][event["event_type"]]["about"],
            "sentiment": event["sentiment"],
            "tone": jobs.TONES[event["sentiment"]],
        }
        for slot, event in enumerate(model["story"])
    ]
    identity = ("slot", "ticker", "company_name", "date", "event_type")

    def row(event: dict[str, Any], answer: dict[str, str]) -> dict[str, Any]:
        return {key: event[key] for key in identity} | {
            "sentiment_label": event["sentiment"],
            "headline": answer["headline"].strip(),
            "summary": answer["summary"].strip(),
        }

    done = {
        old["slot"]: old
        for old in existing
        if old["slot"] < len(events) and old == row(events[old["slot"]], old)  # unchanged event and issuer
    }
    rounds: Rows = []
    for attempt in range(roster.MAX_ROUNDS):
        pending = [event for event in events if event["slot"] not in done]
        if not pending:
            return [done[event["slot"]] for event in events], rounds
        began = time.monotonic()
        result = designer.run("stories", attempt, pd.DataFrame(pending), jobs.Story, jobs.STORY_PROMPT, max_tokens=600)
        refused = 0
        for record in result.to_dict("records"):
            if record["name_root"].casefold() in sec.words(record["answer"]["headline"]):
                done[record["slot"]] = row(events[record["slot"]], record["answer"])
            else:
                refused += 1
        rounds.append(round_record(len(pending), len(result), {"headline": refused} if refused else {}, began))
    raise SystemExit(f"stories: events still failing after {roster.MAX_ROUNDS} rounds: {rounds[-1]}")


def round_record(requested: int, answered: int, refused: dict[str, int], began: float) -> dict[str, Any]:
    seconds = round(time.monotonic() - began, 1)
    return {"requested": requested, "unanswered": requested - answered, "refused": refused, "seconds": seconds}


def check_unique(companies: Rows) -> None:
    for key, normalize in (("ticker", str), ("name_root", str.casefold), ("company_name", sec.normalized_name)):
        values = [normalize(row[key]) for row in companies]
        if len(set(values)) != len(values):
            raise SystemExit(f"companies: {key} is not unique")


if __name__ == "__main__":
    sys.exit(main())
