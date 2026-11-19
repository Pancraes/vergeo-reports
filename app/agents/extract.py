"""Deterministic readings of a Vergeo diagnostic log: the facts a triage needs, computed without a
model.

Each function takes Ev rows (app.agents.source) and returns small JSON-ready dicts. The agent
tools are thin wrappers over these, and app.agents.rules checks the agent's answer against the
same functions, so a claim like "the ring was ARRIVAL on the first fix after a 38 min blackout"
is verified by code, not by the model's say-so.

Tag knowledge used here, all from the Vergeo source and FIELD_CORPUS:

- A ring is ESCALATION_START. The TRIGGER_ALLOWED just before it says what rang: path=ARRIVAL, or
  reason=TIME_BACKSTOP with tier=strong / weak / blind_motion. Blind-hop writes the time-backstop
  reason too (a shared label), so the tier decides.
- An accepted fix is DIAG2|FIX. src=FUSED_NO_GNSS is a network-only fix: it lags the train by
  kilometres and is not satellite evidence (2026-11-06), so it is counted separately.
- The near-stop tier is LAST_MILE_SIGNAL_LOSS_*.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta

from .source import Ev

TS_FORMAT = "%Y-%m-%d %H:%M:%S.%f"

RING_TAG = "ESCALATION_START"
TRIGGER_TAGS = ("TRIGGER_ALLOWED", "TRIGGER_HELD", "TRIGGER_BLOCKED")
# Network-only and geofence fixes are positions, but not satellite positions.
NON_GNSS_SOURCES = {"FUSED_NO_GNSS", "GEOFENCE"}

DECISION_PREFIXES = (
    "TRIGGER_",
    "ESCALATION_START",
    "TIME_BACKSTOP_",
    "BLIND_HOP_",
    "BLIND_PROJECTION_",
    "BACKSTOP_OWNER_",
    "LAST_MILE_SIGNAL_LOSS_",
    "MISSED_STOP_RECOVERY_",
    "ALARM_ENABLED",
    "ALARM_DISABLED",
    "ALARM_UPSERT",
    "ALARM_EDIT_",
    "CREATED_INSIDE_",
    "CONFIRMED_EXIT",
    "JS_ALARM_STOPPED",
    "AUDIO_STOP",
    "REENTRY_DETECTED",
    "TRANSIT_BRIDGE",
)
# Tags that repeat on every evaluator pass; runs of them are collapsed to first/last/count.
REPEATING_DECISIONS = {
    "BLIND_HOP_ROUTE_HELD",
    "LAST_MILE_SIGNAL_LOSS_BLOCKED",
    "MISSED_STOP_RECOVERY_BLOCKED",
    "MISSED_STOP_RECOVERY_CANDIDATE",
    "BACKSTOP_OWNER_BLOCKED",
    "BACKSTOP_OWNER_RETAINED",
    "TIME_BACKSTOP_CANDIDATE",
    "BLIND_HOP_CANDIDATE",
    "REENTRY_DETECTED",
}
# The fields worth showing per decision tag. Everything else on those lines is internal state.
DECISION_FIELDS = (
    "path",
    "reason",
    "tier",
    "dist",
    "rad",
    "canonRad",
    "effRad",
    "commitRad",
    "acc",
    "prov",
    "spd",
    "was",
    "in",
    "minDist",
    "createdInside",
    "exitSeen",
    "dec",
    "why",
    "netMin",
    "netMax",
    "dTrack",
    "rdec",
    "rwhy",
    "rMin",
    "rMax",
    "oldOwner",
    "newOwner",
    "selectedDeadline",
    "deadline",
    "anchorReason",
    "remainingM",
    "selectedMps",
    "paceSource",
    "lastFixDist",
    "blackoutMs",
    "cancelReason",
    "source",
    "baseRad",
    "timing",
    "active",
    "seed",
    "fresh",
    "editOfActive",
    "jumpM",
    "gapS",
    "routeRemainM",
)


def fmt(ts: datetime | None) -> str | None:
    return ts.strftime(TS_FORMAT)[:-3] if ts else None


def parse_ts(text: str) -> datetime:
    for f in (TS_FORMAT, "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, f)
        except ValueError:
            continue
    raise ValueError(f"unrecognised time {text!r}; use YYYY-MM-DD HH:MM:SS[.fff]")


def short_alarm(alarm_id: str | None) -> str | None:
    """The first 6 hex digits: enough to tell a report's alarms apart, not the raw id."""
    return f"alarm-{alarm_id.replace('-', '')[:6]}" if alarm_id else None


def pick(ev: Ev, keys: Iterable[str]) -> dict:
    out = {}
    for k in keys:
        v = ev.get(k)
        if v not in (None, ""):
            out[k] = v
    return out


# --- rings and alarm sessions ------------------------------------------------------------------


