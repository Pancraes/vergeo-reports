# Agentic incident triage

`app/agents/` adds two Claude agents on top of the report store. They are a development tool for
Vergeo field rides, not a product feature.

- **Triage agent.** Input: one problem report and the rider's lap notes. Output: a validated
  `IncidentAnalysis` covering what rang and why, the blackout, route state, the ML shadow, a verdict
  and the evidence behind it. It cannot edit code.
- **Fix agent.** Runs only when triage reports a log-supported defect. It works in an isolated git
  worktree of the Vergeo repo. The required order is a failing replay test first, then the
  narrowest fix. A deterministic validator decides whether the candidate goes to a human. It never
  merges, pushes or installs.

Review and validation is code, not a third agent. Its checks are mechanical: did the regression
fail before the fix, does the full suite pass, are the guard suites green. Code checks that
reliably; a model would only add an opinion.

## Why this exists

A Vergeo ride report is 0.5-2 MB of `TAG|key=value` lines. Triaging one by hand means several
steps:
- find the ride;
- line the rider's stopwatch up with the phone clock;
- find the ring and the line that caused it;
- rebuild the location timeline and blackouts;
- read the route records, safety-net passes and R4 shadow;
- compare against past rides in `FIELD_CORPUS.md`.

The verdict vocabulary has to come from those past rides. "Late" after a complete underground
blackout is the agreed product boundary, not a bug. An on-time ring can still be an accident of
cancelling errors.

## Architecture

```text
report.txt + rider notes
   |
   v
ingest_report()  (the same function POST /reports uses)  ->  PostgreSQL
   |
   v
PostgresSource   (reads back through services.queries.event_filter_clauses, as GET /events does)
   |
   v
ToolBox  -- 14 typed, read-only tools --  sanitize() + AuditLog  -->  Claude Agent SDK
   |                                                                     (in-process MCP server,
   |                                                                      built-in tools disabled,
   |                                                                      JSON-schema output)
   v
IncidentAnalysis (Pydantic)  ->  rules.check() against facts computed from the log
   |                               (one repair round; leftover violations reported, not hidden)
   v
result.json + audit.jsonl under local_data/agent_runs/<run>/
   |
   |  needs_fix_investigation = true
   v
fix.py:  git worktree add -b fix-agent/<id>  ->  fix agent (policy-gated tools)
         ->  validate_candidate()  ->  REVIEW.md + candidate.diff  ->  human
```

`--offline` swaps PostgresSource for ParsedSource, which keeps the same parser output in memory.
Use it for unit tests and for runs without a database. Cross-report search is unavailable there.

| Module | Role |
|---|---|
| `schemas.py` | `IncidentAnalysis`, verdict / trigger-path / failure-class enums, `RunMetrics` |
| `source.py` | `PostgresSource` / `ParsedSource`: one small event interface |
| `extract.py` | Deterministic log readings (rings, sessions, fixes, gaps, decisions, route, safety net, ML, motion) |
| `laps.py` | Rider notes to timing facts, then alignment with the phone clock |
| `tools.py` | The agent's tools, call counting, size caps, the SDK MCP server |
| `privacy.py` | `sanitize()`, `scrub_text()`, `AuditLog` |
| `rules.py` | Invariants the answer must satisfy against log facts |
| `corpus.py` | FIELD_CORPUS search, with the date cutoff used for blind evaluation |
| `triage.py` | Agent loop, repair round, CLI |
| `evaluation.py` | Blind historical evaluation and scoring |
| `fix.py` | Worktree, tool policy, fix agent, validation gate, review artifact |

## Tools

| Tool | Answers |
|---|---|
| `get_report_overview` | Build, report window, every alarm session (armed, rings with trigger path, end), default focus |
| `get_location_fixes` | Fixes with provider/source/accuracy/distance; gaps; the longest satellite-only gap |
| `get_rings` | Each `ESCALATION_START` with the `TRIGGER_ALLOWED` that caused it |
| `get_alarm_decisions` | Allowed/held/blocked edges, owners, backstop and blind-hop lifecycles (repeats collapsed) |
| `get_route_state` | Route records (fold, spread, provenance), station topology and directed checks |
| `get_safety_net_passes` | lm / tb / bh reasons per pass, collapsed into runs |
| `get_ml_predictions` | R4 shadow predictions and gated passes (shadow only) |
| `get_motion` | Still/walking/vehicle segments, motion resets, screen-on moments |
| `query_report_events` | Escape hatch: raw rows for named tags with `key:value` filters (capped) |
| `query_all_reports` | One tag across every stored report (database runs) |
| `search_historical_incidents`, `read_field_corpus` | Past rides, hidden on or after the case date during evaluation |
| `parse_user_laps`, `align_laps_to_phone_time` | Notes to constraints; phone-clock offset with method and uncertainty |

