"""Rider lap notes: natural language in, explicit timing constraints out, then lined up with the
phone clock.

Riders time a ride on a separate stopwatch and report it however they like:

    "At exactly 3:05 I started laps. 47 seconds later the train started moving.
     33 minutes 10 seconds later I got off at Elm Park."
    "lap started 15:05, train moved +47 s, Elm Park doors +33m10s"
    "start 16:34:20, ring +21:27 at Cedar, doors +39:54"

Two things this module refuses to pretend:

1. Stopwatch time is not phone time. The phone has run 29-45 s off the rider's clock on past rides.
   align() estimates the offset from logged events (the ring, the train starting to move) and
   reports how sure it is.
2. "+X" is ambiguous. "after that" / "later" means after the previous lap; a bare "+21:27" in a
   stopwatch list usually means since the first lap. The parser records which reading it used and
   flags the ambiguity instead of hiding it.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

ROLE_WORDS = (
    ("ring", ("ring", "rang", "trigger", "alarm went", "went off", "fired")),
    ("doors", ("doors", "door", "got off", "get off", "off the train")),
    ("station", ("reached", "got to", "arriv", "pulled into", "passed")),
    (
        "train_moving",
        ("moving", "moved", "departed", "depart", "started going", "train start", "boarded"),
    ),
    ("boarding", ("board",)),
    ("exit", ("exit", "above ground", "surface", "got out", "outside")),
    ("start", ("start", "fare gate", "gates", "went down", "turned it on", "turn it on", "lap")),
)

_CLOCK = re.compile(
    r"(?<![\d:+])(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\s*([ap])\.?m\.?)?(?![\d:])", re.I
)
_PLUS_COLON = re.compile(r"\+\s*(?:(\d+):)?(\d{1,2}):(\d{2})(?!\d)")
_DURATION = re.compile(
    r"(\+\s*)?(?:(\d+)\s*(?:hours?|hrs?|h)\b[\s,and]*)?"
    r"(?:(\d+)\s*(?:minutes?|mins?|m)\b[\s,and]*)?"
    r"(?:(\d+(?:\.\d+)?)\s*(?:seconds?|secs?|s)\b)?",
    re.I,
)
_COMPACT = re.compile(r"(\+\s*)?(\d+)m\s*(\d+)s\b", re.I)
_AFTER_PREVIOUS = re.compile(r"\b(later|after that|after this|afterwards|then)\b", re.I)
_AGO = re.compile(r"\bago\b", re.I)
_AFTER_START = re.compile(r"\b(from (the )?start|since (the )?start|after (the )?start)\b", re.I)


@dataclass
class LapFact:
    text: str
    role: str
    clock: str | None = None  # rider clock as written, "15:05" / "3:05"
    offset_s: float | None = None  # duration as written
    relative_to: str | None = None  # "previous" | "start" | None for an absolute time
    ambiguous: bool = False
    since_start_s: float | None = None  # resolved: seconds after the first fact


@dataclass
class LapParse:
    original: str
    facts: list[LapFact] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def role_of(text: str) -> str:
    low = text.lower()
    for role, words in ROLE_WORDS:
        if any(w in low for w in words):
            return role
    return "other"


def _clause_split(note: str) -> list[str]:
    # Sentences and comma/semicolon lists. Keep "3:05" intact (the colon is not a separator).
    parts = re.split(r"(?<=[.;!?])\s+|,\s*|\n+|\band then\b", note)
    return [p.strip(" .") for p in parts if p and p.strip(" .")]


def _duration_in(clause: str) -> tuple[float | None, bool]:
    """Seconds named in a clause and whether it was written with '+'."""
    m = _PLUS_COLON.search(clause)
    if m:
        h, mi, s = int(m.group(1) or 0), int(m.group(2)), int(m.group(3))
        return h * 3600 + mi * 60 + s, True
    m = _COMPACT.search(clause)
    if m:
        return int(m.group(2)) * 60 + int(m.group(3)), bool(m.group(1))
    # "4 minutes 40 later": a bare number after minutes is seconds.
    m = re.search(r"(\d+)\s*(?:minutes?|mins?)\s+(\d{1,2})\s+(later|after)", clause, re.I)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2)), False
    # "1:03:00 later" style: a colon duration followed by later/after.
    m = re.search(r"(\d+):(\d{2}):(\d{2})\s+(later|after)", clause, re.I)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)), False
    best = None
    for m in _DURATION.finditer(clause):
        if not any(m.group(i) for i in (2, 3, 4)):
            continue
        secs = int(m.group(2) or 0) * 3600 + int(m.group(3) or 0) * 60 + float(m.group(4) or 0)
        best = (secs, bool(m.group(1)))
        break
    return best if best else (None, False)


def _merge_orphans(clauses: list[str]) -> list[str]:
    """Rejoin "33 minutes after that, I got off at Elm Park": a clause with a time but no event
    words takes the next clause when that one has no time of its own."""
    out: list[str] = []
    i = 0
    while i < len(clauses):
        c = clauses[i]
        if (
            i + 1 < len(clauses)
            and role_of(c) == "other"
            and (_duration_in(c)[0] is not None or _CLOCK.search(c))
            and _duration_in(clauses[i + 1])[0] is None
            and not _CLOCK.search(clauses[i + 1])
        ):
            out.append(f"{c}, {clauses[i + 1]}")
            i += 2
            continue
        out.append(c)
        i += 1
    return out


def parse_laps(note: str) -> LapParse:
    out = LapParse(original=note)
    for clause in _merge_orphans(_clause_split(note)):
        duration, plus = _duration_in(clause)
        clock_m = None if (plus and _PLUS_COLON.search(clause)) else _CLOCK.search(clause)
        label = clause
        if clock_m and duration is None:
            hh, mm, ss, ap = clock_m.groups()
            clock = f"{int(hh):02d}:{mm}" + (f":{ss}" if ss else "") + (f" {ap}m" if ap else "")
            out.facts.append(LapFact(text=label, role=role_of(clause), clock=clock))
            continue
        if duration is None:
            continue
        if _AGO.search(clause):
            rel, amb = "now", False  # placed from the time the note was written
        elif _AFTER_START.search(clause):
            rel, amb = "start", False
        elif _AFTER_PREVIOUS.search(clause):
            rel, amb = "previous", False
        elif plus:
            # A bare "+mm:ss" list is usually cumulative stopwatch time; record the doubt.
            rel, amb = "start", True
        else:
            rel, amb = "previous", True
        out.facts.append(
            LapFact(
                text=label, role=role_of(clause), offset_s=duration, relative_to=rel, ambiguous=amb
            )
        )
    _resolve(out)
    return out


def _resolve(p: LapParse) -> None:
    if not p.facts:
        p.notes.append("no timing facts recognised")
        return
    first = p.facts[0]
    if first.relative_to == "now":
        first.since_start_s = 0.0
    elif first.clock is None and first.relative_to is not None:
        p.notes.append("the first fact is relative; there is no lap zero to measure from")
    else:
        first.since_start_s = 0.0 if first.clock is not None else None
    prev = first.since_start_s
    for f in p.facts[1:]:
        if f.clock is not None:
            f.since_start_s = None  # a second absolute time is placed by align()
            prev = None
            continue
        if f.relative_to == "start":
            f.since_start_s = f.offset_s
        elif prev is not None and f.offset_s is not None:
            f.since_start_s = prev + f.offset_s
        prev = f.since_start_s
    if any(f.ambiguous for f in p.facts):
        p.notes.append(
            "some offsets could be since the first lap or since the previous one; "
            "the reading used is recorded per fact"
        )


# --- alignment with the phone clock ------------------------------------------------------------


@dataclass
class Alignment:
    phone_minus_rider_s: float | None
    uncertainty_s: float | None
    method: str
    candidates: list[dict]
    facts: list[dict]
    notes: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


def resolve_clock(clock: str, around: datetime) -> datetime:
    """A rider clock like "3:05" or "15:05:00" on the day of `around`, choosing a.m./p.m. so the
    time lands nearest to it (riders write 12-hour times without saying which)."""
    parts = clock.split()
    hms = [int(x) for x in parts[0].split(":")]
    h, m, s = hms[0], hms[1], hms[2] if len(hms) > 2 else 0
    ampm = parts[1] if len(parts) > 1 else None
    if ampm == "pm" and h < 12:
        h += 12
    if ampm == "am" and h == 12:
        h = 0
    base = around.replace(hour=0, minute=0, second=0, microsecond=0)
    options = [base + timedelta(hours=h, minutes=m, seconds=s)]
    if ampm is None and h < 12:
        options.append(base + timedelta(hours=h + 12, minutes=m, seconds=s))
    return min(options, key=lambda t: abs((t - around).total_seconds()))


def align(
    parse: LapParse,
    *,
    around: datetime,
    ring_times: list[datetime],
    vehicle_starts: list[datetime],
    note_written_at: datetime | None = None,
    default_uncertainty_s: float = 60.0,
) -> Alignment:
    """Estimate phone_clock - rider_clock and place every fact on the phone clock.

    Anchors, strongest first:
    - a "ring" lap matched to the logged ESCALATION_START (the rider reacts within seconds);
    - a "train moving" lap matched to the first sustained vehicle motion after the start lap
      (BLACKOUT_MOTION_STATE windows are 10 s, so +/-10 s);
    - otherwise no offset: rider time is used as phone time with the stated default uncertainty.
    """
    notes: list[str] = list(parse.notes)
    start = next((f for f in parse.facts if f.clock is not None), None)
    start_rider = resolve_clock(start.clock, around) if start else None
    first = parse.facts[0] if parse.facts else None
    if start is None and first is not None and first.relative_to == "now":
        if note_written_at is not None and first.offset_s is not None:
            # "2 hours 51 minutes ago": the note's own timestamp is on the phone's day, but it
            # is the rider's clock that measured "ago", so it is still rider time.
            start, start_rider = first, note_written_at - timedelta(seconds=first.offset_s)
        else:
            notes.append("the note says 'ago' but no note time was given")
    if start is not None and start is not parse.facts[0]:
        notes.append("the absolute time is not on the first fact; offsets before it are unplaced")

    def rider_time(f: LapFact) -> datetime | None:
        if f.clock is not None:
            return resolve_clock(f.clock, around)
        if f is start:
            return start_rider
        if start_rider is not None and f.since_start_s is not None and start is parse.facts[0]:
            return start_rider + timedelta(seconds=f.since_start_s)
        return None

    candidates: list[dict] = []
    for f in parse.facts:
        rt = rider_time(f)
        if f.role == "ring" and ring_times:
            if rt is None:
                continue
            nearest = min(ring_times, key=lambda t: abs((t - rt).total_seconds()))
            off = (nearest - rt).total_seconds()
            if abs(off) <= 600:
                candidates.append(
                    {
                        "method": "ring_lap_vs_ESCALATION_START",
                        "offset_s": off,
                        "uncertainty_s": 10.0,
                        "fact": f.text,
                    }
                )
        if f.role == "train_moving" and vehicle_starts and rt is not None:
            near = [t for t in vehicle_starts if abs((t - rt).total_seconds()) <= 300]
            if near:
                best = min(near, key=lambda t: abs((t - rt).total_seconds()))
                candidates.append(
                    {
                        "method": "moving_lap_vs_vehicle_motion",
                        "offset_s": (best - rt).total_seconds(),
                        "uncertainty_s": 15.0,
                        "fact": f.text,
                    }
                )
    if candidates:
        best = min(candidates, key=lambda c: c["uncertainty_s"])
        offset, unc, method = best["offset_s"], best["uncertainty_s"], best["method"]
        if len(candidates) > 1:
            spread = max(c["offset_s"] for c in candidates) - min(c["offset_s"] for c in candidates)
            if spread > unc:
                notes.append(f"anchors disagree by {spread:.0f} s; using {method}")
                unc = max(unc, spread / 2)
    elif start_rider is not None:
        offset, unc, method = 0.0, default_uncertainty_s, "none: rider clock used as phone clock"
    else:
        offset, unc, method = None, None, "unplaceable: no absolute time and no anchor"

    placed = []
    for f in parse.facts:
        rt = rider_time(f)
        phone = rt + timedelta(seconds=offset) if (rt is not None and offset is not None) else None
        placed.append(
            {
                "what": f.text,
                "role": f.role,
                "rider_clock": rt.strftime("%H:%M:%S") if rt else None,
                "phone_clock": phone.strftime("%Y-%m-%d %H:%M:%S") if phone else None,
                "since_start_s": f.since_start_s,
                "relative_to": f.relative_to,
                "ambiguous": f.ambiguous,
            }
        )
    return Alignment(offset, unc, method, candidates, placed, notes)