def ring_source(trigger: Ev | None) -> str:
    """Map the TRIGGER_ALLOWED line that rang to a schemas.TriggerPath value."""
    if trigger is None:
        return "NONE"
    if trigger.get("path") == "ARRIVAL":
        return "ARRIVAL"
    reason = trigger.get("reason") or ""
    tier = trigger.get("tier") or ""
    if "LAST_MILE" in reason or "NEAR" in reason:
        return "NEAR_STOP"
    if reason == "TIME_BACKSTOP":
        if tier == "blind_motion":
            return "BLIND_HOP"
        if tier == "weak":
            return "TIME_BACKSTOP_WEAK"
        if tier in ("strong", "time_backstop"):
            return "TIME_BACKSTOP_STRONG"
    return "OTHER"


def find_rings(evs: Sequence[Ev]) -> list[dict]:
    """Every ESCALATION_START with the trigger line that caused it (the last TRIGGER_ALLOWED for
    the same alarm within 2 s before it)."""
    rings = []
    for i, ev in enumerate(evs):
        if ev.tag != RING_TAG:
            continue
        trigger = None
        for prev in reversed(evs[max(0, i - 400) : i]):
            if ev.ts - prev.ts > timedelta(seconds=2):
                break
            if prev.tag == "TRIGGER_ALLOWED" and prev.alarm_id == ev.alarm_id:
                trigger = prev
                break
        rings.append(
            {
                "time": fmt(ev.ts),
                "alarm": short_alarm(ev.alarm_id),
                "trigger_path": ring_source(trigger),
                "trigger": pick(trigger, DECISION_FIELDS) if trigger else None,
                "acc": ev.get("ACC"),
            }
        )
    return rings


def alarm_sessions(evs: Sequence[Ev]) -> list[dict]:
    """Arm-to-end windows per alarm, so the agent can pick the ride the rider is talking about.

    A session starts at ALARM_ENABLED and ends at the next ALARM_DISABLED, ALARM_ENABLED or the
    stop after a ring, whichever is first. A TRIGGER_ALLOWED with no ESCALATION_START after it is
    reported separately: 2026-11-09 23:53:50 logged an ARRIVAL that a stuck state then blocked.
    """
    sessions: list[dict] = []
    open_by_alarm: dict[str, dict] = {}
    last_upsert: dict[str, Ev] = {}
    rings = {(r["alarm"], r["time"]): r for r in find_rings(evs)}

    def close(alarm: str, ts: datetime, why: str) -> None:
        s = open_by_alarm.pop(alarm, None)
        if s:
            s["ended_at"] = fmt(ts)
            s["end_reason"] = why
            sessions.append(s)

    for ev in evs:
        a = short_alarm(ev.alarm_id)
        if a is None:
            continue
        if ev.tag == "ALARM_UPSERT":
            last_upsert[a] = ev
        elif ev.tag == "ALARM_ENABLED":
            close(a, ev.ts, "re_enabled")
            up = last_upsert.get(a)
            open_by_alarm[a] = {
                "alarm": a,
                "armed_at": fmt(ev.ts),
                "settings": pick(up, ("type", "mode", "timing", "baseRad")) if up else {},
                "rings": [],
                "unrung_triggers": [],
            }
        elif ev.tag == "ALARM_DISABLED":
            close(a, ev.ts, "disabled")
        elif a in open_by_alarm:
            s = open_by_alarm[a]
            if ev.tag == RING_TAG:
                s["rings"].append(rings.get((a, fmt(ev.ts))))
            elif ev.tag == "TRIGGER_ALLOWED":
                s["_pending_trigger"] = ev
            elif ev.tag == "TRIGGER_BLOCKED" and s.get("_pending_trigger") is not None:
                t = s.pop("_pending_trigger")
                if ev.ts - t.ts <= timedelta(seconds=2):
                    s["unrung_triggers"].append(
                        {
                            "time": fmt(t.ts),
                            "trigger": pick(t, DECISION_FIELDS),
                            "blocked_by": ev.get("reason"),
                        }
                    )
            elif ev.tag == "JS_ALARM_STOPPED_EVENT_SENT" and s["rings"]:
                close(a, ev.ts, "stopped_after_ring")
    end = evs[-1].ts if evs else None
    for a in list(open_by_alarm):
        close(a, end, "report_ended")  # type: ignore[arg-type]
    for s in sessions:
        s.pop("_pending_trigger", None)
    return sorted(sessions, key=lambda s: s["armed_at"])


# --- location ----------------------------------------------------------------------------------


