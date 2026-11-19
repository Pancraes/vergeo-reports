"""A synthetic field ride in the exact DurableLog shapes the agent tools read.

Made-up values, real tag and key names: arm above ground, surface fixes, a 38 min zero-fix
blackout with held safety-net passes and R4 shadow lines, then a held first surfacing fix and an
ARRIVAL ring. One old-format line carries target=<a place name> the way August builds logged it,
so privacy tests can prove it never reaches the model.
"""

from __future__ import annotations

from tests.factory import build_report

ALARM = "a1b2c3d4-0000-4000-8000-000000000001"
OTHER = "f0e1d2c3-0000-4000-8000-000000000002"
PLACE = "Example Park Trail"


def _fix(ts: str, er: int, prov: str, acc: float, dist: int, src: str, screen: str = "OFF") -> str:
    return (
        f"2026-01-10 {ts}|er={er}|DIAG2|FIX|t=1|prov={prov}|acc={acc}|spd=1.0|screen={screen}"
        f"|app=BG|dist={dist}|radius=440|mode=ECO|src={src}|gnssUsed=0"
    )


RIDE_LINES = [
    f"2026-01-10 09:00:00.000|er=1000|TIME_BACKSTOP_CANDIDATE|ID={OTHER}|target={PLACE}|tier=weak",
    f"2026-01-10 09:55:47.226|er=2000|ALARM_UPSERT|ID={ALARM}|type=ARRIVAL|mode=auto"
    "|timing=exact|active=true|baseRad=500",
    f"2026-01-10 09:55:47.569|er=2343|ALARM_ENABLED|ID={ALARM}|seed=true|fresh=false",
    f"2026-01-10 09:55:47.600|er=2374|TEST_SESSION_START|alarm={ALARM}|ver=2.0.0|code=45"
    "|gitSha=c567346",
    _fix("09:56:48.819", 3000, "gps", 7.5, 9529, "GNSS", "ON"),
    "2026-01-10 09:59:53.853|er=4000|STATION_STRAIGHT_LINE|known=true|why=entry_station|routes=2"
    "|receding=2|foldExcessM=3959|spreadM=2680|straightM=9446|acc=27",
    _fix("10:00:22.803", 5000, "gps", 13.9, 9325, "GNSS"),
    _fix("10:01:54.806", 6000, "gps", 35.4, 9228, "GNSS"),
    "2026-01-10 10:02:55.101|er=7000|ML_SHADOW|alarmHash=abc12|staleMs=60194|radiusM=590"
    "|support=TRAINED_RANGE|lastDistM=9228|nObs=11|mlEtaS=1125.4|gate=ok",
    f"2026-01-10 10:03:25.063|er=8000|SAFETY_NET_STATUS|ID={ALARM}|lm=insufficient_progress"
    "|tb=insufficient_progress_post_reset|bh=not_evaluated|motionState=walking|blackoutMs=90188",
    "2026-01-10 10:05:45.337|er=9000|BLACKOUT_MOTION_STATE|from=walking|to=vehicle|stdDev=0.9",
    f"2026-01-10 10:09:55.549|er=10000|SAFETY_NET_STATUS|ID={ALARM}|lm=insufficient_progress"
    "|tb=insufficient_progress_post_reset|bh=not_evaluated|motionState=vehicle|blackoutMs=480709",
    "2026-01-10 10:14:55.810|er=11000|ML_SHADOW|alarmHash=abc12|nObs=-1|stale_or_empty_buffer",
    "2026-01-10 10:38:40.000|er=12000|BLACKOUT_MOTION_STATE|from=vehicle|to=walking|stdDev=1.9",
    f"2026-01-10 10:39:56.862|er=13000|SAFETY_NET_STATUS|ID={ALARM}|lm=insufficient_progress"
    "|tb=insufficient_progress_post_reset|bh=not_evaluated|motionState=walking|blackoutMs=2282026",
    _fix("10:40:21.346", 14000, "gps", 28.9, 58, "GNSS"),
    f"2026-01-10 10:40:21.417|er=14100|TRIGGER_HELD|ID={ALARM}|reason=STATION_ROUTE_AMBIGUOUS"
    "|dist=58|rad=440|acc=28|prov=gps|dec=ELIGIBLE|why=within_radius_by_network|netMin=65"
    "|rdec=UNKNOWN|rwhy=direction_unknown|rMin=65|rMax=1509",
    "2026-01-10 10:40:21.418|er=14101|STATION_SHADOW|phase=live|dec=ELIGIBLE"
    "|why=within_radius_by_network|netMin=65|netMax=88|dTrack=26|dist=59|rad=590|acc=29"
    "|rdec=UNKNOWN|rwhy=direction_unknown|rMin=65|rMax=1509",
    _fix("10:40:26.794", 15000, "gps", 22.5, 60, "GNSS"),
    f"2026-01-10 10:40:26.839|er=15045|TRIGGER_ALLOWED|ID={ALARM}|path=ARRIVAL|dist=60|rad=440"
    "|acc=22|prov=gps|canonRad=590",
    "2026-01-10 10:40:26.839|er=15046|STATION_SHADOW|phase=live|dec=UNKNOWN|why=off_network"
    "|dTrack=29|dist=60|rad=590|acc=23|rdec=UNKNOWN|rwhy=off_network|prodFire=true",
    f"2026-01-10 10:40:26.877|er=15083|ESCALATION_START|ID={ALARM}|ACC=22.5",
    f"2026-01-10 10:40:36.021|er=25000|JS_ALARM_STOPPED_EVENT_SENT|ID={ALARM}|idSource=caller",
]


def ride_report() -> bytes:
    return build_report(description="rang only after I got out", lines=RIDE_LINES)


def ride_source():
    from app.agents.source import ParsedSource
    from app.parser import parse_report

    return ParsedSource(parse_report(ride_report()), "test-report")
