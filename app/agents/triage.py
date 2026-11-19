"""Triage agent: one report (+ optional rider notes) in, a validated IncidentAnalysis out.

    python -m app.agents.triage --report path/to/report.txt \\
        --notes "lap started 15:05, train moved +47 s, doors +33m10s after that"

By default the report is ingested into PostgreSQL with the same ingest_report the API uses, and
the tools read it back through the API's query clauses. --offline parses it in memory instead
(no database; cross-report search is then unavailable).

The model runs through the Claude Agent SDK with built-in tools disabled: it can call only the
vergeo tools (app.agents.tools), it does not load any local settings or CLAUDE.md, and its answer
must match the IncidentAnalysis JSON schema. The answer is then checked by app.agents.rules; one
repair round is allowed, and what remains is reported as rule_violations.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from . import extract as x
from .corpus import CorpusIndex
from .privacy import AuditLog, scrub_text
from .prompts import REPAIR_PROMPT, TRIAGE_SYSTEM, triage_task
from .rules import check, facts_for
from .schemas import IncidentAnalysis, RunMetrics, TriageResult
from .source import EventSource, ParsedSource
from .tools import ToolBox, build_sdk_server

DEFAULT_MODEL = "claude-opus-5-5"
RUNS_DIR = Path("local_data") / "agent_runs"


@dataclass
class ModelReply:
    structured: dict | None
    text: str | None
    turns: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cost_usd: float | None = None
    error: str | None = None


# A Session sends one prompt and returns the model's final structured reply. The SDK-backed one
# keeps the conversation, so the repair prompt is a follow-up turn, not a fresh run. Tests pass
# a scripted fake.
Session = Callable[[str], Awaitable[ModelReply]]


class SdkSession:
    def __init__(self, box: ToolBox, *, model: str, max_turns: int = 40):
        self.box = box
        self.model = model
        self.max_turns = max_turns
        self._client = None

    async def __aenter__(self):
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

        server, allowed = build_sdk_server(self.box)
        options = ClaudeAgentOptions(
            system_prompt=TRIAGE_SYSTEM,
            model=self.model,
            tools=[],  # no Read/Bash/Edit/Web: only the vergeo MCP tools below
            mcp_servers={"vergeo": server},
            allowed_tools=allowed,
            setting_sources=[],  # ignore user/project settings and CLAUDE.md
            max_turns=self.max_turns,
            output_format={"type": "json_schema", "schema": IncidentAnalysis.model_json_schema()},
        )
        self._client = ClaudeSDKClient(options=options)
        await self._client.connect()
        return self

    async def __aexit__(self, *exc):
        if self._client is not None:
            await self._client.disconnect()

    async def __call__(self, prompt: str) -> ModelReply:
        from claude_agent_sdk import ResultMessage

        self.box.audit.record("prompt", "user", None, prompt)
        await self._client.query(prompt)
        reply = ModelReply(structured=None, text=None)
        async for message in self._client.receive_response():
            if isinstance(message, ResultMessage):
                usage = message.usage or {}
                reply = ModelReply(
                    structured=message.structured_output,
                    text=message.result,
                    turns=message.num_turns,
                    input_tokens=usage.get("input_tokens"),
                    output_tokens=usage.get("output_tokens"),
                    cache_read_tokens=usage.get("cache_read_input_tokens"),
                    cost_usd=message.total_cost_usd,
                    error=(message.subtype if message.is_error else None),
                )
        return reply


def parse_analysis(reply: ModelReply) -> tuple[IncidentAnalysis | None, str | None]:
    """Validate the model's answer. Falls back to JSON in the text if no structured output came
    back (an SDK error path), and reports why it failed instead of raising."""
    candidate = reply.structured
    if candidate is None and reply.text:
        try:
            start, end = reply.text.index("{"), reply.text.rindex("}") + 1
            candidate = json.loads(reply.text[start:end])
        except ValueError:
            candidate = None
    if candidate is None:
        return None, f"no analysis returned ({reply.error or 'empty reply'})"
    try:
        return IncidentAnalysis.model_validate(candidate), None
    except ValidationError as e:
        return None, f"analysis failed schema validation: {e.error_count()} errors: {e}"


async def run_triage(
    box: ToolBox,
    session: Session,
    *,
    notes: str | None,
    focus: str | None = None,
    model: str | None = None,
) -> TriageResult:
    started = time.perf_counter()
    totals = {"turns": 0, "in": 0, "out": 0, "cache": 0, "cost": 0.0}

    def add(r: ModelReply) -> None:
        totals["turns"] += r.turns or 0
        totals["in"] += r.input_tokens or 0
        totals["out"] += r.output_tokens or 0
        totals["cache"] += r.cache_read_tokens or 0
        totals["cost"] += r.cost_usd or 0.0

    prompt = triage_task(box.source.meta.report_id, scrub_text(notes) if notes else None, focus)
    reply = await session(prompt)
    add(reply)
    analysis, error = parse_analysis(reply)
    violations: list[str] = []
    repairs = 0

    if analysis is None:
        # One retry for a malformed answer, with the validation error as the instruction.
        repairs += 1
        reply = await session(
            f"Your reply was not a valid IncidentAnalysis: {error}. Return it "
            "again, complete and valid."
        )
        add(reply)
        analysis, error = parse_analysis(reply)

    if analysis is not None:
        facts = facts_for(analysis_window_events(box, analysis))
        violations = check(analysis, facts)
        if violations and repairs < 2:
            repairs += 1
            reply = await session(
                REPAIR_PROMPT.format(violations="\n".join(f"- {v}" for v in violations))
            )
            add(reply)
            repaired, rep_error = parse_analysis(reply)
            if repaired is not None:
                analysis = repaired
                violations = check(analysis, facts_for(analysis_window_events(box, analysis)))
            else:
                violations.append(f"repair round failed: {rep_error}")

    metrics = RunMetrics(
        wall_seconds=round(time.perf_counter() - started, 1),
        tool_calls=sum(box.calls.values()),
        tool_calls_by_name=dict(box.calls),
        model_turns=totals["turns"] or None,
        input_tokens=totals["in"] or None,
        output_tokens=totals["out"] or None,
        cache_read_tokens=totals["cache"] or None,
        cost_usd=round(totals["cost"], 4) if totals["cost"] else None,
        events_returned=box.events_returned,
        bytes_to_model=box.audit.bytes_sent,
        model=model,
        repair_rounds=repairs,
    )
    return TriageResult(
        analysis=analysis,
        rule_violations=violations,
        metrics=metrics,
        error=None if analysis else error,
    )


def analysis_window_events(box: ToolBox, a: IncidentAnalysis):
    """The events the rules judge against: the alarm session that contains the analysis's
    alarm_time (or arm time), else the focus window."""
    anchor = a.alarm_time or a.alarm_arm_time
    sessions = x.alarm_sessions(box.source.events())
    if anchor:
        for s in sessions:
            start, end = x.parse_ts(s["armed_at"]), x.parse_ts(s["ended_at"])
            try:
                t = _anchor_time(anchor, start)
            except ValueError:
                break
            if start <= t <= end:
                return box.source.events(ts_from=start, ts_to=end)
    return box.evs()


def _anchor_time(text: str, ref: datetime) -> datetime:
    text = text.strip().replace("T", " ")
    if len(text) <= 12:
        h, m, *rest = text.split(":")
        sec = float(rest[0]) if rest else 0.0
        return ref.replace(
            hour=int(h), minute=int(m), second=int(sec), microsecond=int((sec % 1) * 1e6)
        )
    return x.parse_ts(text)


# --- CLI ---------------------------------------------------------------------------------------


def make_source(report: Path, *, offline: bool):
    """Returns (source, cross_report_fn, closer)."""
    if offline:
        return ParsedSource.from_file(report), None, lambda: None
    from .db_tools import open_postgres_source

    return open_postgres_source(report)


def make_box(
    source: EventSource,
    run_dir: Path,
    *,
    corpus_cutoff=None,
    focus_from=None,
    focus_to=None,
    note_written_at=None,
    cross_report=None,
) -> ToolBox:
    box = ToolBox(
        source=source,
        corpus=CorpusIndex.load().visible_before(corpus_cutoff),
        audit=AuditLog(run_dir / "audit.jsonl"),
        note_written_at=note_written_at,
        cross_report=cross_report,
    )
    if focus_from and focus_to:
        box.focus_from, box.focus_to = focus_from, focus_to
    else:
        box.default_focus()
    return box


async def triage_file(
    report: Path,
    notes: str | None,
    *,
    offline: bool,
    model: str,
    focus: str | None = None,
    focus_from=None,
    focus_to=None,
    note_written_at=None,
    corpus_cutoff=None,
    run_dir: Path | None = None,
) -> tuple[TriageResult, Path]:
    run_dir = run_dir or RUNS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
    run_dir.mkdir(parents=True, exist_ok=True)
    source, cross, close = make_source(report, offline=offline)
    try:
        box = make_box(
            source,
            run_dir,
            corpus_cutoff=corpus_cutoff,
            focus_from=focus_from,
            focus_to=focus_to,
            note_written_at=note_written_at,
            cross_report=cross,
        )
        async with SdkSession(box, model=model) as session:
            result = await run_triage(box, session, notes=notes, focus=focus, model=model)
    finally:
        close()
    (run_dir / "result.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return result, run_dir


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Triage one Vergeo problem report with the agent.")
    p.add_argument("--report", type=Path, required=True)
    p.add_argument("--notes", help="The rider's lap notes, verbatim")
    p.add_argument("--focus", help="Which ride, in words (e.g. '2026-11-18 afternoon SB ride')")
    p.add_argument("--from", dest="ts_from", help="Focus window start, phone clock")
    p.add_argument("--to", dest="ts_to", help="Focus window end, phone clock")
    p.add_argument("--note-time", help="When the notes were written, for 'N minutes ago'")
    p.add_argument("--offline", action="store_true", help="Parse in memory; no database")
    p.add_argument("--model", default=DEFAULT_MODEL)
    args = p.parse_args(argv)

    result, run_dir = asyncio.run(
        triage_file(
            args.report,
            args.notes,
            offline=args.offline,
            model=args.model,
            focus=args.focus,
            focus_from=x.parse_ts(args.ts_from) if args.ts_from else None,
            focus_to=x.parse_ts(args.ts_to) if args.ts_to else None,
            note_written_at=x.parse_ts(args.note_time) if args.note_time else None,
        )
    )
    print(result.model_dump_json(indent=2))
    print(f"\nrun saved to {run_dir}", file=sys.stderr)
    return 0 if result.analysis else 1


if __name__ == "__main__":
    raise SystemExit(main())