def fixes(evs: Sequence[Ev]) -> list[dict]:
    out = []
    for ev in evs:
        if ev.tag != "DIAG2" or ev.subtag != "FIX":
            continue
        out.append(
            {
                "time": fmt(ev.ts),
                "prov": ev.get("prov"),
                "src": ev.get("src"),
                "acc": round(ev.num("acc"), 1) if ev.num("acc") is not None else None,
                "dist": ev.num("dist"),
                "gnssUsed": ev.get("gnssUsed"),
                "screen": ev.get("screen"),
                "app": ev.get("app"),
                "mode": ev.get("mode"),
            }
        )
    # dist=2147483647 is Integer.MAX_VALUE: the alarm was already stopped, no distance computed.
    for f in out:
        if f["dist"] is not None and f["dist"] >= 2_000_000_000:
            f["dist"] = None
    return out


def gaps(fix_rows: Sequence[dict], *, min_gap_s: float, gnss_only: bool = False) -> list[dict]:
    """Periods with no location of at least min_gap_s. gnss_only skips network/geofence fixes,
    which otherwise hide a satellite blackout (2026-11-06 evening: fused network fixes every
    20-30 s in the tunnel kept the blackout under 150 s)."""
    rows = [f for f in fix_rows if not gnss_only or f.get("src") not in NON_GNSS_SOURCES]
    out = []
    for before, after in zip(rows, rows[1:], strict=False):
        t0, t1 = parse_ts(before["time"]), parse_ts(after["time"])
        dt = (t1 - t0).total_seconds()
        if dt >= min_gap_s:
            out.append(
                {
                    "from": before["time"],
                    "to": after["time"],
                    "duration_s": round(dt, 1),
                    "last_fix_before": before,
                    "first_fix_after": after,
                }
            )
    return out


def location_summary(evs: Sequence[Ev], *, min_gap_s: float = 120) -> dict:
    rows = fixes(evs)
    src = Counter(f.get("src") or f.get("prov") for f in rows)
    all_gaps = gaps(rows, min_gap_s=min_gap_s)
    gnss_gaps = gaps(rows, min_gap_s=min_gap_s, gnss_only=True)
    longest = max(all_gaps, key=lambda g: g["duration_s"], default=None)
    return {
        "fix_count": len(rows),
        "fixes_by_source": dict(src),
        "first_fix": rows[0] if rows else None,
        "last_fix": rows[-1] if rows else None,
        "gaps": all_gaps,
        "longest_gap": longest,
        "longest_gnss_gap_s": max((g["duration_s"] for g in gnss_gaps), default=0.0),
        "network_only_fixes": src.get("FUSED_NO_GNSS", 0),
    }


