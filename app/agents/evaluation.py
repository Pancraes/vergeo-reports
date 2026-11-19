"""Blind evaluation of the triage agent against hand-labelled historical rides.

    python -m app.agents.evaluation --cases local_data/eval/cases.json [--offline] [--only ID]

Each case names a report file, the ride's window, the rider's notes as they were given, and the
expected answer taken from the earlier manual analysis (FIELD_CORPUS / incident notes). The
labels are never produced by this agent, and while a case runs the agent's corpus tool only shows
sections dated before that ride (CorpusIndex.visible_before), so it cannot read its own answer.

Scored per case, and kept even when they fail:

- verdict          the agent's verdict is one of the labelled acceptable verdicts
- trigger_path     matches the label
- direction        early / late / on_time / no_ring agrees
- blackout         the agent reports a blackout >= 10 min exactly when the label says so
- failure_class    defect vs known limit vs none agrees (any labelled acceptable value)
- doors_error_s    |agent doors time - labelled doors time| where the rider lapped the doors
- early_late_error_s  |agent early_late_seconds - labelled value| where known

Runtime, tool calls, tokens and cost come from RunMetrics, measured per run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
from datetime import date, datetime
from pathlib import Path

from pydantic import BaseModel

from . import extract as x
from .schemas import IncidentAnalysis, TriageResult, Verdict

BLACKOUT_S = 600
ON_TIME_S = 120


class Expected(BaseModel):
    verdicts: list[str]
    trigger_path: str
    direction: str  # early | late | on_time | no_ring
    blackout: bool | None = None  # None: not labelled, not scored
    failure_class: list[str]
    doors_phone: str | None = None
    doors_uncertainty_s: float | None = None
    early_late_s: float | None = None


class Case(BaseModel):
    id: str
    report: str
    ride_date: str
    focus: str | None = None
    notes: str | None = None
    note_written_at: str | None = None
    expected: Expected
    label_source: str
    # The ride window. "from"/"to" in the JSON.
    from_: str | None = None
    to: str | None = None

    @classmethod
    def from_json(cls, d: dict) -> Case:
        d = dict(d)
        d["from_"] = d.pop("from", None)
        return cls.model_validate(d)


def agent_direction(a: IncidentAnalysis) -> str:
    if a.trigger_path.value == "NONE":
        return "no_ring"
    if a.verdict == Verdict.early_false_alarm:
        return "early"
    if a.verdict in (Verdict.late_due_to_complete_blackout, Verdict.acquisition_failure):
        return "late"
    if a.early_late_seconds is not None:
        if a.early_late_seconds < -ON_TIME_S:
            return "early"
        if a.early_late_seconds > ON_TIME_S:
            return "late"
    return "on_time"


def score(case: Case, result: TriageResult) -> dict:
    exp = case.expected
    a = result.analysis
    row: dict = {
        "id": case.id,
        "ok": a is not None,
        "rule_violations": result.rule_violations,
        "metrics": result.metrics.model_dump(),
        "error": result.error,
    }
    if a is None:
        row.update(
            verdict=False, trigger_path=False, direction=False, blackout=None, failure_class=False
        )
        return row
    row["agent"] = {
        "verdict": a.verdict.value,
        "trigger_path": a.trigger_path.value,
        "direction": agent_direction(a),
        "failure_class": a.failure_class.value,
        "blackout_duration_s": a.blackout_duration_s,
        "doors": a.estimated_arrival_or_doors_time,
        "early_late_seconds": a.early_late_seconds,
        "confidence": a.confidence.value,
        "needs_fix_investigation": a.needs_fix_investigation,
        "items_to_inspect": a.items_to_inspect,
    }
    row["verdict"] = a.verdict.value in exp.verdicts
    row["trigger_path"] = a.trigger_path.value == exp.trigger_path
    row["direction"] = agent_direction(a) == exp.direction
    row["failure_class"] = a.failure_class.value in exp.failure_class
    if exp.blackout is None:
        row["blackout"] = None
    else:
        reported = (a.blackout_duration_s or 0) >= BLACKOUT_S
        row["blackout"] = reported == exp.blackout
    row["doors_error_s"] = None
    if exp.doors_phone and a.estimated_arrival_or_doors_time:
        try:
            truth = x.parse_ts(exp.doors_phone)
            got = _as_time(a.estimated_arrival_or_doors_time, truth)
            row["doors_error_s"] = round(abs((got - truth).total_seconds()), 1)
        except ValueError:
            row["doors_error_s"] = "unparseable"
    row["early_late_error_s"] = None
    if exp.early_late_s is not None and a.early_late_seconds is not None:
        row["early_late_error_s"] = round(abs(a.early_late_seconds - exp.early_late_s), 1)
    return row


def _as_time(text: str, ref: datetime) -> datetime:
    text = text.strip().replace("T", " ")
    if len(text) <= 12:
        h, m, *rest = text.split(":")
        s = float(rest[0]) if rest else 0.0
        return ref.replace(hour=int(h), minute=int(m), second=int(s), microsecond=0)
    return x.parse_ts(text)


def summarize(rows: list[dict]) -> dict:
    def rate(key):
        scored = [r[key] for r in rows if r.get(key) is not None]
        return {"correct": sum(1 for v in scored if v is True), "scored": len(scored)}

    walls = [r["metrics"]["wall_seconds"] for r in rows if r.get("metrics")]
    calls = [r["metrics"]["tool_calls"] for r in rows if r.get("metrics")]
    costs = [r["metrics"]["cost_usd"] for r in rows if r.get("metrics", {}).get("cost_usd")]
    doors = [r["doors_error_s"] for r in rows if isinstance(r.get("doors_error_s"), int | float)]
    el = [
        r["early_late_error_s"]
        for r in rows
        if isinstance(r.get("early_late_error_s"), int | float)
    ]
    return {
        "cases": len(rows),
        "completed": sum(1 for r in rows if r["ok"]),
        "verdict": rate("verdict"),
        "trigger_path": rate("trigger_path"),
        "direction": rate("direction"),
        "blackout": rate("blackout"),
        "failure_class": rate("failure_class"),
        "doors_error_s": {
            "n": len(doors),
            "median": statistics.median(doors) if doors else None,
            "max": max(doors) if doors else None,
        },
        "early_late_error_s": {
            "n": len(el),
            "median": statistics.median(el) if el else None,
            "max": max(el) if el else None,
        },
        "wall_seconds": {
            "median": statistics.median(walls) if walls else None,
            "min": min(walls) if walls else None,
            "max": max(walls) if walls else None,
            "total": round(sum(walls), 1),
        },
        "tool_calls": {"median": statistics.median(calls) if calls else None, "total": sum(calls)},
        "cost_usd": {
            "total": round(sum(costs), 2) if costs else None,
            "median": round(statistics.median(costs), 3) if costs else None,
        },
        "cases_with_rule_violations": sum(1 for r in rows if r.get("rule_violations")),
    }


async def run_case(case: Case, *, offline: bool, model: str, out_dir: Path) -> dict:
    from .triage import triage_file

    result, run_dir = await triage_file(
        Path(case.report),
        case.notes,
        offline=offline,
        model=model,
        focus=case.focus,
        focus_from=x.parse_ts(case.from_) if case.from_ else None,
        focus_to=x.parse_ts(case.to) if case.to else None,
        note_written_at=x.parse_ts(case.note_written_at) if case.note_written_at else None,
        corpus_cutoff=date.fromisoformat(case.ride_date),
        run_dir=out_dir / case.id,
    )
    row = score(case, result)
    row["run_dir"] = str(run_dir)
    return row


async def run_all(
    cases: list[Case], *, offline: bool, model: str, out_dir: Path, parallel: int
) -> list[dict]:
    sem = asyncio.Semaphore(parallel)

    async def one(c: Case) -> dict:
        async with sem:
            try:
                row = await run_case(c, offline=offline, model=model, out_dir=out_dir)
            except Exception as e:  # keep the failure as a scored row, never drop the case
                row = {
                    "id": c.id,
                    "ok": False,
                    "error": f"{type(e).__name__}: {e}",
                    "verdict": False,
                    "trigger_path": False,
                    "direction": False,
                    "blackout": None,
                    "failure_class": False,
                    "metrics": None,
                }
            print(
                f"{c.id}: verdict={row.get('verdict')} path={row.get('trigger_path')} "
                f"dir={row.get('direction')} fail={row.get('failure_class')}",
                flush=True,
            )
            return row

    return await asyncio.gather(*(one(c) for c in cases))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Blind evaluation of the triage agent.")
    p.add_argument("--cases", type=Path, default=Path("local_data/eval/cases.json"))
    p.add_argument("--only", action="append", help="Run only these case ids")
    p.add_argument("--offline", action="store_true")
    p.add_argument("--model", default="claude-opus-5-5")
    p.add_argument("--parallel", type=int, default=3)
    args = p.parse_args(argv)

    cases = [Case.from_json(c) for c in json.loads(args.cases.read_text())["cases"]]
    if args.only:
        cases = [c for c in cases if c.id in args.only]
    out_dir = args.cases.parent / f"run-{datetime.now():%Y%m%d-%H%M%S}"
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = asyncio.run(
        run_all(
            cases, offline=args.offline, model=args.model, out_dir=out_dir, parallel=args.parallel
        )
    )
    summary = summarize(rows)
    (out_dir / "results.json").write_text(
        json.dumps({"summary": summary, "cases": rows}, indent=2, default=str), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))
    print(f"results: {out_dir / 'results.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
