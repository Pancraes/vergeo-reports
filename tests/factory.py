"""Builds synthetic reports in the exact format ProblemReport.assemble() produces.

Synthetic, because real reports stay out of git. The event lines below copy the shapes of real
DurableLog lines (tags, key names, value styles), with made-up values.
"""

from __future__ import annotations

APP = "APP|ver=1.9.0|code=44|debug=false|device=motorola moto g(7) play|api=29"
ENV = (
    "ENV|fine=true|coarse=true|background=true|notif=n/a|battOptExempt=true|batterySaver=false"
    "|gpsProvider=true|networkProvider=true|network=false|wifi=false|cellular=false"
)
ALARM = "00000000-0000-4000-8000-000000000001"

DEFAULT_LINES = [
    "2026-11-06 17:40:00.100|er=721000000|POSITIONING_HEALTH|at=JS_QUERY|activeNetwork=false"
    "|risk=HIGH|reason=GPS_ONLY_NO_DATA_NO_WIFI",
    "==== VERGEO SESSION START 2026-11-06 17:44:38.001 ====",
    APP,
    ENV,
    f"2026-11-06 17:44:39.589|er=721766341|TEST_SESSION_START|alarm={ALARM}|ver=1.9.0|code=44"
    "|gitSha=c567346|at=1793403879589",
    "2026-11-06 17:45:12.000|er=721799000|DIAG2|FIX|t=1793403912000|prov=gps|acc=17.152|spd=12.58",
    f"2026-11-06 18:13:57.718|er=723500000|TRIGGER_ALLOWED|ID={ALARM}|path=ARRIVAL|dist=1982"
    "|rad=2090|ef=-",
    f"2026-11-06 18:14:03.881|er=723506000|BLIND_HOP_CANDIDATE|ID={ALARM}|tier=blind_motion"
    "|paceMps=7.4|tier=blind_motion|reason=",
    "2026-11-06 18:14:04.000|er=723507000|AUDIO_STOP|source=STOP_ALL|LOCKSCREEN_HOLD",
]


def build_report(
    *,
    description: str = "Rang late at the destination stop",
    lines: list[str] | None = None,
    app: str = APP,
    env: str | None = ENV,
    diagnostics: bool = True,
    trailer: bool = True,
) -> bytes:
    lines = DEFAULT_LINES if lines is None else lines
    out = ["==== VERGEO PROBLEM REPORT ====", app, "---- what the user reported ----", description]
    if not diagnostics:
        out += [
            "---- recent activity (sanitized, oldest first) ----",
            "(diagnostic information was not included in this report)",
        ]
        if trailer:
            out.append(
                "==== REPORT COMPLETE | diagnostics=false | lines=0 | window_truncated=false ===="
            )
        return ("\n".join(out) + "\n").encode()
    if env is not None:
        out.append(env)
    out.append("---- recent activity (sanitized, oldest first) ----")
    out += lines
    if trailer:
        out.append(
            f"==== REPORT COMPLETE | diagnostics=true | lines={len(lines)}"
            " | window_truncated=false ===="
        )
    return ("\n".join(out) + "\n").encode()