**Why the tools are narrow:**
- The model never gets a shell, the filesystem, the raw 2 MB file or a database connection.
- Each tool returns the rows one forensic question needs. Replies are capped at 48 KB, and long
  series are downsampled so both ends survive.
- Errors come back as JSON, so the model can correct itself and nothing crashes the run.
- Every call is counted. That makes "N tool calls, M event rows, B bytes sent" measurable.

## Rider lap notes

`laps.parse_laps` turns phrasing like "At exactly 3:05 I started laps. 47 seconds later the train
started moving. 33 minutes and 10 seconds after that, I got off at Elm Park" into facts with
roles (start, train_moving, station, doors, ring, exit) and offsets.

It does not pretend:
- **"+21:27" in a list is ambiguous.** It could be since the first lap or since the previous one.
  The reading used is stored, and the fact is flagged.
- **Stopwatch time is not phone time.** `align()` estimates the phone-minus-rider offset from
  logged anchors: a ring lap against `ESCALATION_START` (±10 s), or a train-moving lap against
  the first sustained vehicle motion (±15 s). It reports the method and uncertainty. With no
  anchor it says so and uses ±60 s.
- **Times are resolved, not guessed.** "3:05" is placed a.m. or p.m. nearest the ride. "N
  minutes ago" is placed from the note's own timestamp.

## Rules checked against the log

`rules.check()` compares the answer with facts computed without the model:
- `trigger_path` must equal what the ringing `TRIGGER_ALLOWED` line says;
- `alarm_time` must be the logged ring;
- `blackout_duration_s` must match some location gap in the window (within max(120 s, 10%)),
  counting the open gap that ends at the ring; a session can hold idle gaps before the ride, so
  the claim need not be the longest one;
- early / late must agree with the sign of `early_late_seconds`;
- a success needs a ring;
- `late_due_to_complete_blackout` needs a real blackout, and is not a defect without a hypothesis;
- `needs_fix_investigation` needs a written defect hypothesis.

The model gets one repair round. Violations that remain are reported next to the analysis.

## Privacy boundary

The app sanitizes reports on the device. The agent layer does not rely on that:
- **Forbidden keys.** `sanitize()` runs on every tool reply. It drops forbidden keys at any depth
  (positions, `target`/destination, names/labels, SSID/BSSID/MAC, tokens).
- **Ids and positions.** Raw alarm UUIDs become `alarm-xxxxxx`, and anything shaped like a
  latitude/longitude pair is redacted.
- **Old builds.** Builds from August logged the destination's name as `target=<place>`. That value
  never reaches the model; a test pins it. A first version redacted only up to the next space and
  leaked part of a multi-word name; the test now pins the full value.
- **The model sees only tool output.** Built-in tools are disabled. `setting_sources=[]` loads no
  CLAUDE.md or settings, and the system prompt asks for no personal details.
- **Audit.** `audit.jsonl` records every prompt and tool reply, byte for byte as sent.
- **What remains.** This removes identifiers, not the travel timeline. Times and
  destination-relative distances still describe one person's ride. So runs, evaluation cases and
  reports stay in `local_data/` (gitignored), and only the developer's own reports are analysed.

## Evaluation

`python -m app.agents.evaluation --cases local_data/eval/cases.json`

- **Cases.** Twenty historical rides, each a report file, the ride window and the rider's notes as
  given. The expected answer comes from the earlier manual analysis (FIELD_CORPUS sections and
  incident notes), and every case cites its source. Where the human record accepts two readings
  (for example "the final ring was structural, avoiding the early ring was accidental"), both are
  listed. The cases stay local because they name stations and times.
  `examples/eval_case_example.json` shows the format.
- **Blind.** While a case runs, the corpus tools show only sections dated before that ride. The
  agent cannot read the human answer.
- **Scored:**
  - verdict;
  - trigger path;
  - early / late / on time / no ring;
  - blackout reported (only where labelled);
  - failure class (defect vs known limit vs none);
  - doors-time error and early/late error, where the rider lapped the doors.

  Failed runs count as wrong; nothing is dropped.
- **Measured per run:** wall time, tool calls by name, model turns, tokens and cost as reported by
  the SDK, event rows and bytes sent.

Results: see "Measured results" below.

## Measured results

One run of each, on the developer's own rides. Cases and reports are private; only aggregates
are published.

**Triage, blind, 20 historical rides (2026-11-18):** all 20 completed.

| Field | Correct |
|---|---|
| verdict | 16/20 |
| trigger path | 20/20 |
| ride direction | 18/20 |
| blackout reported (where labelled) | 16/17 |
| failure class | 16/20 |