def downsample(rows: list[dict], limit: int) -> tuple[list[dict], bool]:
    """Keep the first and last rows and an even spread between, so a long ride fits a tool reply
    without hiding its ends (where the evidence usually is)."""
    if len(rows) <= limit:
        return rows, False
    head = limit // 3
    tail = limit // 3
    middle = limit - head - tail
    mid_rows = rows[head : len(rows) - tail]
    step = max(1, len(mid_rows) // middle)
    return rows[:head] + mid_rows[::step][:middle] + rows[len(rows) - tail :], True


# --- decisions, route, safety net, ML, motion --------------------------------------------------


def collapse_runs(rows: list[dict], key_fields: Sequence[str]) -> list[dict]:
    """Merge consecutive rows whose key fields are equal into one row with first/last/count."""
    out: list[dict] = []
    for row in rows:
        key = tuple(row.get(k) for k in key_fields)
        if out and out[-1].get("_key") == key:
            out[-1]["count"] += 1
            out[-1]["last"] = row["time"]
            continue
        out.append({**row, "_key": key, "count": 1, "last": row["time"]})
    for row in out:
        row.pop("_key")
        if row["count"] == 1:
            row.pop("count")
            row.pop("last")
    return out


def decisions(evs: Sequence[Ev]) -> list[dict]:
    rows = []
    for ev in evs:
        if not ev.tag.startswith(DECISION_PREFIXES):
            continue
        rows.append(
            {
                "time": fmt(ev.ts),
                "tag": ev.tag,
                "alarm": short_alarm(ev.alarm_id),
                **pick(ev, DECISION_FIELDS),
            }
        )
    out: list[dict] = []
    for row in rows:
        prev = out[-1] if out else None
        if (
            prev is not None
            and row["tag"] in REPEATING_DECISIONS
            and prev["tag"] == row["tag"]
            and prev.get("reason") == row.get("reason")
        ):
            prev["count"] = prev.get("count", 1) + 1
            prev["last"] = row["time"]
            continue
        out.append(row)
    return out


def route_state(evs: Sequence[Ev]) -> dict:
    straight = [
        {
            "time": fmt(e.ts),
            **pick(
                e,
                (
                    "known",
                    "why",
                    "routes",
                    "receding",
                    "origins",
                    "foldExcessM",
                    "spreadM",
                    "straightM",
                    "acc",
                ),
            ),
        }
        for e in evs
        if e.tag == "STATION_STRAIGHT_LINE"
    ]
    shadow = [
        {
            "time": fmt(e.ts),
            **pick(
                e,
                (
                    "phase",
                    "dec",
                    "why",
                    "netMin",
                    "netMax",
                    "dTrack",
                    "dist",
                    "rad",
                    "acc",
                    "rdec",
                    "rwhy",
                    "rMin",
                    "rMax",
                    "hyp",
                    "rdirs",
                    "xfer",
                    "prodFire",
                    "veto",
                ),
            ),
        }
        for e in evs
        if e.tag == "STATION_SHADOW"
    ]
    coarse = [
        {
            "time": fmt(e.ts),
            "tag": e.tag,
            **pick(e, ("acc", "in", "out", "routes", "remainM", "hyps", "live", "widthS")),
        }
        for e in evs
        if e.tag in ("OFFLINE_TRANSIT_COARSE", "OFFLINE_TRANSIT_CHECKPOINT")
    ]
    return {
        "straight_line_records": straight,
        "station_checks": collapse_runs(shadow, ("dec", "why", "rdec", "rwhy")),
        "offline_transit": collapse_runs(coarse, ("tag", "hyps", "in", "out")),
    }


def safety_net(evs: Sequence[Ev]) -> list[dict]:
    rows = [
        {
            "time": fmt(e.ts),
            "alarm": short_alarm(e.alarm_id),
            **pick(
                e,
                (
                    "lm",
                    "tb",
                    "bh",
                    "motionState",
                    "vehicleAccumMs",
                    "blackoutMs",
                    "fixCount",
                    "lastFixDist",
                    "minDist",
                    "closed",
                    "effRad",
                    "commitRad",
                ),
            ),
        }
        for e in evs
        if e.tag == "SAFETY_NET_STATUS"
    ]
    return collapse_runs(rows, ("alarm", "lm", "tb", "bh"))


def ml_shadow(evs: Sequence[Ev]) -> dict:
    preds, gates = [], Counter()
    for e in evs:
        if e.tag != "ML_SHADOW":
            continue
        if e.get("mlEtaS") is not None:
            eta = e.num("mlEtaS") or 0.0
            preds.append(
                {
                    "time": fmt(e.ts),
                    "mlEtaS": eta,
                    "predicted_crossing": fmt(e.ts + timedelta(seconds=eta)),
                    **pick(
                        e, ("staleMs", "radiusM", "lastDistM", "support", "gate", "nObs", "model")
                    ),
                }
            )
        else:
            bare = e.fields.get("_bare") or []
            gates[(bare[-1] if bare else e.get("gate")) or "unknown"] += 1
    return {
        "predictions": preds,
        "gated_passes": dict(gates),
        "authority": "shadow only: R4 never decides a ring",
    }


def motion(evs: Sequence[Ev]) -> dict:
    segments = []
    for e in evs:
        if e.tag != "BLACKOUT_MOTION_STATE":
            continue
        to_state = e.get("to")
        if segments and segments[-1]["state"] == to_state:
            continue
        if segments:
            segments[-1]["to"] = fmt(e.ts)
        segments.append({"state": to_state, "from": fmt(e.ts), "to": None})
    resets = [
        {
            "time": fmt(e.ts),
            **pick(
                e,
                (
                    "reason",
                    "fromState",
                    "toState",
                    "accumBeforeMs",
                    "accumAfterMs",
                    "anchorChanged",
                ),
            ),
        }
        for e in evs
        if e.tag == "BLIND_MOTION_RESET"
    ]
    monitor = [
        {"time": fmt(e.ts), **pick(e, ("on", "reason"))}
        for e in evs
        if e.tag == "BLACKOUT_MOTION_MONITOR"
    ]
    return {"segments": segments, "resets": resets, "monitor": monitor}


def screen_events(evs: Sequence[Ev]) -> list[dict]:
    """Moments the rider woke the phone. Useful for lining up a stopwatch, never as a cause."""
    out = []
    prev_screen = None
    for e in evs:
        if e.tag == "APP_FOREGROUND_REFRESH_REQUESTED" and e.get("screen") == "ON":
            out.append({"time": fmt(e.ts), "what": "app opened (foreground refresh)"})
        elif e.tag == "DIAG2" and e.subtag == "FIX":
            screen = e.get("screen")
            if screen == "ON" and prev_screen == "OFF":
                out.append({"time": fmt(e.ts), "what": "screen on at a fix"})
            prev_screen = screen
    return out


def builds(evs: Sequence[Ev]) -> list[dict]:
    return [
        {
            "time": fmt(e.ts),
            "gitSha": e.get("gitSha"),
            "ver": e.get("ver"),
            "alarm": short_alarm(e.get("alarm")),
        }
        for e in evs
        if e.tag == "TEST_SESSION_START"
    ]
