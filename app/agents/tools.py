"""The triage agent's tools: narrow, typed, read-only views of one report.

Why narrow tools instead of shell access:

- The model never gets the filesystem, the raw 2 MB report or a database connection. It gets the
  rows a tool chooses to return, after app.agents.privacy.sanitize.
- Each tool answers one forensic question the way the Vergeo field notes answer it (fixes and
  blackouts, what rang, route records, safety-net passes, R4 shadow, motion), so the model spends
  its turns reasoning instead of re-deriving log grammar.
- Every call is counted and audited (RunMetrics, AuditLog), which is what makes runtime and
  "what did the model see" measurable.

ToolBox holds the per-run state. build_sdk_server() exposes the same functions to the Claude
Agent SDK as an in-process MCP server; tests call the functions directly.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from . import extract as x
from .corpus import CorpusIndex
from .laps import align, parse_laps
from .privacy import AuditLog, sanitize
from .source import EventSource

MAX_REPLY_BYTES = 48_000
TIME_ARGS = {
    "from_time": {
        "type": "string",
        "description": "Phone clock YYYY-MM-DD HH:MM:SS; default: the focus window start",
    },
    "to_time": {
        "type": "string",
        "description": "Phone clock YYYY-MM-DD HH:MM:SS; default: the focus window end",
    },
}


@dataclass
class ToolSpec:
    name: str
    description: str
    schema: dict
    fn: Callable[..., Any]


@dataclass
class ToolBox:
    source: EventSource
    corpus: CorpusIndex
    audit: AuditLog
    focus_from: datetime | None = None
    focus_to: datetime | None = None
    note_written_at: datetime | None = None
    cross_report: Callable[..., dict] | None = None  # set only when a database is available
    calls: Counter = field(default_factory=Counter)
    events_returned: int = 0

    # --- helpers -------------------------------------------------------------------------------

    def window(self, from_time: str | None, to_time: str | None) -> tuple[datetime, datetime]:
        start = x.parse_ts(from_time) if from_time else self.focus_from
        end = x.parse_ts(to_time) if to_time else self.focus_to
        if start is None or end is None:
            start = start or self.source.meta.window_start
            end = end or (self.source.meta.window_end + timedelta(seconds=1))
        return start, end

    def evs(self, from_time=None, to_time=None, tags=None):
        start, end = self.window(from_time, to_time)
        return self.source.events(tags=tags, ts_from=start, ts_to=end)

    def default_focus(self) -> None:
        """The latest alarm session, from 60 min before arming to 10 min after it ended: rides are
        reported right after they happen, so the last session is the usual subject."""
        sessions = x.alarm_sessions(self.source.events())
        if not sessions:
            return
        s = sessions[-1]
        self.focus_from = x.parse_ts(s["armed_at"]) - timedelta(minutes=60)
        self.focus_to = x.parse_ts(s["ended_at"]) + timedelta(minutes=10)

    # --- tools ---------------------------------------------------------------------------------

    def get_report_overview(self) -> dict:
        all_evs = self.source.events()
        m = self.source.meta
        tags = Counter(e.tag for e in all_evs)
        return {
            "report_id": m.report_id,
            "app": {
                "version": m.app_version,
                "code": m.version_code,
                "device": m.device_model,
                "android_api": m.android_api,
            },
            "env_at_report": m.env,
            "user_description": m.description[:500],
            "window": [x.fmt(m.window_start), x.fmt(m.window_end)],
            "event_count": m.event_count,
            "builds": x.builds(all_evs),
            "alarm_sessions": x.alarm_sessions(all_evs),
            "focus_window": [x.fmt(self.focus_from), x.fmt(self.focus_to)],
            "top_tags": dict(tags.most_common(25)),
        }

    def get_location_fixes(
        self, from_time=None, to_time=None, max_rows: int = 120, min_gap_s: float = 120
    ) -> dict:
        evs = self.evs(from_time, to_time)
        summary = x.location_summary(evs, min_gap_s=min_gap_s)
        rows, sampled = x.downsample(x.fixes(evs), max(10, min(int(max_rows), 300)))
        return {**summary, "fixes": rows, "fixes_downsampled": sampled}

    def get_rings(self, from_time=None, to_time=None) -> dict:
        return {"rings": x.find_rings(self.evs(from_time, to_time))}

    def get_alarm_decisions(self, from_time=None, to_time=None) -> dict:
        rows = x.decisions(self.evs(from_time, to_time))
        rows, sampled = x.downsample(rows, 250)
        return {"decisions": rows, "downsampled": sampled}

    def get_route_state(self, from_time=None, to_time=None) -> dict:
        return x.route_state(self.evs(from_time, to_time))

    def get_safety_net_passes(self, from_time=None, to_time=None) -> dict:
        return {"passes": x.safety_net(self.evs(from_time, to_time))}

    def get_ml_predictions(self, from_time=None, to_time=None) -> dict:
        return x.ml_shadow(self.evs(from_time, to_time))

    def get_motion(self, from_time=None, to_time=None) -> dict:
        evs = self.evs(from_time, to_time)
        out = x.motion(evs)
        out["segments"], sampled = x.downsample(out["segments"], 200)
        out["segments_downsampled"] = sampled
        out["screen_events"] = x.screen_events(evs)
        return out

    def query_report_events(
        self,
        tags: list[str],
        from_time=None,
        to_time=None,
        fields: list[str] | None = None,
        limit: int = 60,
    ) -> dict:
        """Escape hatch: raw event rows for named tags, filtered by key:value pairs."""
        want = [f.partition(":") for f in fields or []]
        rows = []
        for e in self.evs(from_time, to_time, tags=tags):
            if all(e.get(k) == v for k, _, v in want):
                rows.append(
                    {
                        "time": x.fmt(e.ts),
                        "tag": e.tag,
                        "subtag": e.subtag,
                        "alarm": x.short_alarm(e.alarm_id),
                        "fields": e.fields,
                    }
                )
        total = len(rows)
        rows, sampled = x.downsample(rows, max(1, min(int(limit), 120)))
        return {"total_matching": total, "rows": rows, "downsampled": sampled}

    def search_historical_incidents(self, query: str, limit: int = 5) -> dict:
        return {
            "field_corpus": self.corpus.search(query, limit=max(1, min(int(limit), 10))),
            "visible_sections": len(self.corpus.sections),
        }

    def read_field_corpus(self, section: int | None = None) -> dict:
        if section is None:
            return {"sections": self.corpus.titles()}
        return self.corpus.read(int(section))

    def query_all_reports(self, tag: str, fields: list[str] | None = None, limit: int = 40) -> dict:
        if self.cross_report is None:
            return {"error": "cross-report search needs the database; this run is offline"}
        return self.cross_report(tag=tag, fields=fields or [], limit=max(1, min(int(limit), 100)))

    def parse_user_laps(self, note: str) -> dict:
        return parse_laps(note).to_dict()

    def align_laps_to_phone_time(self, note: str, around: str | None = None) -> dict:
        evs = self.evs()
        rings = [x.parse_ts(r["time"]) for r in x.find_rings(evs)]
        vehicle = [
            x.parse_ts(s["from"]) for s in x.motion(evs)["segments"] if s["state"] == "vehicle"
        ]
        ref = x.parse_ts(around) if around else (rings[-1] if rings else self.window(None, None)[0])
        result = align(
            parse_laps(note),
            around=ref,
            ring_times=rings,
            vehicle_starts=vehicle,
            note_written_at=self.note_written_at,
        )
        return result.to_dict()

    # --- registry ------------------------------------------------------------------------------

    def specs(self) -> list[ToolSpec]:
        t = TIME_ARGS

        def obj(props: dict, req: tuple = ()) -> dict:
            return {"type": "object", "properties": props, "required": list(req)}

        return [
            ToolSpec(
                "get_report_overview",
                "App/build, the report window, every alarm session "
                "(armed, rings with trigger path, end) and the default focus window. Call "
                "first.",
                obj({}),
                self.get_report_overview,
            ),
            ToolSpec(
                "get_location_fixes",
                "Accepted location fixes in a window with provider, "
                "source (GNSS vs network-only), accuracy, distance to the destination; plus "
                "gaps (blackouts) and the longest satellite-only gap.",
                obj({**t, "max_rows": {"type": "integer"}, "min_gap_s": {"type": "number"}}),
                self.get_location_fixes,
            ),
            ToolSpec(
                "get_rings",
                "Every full alarm (ESCALATION_START) with the trigger line that "
                "caused it and its trigger path.",
                obj(t),
                self.get_rings,
            ),
            ToolSpec(
                "get_alarm_decisions",
                "Trigger allowed/held/blocked, backstop owners, "
                "time-backstop / blind-hop / near-stop lifecycle, enables and stops, with "
                "repeated per-pass lines collapsed.",
                obj(t),
                self.get_alarm_decisions,
            ),
            ToolSpec(
                "get_route_state",
                "Station route records (fold, spread, provenance), station "
                "topology checks (ELIGIBLE/INELIGIBLE/UNKNOWN, directed route range) and "
                "offline transit hypotheses.",
                obj(t),
                self.get_route_state,
            ),
            ToolSpec(
                "get_safety_net_passes",
                "Per-pass reasons of the near-stop (lm), "
                "time-backstop (tb) and blind-hop (bh) tiers, collapsed into runs.",
                obj(t),
                self.get_safety_net_passes,
            ),
            ToolSpec(
                "get_ml_predictions",
                "R4 ML shadow predictions (ETA, predicted crossing) and "
                "gated passes. Shadow only: never alarm authority.",
                obj(t),
                self.get_ml_predictions,
            ),
            ToolSpec(
                "get_motion",
                "Accelerometer motion segments (still/walking/vehicle), motion "
                "episode resets and screen-on moments.",
                obj(t),
                self.get_motion,
            ),
            ToolSpec(
                "query_report_events",
                "Raw event rows for given tags, optionally filtered by "
                "key:value field pairs. Use when the other tools do not cover a tag.",
                obj(
                    {
                        **t,
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "fields": {"type": "array", "items": {"type": "string"}},
                        "limit": {"type": "integer"},
                    },
                    ["tags"],
                ),
                self.query_report_events,
            ),
            ToolSpec(
                "search_historical_incidents",
                "Keyword search over the hand-written field "
                "corpus of past rides (classifications, root causes).",
                obj({"query": {"type": "string"}, "limit": {"type": "integer"}}, ["query"]),
                self.search_historical_incidents,
            ),
            ToolSpec(
                "read_field_corpus",
                "Read one field-corpus section by number, or list "
                "section headings when no number is given.",
                obj({"section": {"type": "integer"}}),
                self.read_field_corpus,
            ),
            ToolSpec(
                "query_all_reports",
                "Count and sample one tag across every stored report "
                "(database runs only), filtered by key:value pairs.",
                obj(
                    {
                        "tag": {"type": "string"},
                        "fields": {"type": "array", "items": {"type": "string"}},
                        "limit": {"type": "integer"},
                    },
                    ["tag"],
                ),
                self.query_all_reports,
            ),
            ToolSpec(
                "parse_user_laps",
                "Parse the rider's lap notes into explicit timing facts.",
                obj({"note": {"type": "string"}}, ["note"]),
                self.parse_user_laps,
            ),
            ToolSpec(
                "align_laps_to_phone_time",
                "Place the rider's laps on the phone clock using "
                "logged anchors (ring, train motion) and report the offset uncertainty.",
                obj({"note": {"type": "string"}, "around": {"type": "string"}}, ["note"]),
                self.align_laps_to_phone_time,
            ),
        ]

    def call(self, name: str, args: dict) -> str:
        """Run one tool and return the exact text the model will see."""
        spec = next((s for s in self.specs() if s.name == name), None)
        self.calls[name] += 1
        if spec is None:
            payload = json.dumps({"error": f"unknown tool {name}"})
        else:
            try:
                result = sanitize(spec.fn(**(args or {})))
                self.events_returned += count_rows(result)
                payload = json.dumps(result, default=str, separators=(",", ":"))
            except (TypeError, ValueError, LookupError) as e:
                payload = json.dumps({"error": f"{type(e).__name__}: {e}"})
            if len(payload.encode()) > MAX_REPLY_BYTES:
                payload = payload.encode()[:MAX_REPLY_BYTES].decode(errors="ignore")
                payload += '..."[truncated: narrow the time window or lower max_rows]"'
        self.audit.record("tool_result", name, sanitize(args), payload)
        return payload


def count_rows(value: Any) -> int:
    """Event-like rows (dicts with a time) in a tool result, for RunMetrics.events_returned."""
    if isinstance(value, dict):
        own = 1 if "time" in value else 0
        return own + sum(count_rows(v) for v in value.values())
    if isinstance(value, list):
        return sum(count_rows(v) for v in value)
    return 0


def build_sdk_server(box: ToolBox):
    """Expose the ToolBox to the Claude Agent SDK as an in-process MCP server named "vergeo"."""
    from claude_agent_sdk import create_sdk_mcp_server, tool

    def make(spec: ToolSpec):
        @tool(spec.name, spec.description, spec.schema)
        async def handler(args: dict) -> dict:
            return {"content": [{"type": "text", "text": box.call(spec.name, args)}]}

        return handler

    specs = box.specs()
    server = create_sdk_mcp_server("vergeo", tools=[make(s) for s in specs])
    return server, [f"mcp__vergeo__{s.name}" for s in specs]