- Doors-time error: median 5.5 s, max 53 s (n=6 rides with a doors lap).
- Per ride: median 74 s wall time and 13 tool calls. Total cost $10.94 as reported by the SDK
  (median $0.44).
- All four verdict misses answered `insufficient_evidence` on rides without a doors lap.
- Fix investigation was requested on 6 rides, all real past defects. It was requested on none of
  the 6 rides that were late only because of a zero-signal blackout.

**Fix agent, blind backtest, 1 defect (2026-11-19):** an early ARRIVAL ring, run from the commit
before its human fix, in an isolated history.
- **Run:** 47 turns, 599 s, $2.11, 0 denied calls.
- **Validator:** accepted. The regression failed on an assertion at the base and passed with the
  fix. The full suite went from 1,055 to 1,059 tests with 0 failures. All guard suites passed, and
  22/22 historical replays passed.
- **Human review against the human fix:**
  - all six in-circle fixes of the incident are held;
  - two genuine off-network arrivals from other rides still ring;
  - a 125-point sweep near the destination gets no new hold.
- **Difference:** the candidate is narrower than the human fix. It does not hold a separate older
  false ring that the human fix also covers.
- **Status:** not merged.

n=1 for the fix agent: this shows the workflow works end to end, not a success rate. No manual
baseline has been measured, so no time saving is claimed.

## Fix agent safety

`python -m app.agents.fix --analysis <run>/result.json [--base <commit>] [--backtest]`

1. **Worktree.** `git worktree add -b fix-agent/<id>` at the base, beside the main checkout.
   `local.properties` is copied, and `node_modules` is junctioned read-only for the Capacitor
   modules.
   - **`--backtest` uses a separate repository, not a worktree.** A worktree shares refs and
     objects with the main checkout, so `git log --all` shows the later human fix. The first
     backtest found it there and closed the case as a duplicate without writing anything. A
     backtest now runs `git init` and fetches only the base commit and its ancestors, with no
     remote, so later commits are unreachable (tested).
   - **Discarding.** Remove the `node_modules` junction first (`cmd /c rmdir <dir>\node_modules`
     on Windows), then the directory or `git worktree remove`. Deleting recursively through a
     junction reaches the main checkout's modules.
2. **Policy.** `check_tool_call()` gates every tool call. It is wired as both the SDK permission
   callback and a PreToolUse hook, and nothing is pre-approved. It denies:
   - git push / merge / rebase / pull / reset --hard / checkout of a shared branch / branch
     deletion / worktree changes;
   - adb;
   - gradle install / assemble / bundle / publish / connected tasks;
   - network tools and package installs;
   - recursive deletes outside the worktree and `cd` out of it;
   - file paths outside the worktree.

   The workflow's own subprocesses go through `_git()` (read-only subcommands, `worktree add`,
   detached checkouts) and `_gradle()` (unit-test tasks only). A test scans `fix.py` to keep it
   that way.
3. **Required order.** Commit 1 is a regression/replay test alone. Commits 2+ are the fix.
4. **`validate_candidate()`.** Rejects the candidate unless all of these hold:
   - commit 1 touches only test sources;
   - its new tests **fail on an assertion** at commit 1 (a compile failure does not count);
   - they pass at HEAD;
   - the complete JVM suite has no new failures against the base commit and runs no fewer tests;
   - `GoldenReliabilityCorpusTest`, `MlShadowCorpusParityTest`, `MlShadowDecisionEquivalenceTest`,
     `MlShadowIsolationTest` and `MlResearchCaptureTest` pass (ML on/off parity and isolation);
   - no `*ReplayTest` that passed at the base fails.
5. **`REVIEW.md`.** Contains the incident, causal chain, reproduction, regression results, suite
   counts, guard and replay status, the diff stat, the agent's own report, denied calls, and
   "HUMAN APPROVAL REQUIRED".

Green tests are necessary, not sufficient. The validator proves the candidate reproduces the
defect and breaks nothing the suite knows about. A human still judges whether the fix is right for
the field evidence, then merges and installs, or discards. There is no auto-merge path to switch
on.

## Running it

```bash
pip install -r requirements-dev.txt     # includes claude-agent-sdk
# the SDK drives the local Claude Code CLI; log in to it once
cp .env.example .env                    # DATABASE_URL etc.
alembic upgrade head
python -m app.agents.triage --report path/to/report.txt \
    --notes "lap started 15:05, train moved 47 s later, doors 33m10s after that"
python -m app.agents.triage --offline --report path/to/report.txt   # no database
```

The unit tests mock the model (`tests/agents/`). Only `evaluation.py` and the CLIs call the real
model, and only when run explicitly.
