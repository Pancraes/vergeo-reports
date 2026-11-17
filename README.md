# vergeo-reports

An ingest and query API for problem reports from **Vergeo**, my location-based alarm app for
Android. It replaces a manual workflow: until now, a field-test problem produced a 0.5-2 MB text
report sent by email, and diagnosing it meant grepping thousands of `TAG|key=value` lines by hand
and rebuilding the timeline in a Markdown table.

This service stores each report byte for byte, parses it into indexed events in PostgreSQL, and
answers the questions I was grepping for:

```text
GET /events?tag=TRIGGER_ALLOWED&field=path:ARRIVAL            every ARRIVAL ring, all rides
GET /events?field=path:ARRIVAL&build_sha=789bf23              ...on one build
GET /events?tag=TRIGGER_ALLOWED&field=reason:TIME_BACKSTOP    every time-backstop ring
GET /reports/{id}/events?tag=TRIGGER_ALLOWED&tag=AUDIO_STOP&from=2026-11-06T15:00:00
```

Status: core complete and tested locally. The 11 historical Vergeo reports (31,361 events) ingest
with zero parse warnings. Not deployed, and the Android app does not upload to it yet; reports
are loaded with `scripts/backfill.py` or `curl`.

## Architecture

```text
Vergeo report (.txt)  --multipart POST, X-API-Key-->  FastAPI (sync def routes, threadpool)
                                                         |
     app/routers/*  ->  app/auth.py (key check)  ->  app/services/ingest.py
                                                         |  1. parse (app/parser, pure Python)
                                                         |  2. one DB transaction
                                                         v
                                   PostgreSQL: reports (incl. raw bytes) + events (JSONB fields)
```

- One service, one database. The parser (`app/parser/`) imports nothing from FastAPI or the
  database, so it is unit-tested on its own and reused by `scripts/backfill.py`.
- Sync SQLAlchemy 2.x with psycopg 3. Alembic owns the schema.

```text
app/
  main.py            FastAPI app, router wiring, OpenAPI description
  config.py          settings from environment variables
  db.py              engine, session-per-request dependency
  models.py          Report, Event (ORM) + constraints and indexes
  schemas.py         Pydantic request/response models
  auth.py            ingest / admin API-key dependencies
  parser/            errors.py, events.py (one line), report.py (whole document)
  services/          ingest.py (transaction), triage.py (status workflow), queries.py (filters)
  routers/           health.py, reports.py, events.py
alembic/versions/    0001_initial_reports_and_events.py
scripts/backfill.py  load the Vergeo repo's incidents/ reports
tests/               pytest against real PostgreSQL
docs/                WALKTHROUGH.md
```

## Quick start (Docker)

```bash
cp .env.example .env        # optional: without it, compose uses dev-only defaults
docker compose up --build
```

- API: <http://localhost:8000>, interactive docs: <http://localhost:8000/docs>
- The api container runs `alembic upgrade head` and then starts uvicorn once Postgres passes its
  health check.
- Without a `.env`, the keys are `dev-ingest-key` / `dev-admin-key`. Those are public development
  values: set real ones before exposing the port anywhere.

Load the historical reports into the running stack (the Vergeo repo is a sibling folder):

```bash
for f in ../Vergeo/incidents/*/raw/problem_report*.txt; do
  curl -s -o /dev/null -w "%{http_code} $f\n" -H "X-API-Key: dev-ingest-key" \
       -F "file=@$f" http://localhost:8000/reports
done
```

## Local development (without Docker for the API)

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
# A Postgres you can reach, e.g.:
docker run -d --name vr-db -e POSTGRES_USER=vergeo -e POSTGRES_PASSWORD=vergeo \
           -e POSTGRES_DB=vergeo_test -p 5433:5432 postgres:16-alpine

export DATABASE_URL=postgresql+psycopg://vergeo:vergeo@localhost:5433/vergeo_test
export INGEST_API_KEY=dev-ingest-key ADMIN_API_KEY=dev-admin-key
alembic upgrade head
uvicorn app.main:app --reload

python -m scripts.backfill ../Vergeo/incidents
```

Tests and lint:

```bash
export TEST_DATABASE_URL=postgresql+psycopg://vergeo:vergeo@localhost:5433/vergeo_test
pytest -q          # drops and re-migrates that database; refuses a name without "test"
ruff check . && ruff format --check .
```

## Environment variables

| Variable | Required | Meaning |
|---|---|---|
| `DATABASE_URL` | yes | SQLAlchemy URL, `postgresql+psycopg://user:pass@host:port/db` |
| `INGEST_API_KEY` | yes | Accepted only on `POST /reports` |
| `ADMIN_API_KEY` | yes | Accepted on every other endpoint except `/health` |
| `MAX_UPLOAD_BYTES` | no | Upload cap, default 4194304 (4 MiB) |
| `TEST_DATABASE_URL` | tests | Database the test suite wipes and migrates |
| `POSTGRES_USER/PASSWORD/DB` | compose | Used by `docker-compose.yml` for the db container |

## API

All requests except `/health` send `X-API-Key`. Times in filters are **device-local without a UTC
offset** (`2026-11-06T18:10:00`); a timezone-qualified time is rejected with 422.

| Method | Path | Key | Purpose |
|---|---|---|---|
| GET | `/health` | none | 200 if a `SELECT 1` reaches Postgres, else 503 |
| POST | `/reports` | ingest | Upload one report (multipart `file`, optional `report_uuid`). 201 / 200 / 409 / 413 / 422 |
| GET | `/reports` | admin | List; filters `app_version`, `version_code`, `device`, `status`, `build_sha`, `from`, `to`; offset pagination |
| GET | `/reports/{id}` | admin | Metadata, ENV flags, parse warnings |
| GET | `/reports/{id}/raw` | admin | The original bytes, as uploaded |
| PATCH | `/reports/{id}` | admin | Triage: `status`, `fix_commits`, `resolution_note` |
| GET | `/reports/{id}/events` | admin | One report's events in line order; `tag`, `subtag`, `alarm_id`, `from`, `to`, `field`; cursor |
| GET | `/events` | admin | Cross-report search; adds `build_sha`, `report_id`, `device`, `app_version`, `version_code`; cursor |

### Examples

```bash
H='X-API-Key: dev-admin-key'

# Upload (the app would also send -F report_uuid=<uuid it generated>)
curl -H 'X-API-Key: dev-ingest-key' -F file=@problem_report.txt http://localhost:8000/reports

# Every ARRIVAL ring in every report, oldest first
curl -H "$H" 'http://localhost:8000/events?tag=TRIGGER_ALLOWED&field=path:ARRIVAL'

# Next page: pass back next_cursor from the previous response
curl -H "$H" 'http://localhost:8000/events?tag=POSITIONING_HEALTH&limit=1000&cursor=eyJ0cyI6...'

# One ride's triggers and audio stops after 15:00
curl -H "$H" 'http://localhost:8000/reports/<id>/events?tag=TRIGGER_ALLOWED&tag=AUDIO_STOP&from=2026-11-06T15:00:00'

# Reports that contain events from build 789bf23
curl -H "$H" 'http://localhost:8000/reports?build_sha=789bf23'

# Triage
curl -X PATCH -H "$H" -H 'Content-Type: application/json' \
     -d '{"status":"fixed","fix_commits":["1473d6d","b0522a3"]}' http://localhost:8000/reports/<id>
curl -X PATCH -H "$H" -H 'Content-Type: application/json' \
     -d '{"status":"closed","resolution_note":"GNSS platform limit, not an app bug"}' http://localhost:8000/reports/<id>

# The original file
curl -H "$H" -o report.txt http://localhost:8000/reports/<id>/raw
```

## How idempotency works

Phones retry. A report sent from a subway platform may be sent twice, so `POST /reports` must be
safe to repeat.

- Each report has a `report_uuid`. The app should generate one when the user composes a report
  and resend it on every retry. Today's reports have none, so when it is omitted the server
  derives it: `uuid5(fixed namespace, sha256(bytes))`. The same bytes always get the same UUID.
- `content_sha256` is the identity of the bytes.

| Situation | Result |
|---|---|
| `report_uuid` not seen before | **201 Created**, `Location: /reports/{id}` |
| same `report_uuid`, same sha256 | **200 OK**, the stored report (nothing written) |
| same `report_uuid`, different sha256 | **409 Conflict**, with `existing_report_id` |

409 rather than "overwrite" or "store a second copy": two different payloads claiming one identity
means the client has a bug, and silently picking one would lose evidence.

**Concurrent duplicates.** The lookup before the insert is only a fast path. Two simultaneous
uploads can both miss it; the `UNIQUE (report_uuid)` constraint then makes the second `INSERT`
fail, and the service rolls back, re-reads the winner's row and answers 200 or 409 as if the
request had arrived second. `tests/test_ingest.py` forces this race with two threads and a barrier.

The same bytes under two *different* client UUIDs are stored twice on purpose
(`content_sha256` is indexed, not unique), because nothing proves the client meant a retry.

## Transactions

`app/services/ingest.py`:

1. size check, sha256, **parse**: no database involved, so a malformed upload (422) never opens a
   transaction and no connection is held during the CPU-bound parse;
2. `SELECT` by `report_uuid`: SQLAlchemy begins the transaction here;
3. `INSERT` the report and flush (the unique constraint fires here);
4. one `executemany` `INSERT` for all events;
5. `COMMIT`.

Any exception in 3-5 rolls back everything: no report without its events. Tests inject a
mid-batch failure and assert both tables are empty afterwards.

## Parser

`app/parser/events.py` handles one line; `app/parser/report.py` handles the document. The format
was derived from `ProblemReport.java` and the 11 real reports, not from a spec:

```text
==== VERGEO PROBLEM REPORT ====
APP|ver=1.9.0|code=44|debug=false|device=motorola moto g(7) play|api=29
---- what the user reported ----
<free text, may span lines>
ENV|fine=true|...|network=false|wifi=false|cellular=false        (only if diagnostics included)
---- recent activity (sanitized, oldest first) ----
2026-11-01 13:40:13.132|er=275099884|POSITIONING_HEALTH|at=JS_QUERY|risk=HIGH|...
...
==== REPORT COMPLETE | diagnostics=true | lines=8514 | window_truncated=false ====
```

Rules for an event line `<ts>|er=<n>|<TAG>|<tokens>`:

- tokens split on `|`; `key=value` splits on the **first** `=` (values may contain `=`);
- empty values are kept (`reason=` gives `""`);
- a **repeated key becomes a list** in order. Real reason: `TRIGGER_ALLOWED` for a time backstop
  logs `tier=weak` and later `tier=time_backstop`; a dict would keep only the second;
- a bare token right after the tag is the **subtag** (`DIAG2|FIX|...`);
- any other bare token goes under the reserved key `_bare`. Real reason: the app logs
  `"AUDIO_STOP|source=" + source` and `source` can contain a pipe (`STOP_ALL|LOCKSCREEN_HOLD`);
- `parse_event_line` returns `None` for lines that are intentionally not events (`APP|`,
  `ENV|`, `====` markers) and raises `MalformedEventLine` for a line that starts with a year but
  has an invalid timestamp, `er`, or tag.

Document-level rules:

- A missing or invalid line 1 header, `APP|` line, description header or diagnostics header is a
  `ReportFormatError`, and the upload gets a 422.
- A malformed event line becomes a **parse warning** (`line_no`, `reason`) and parsing
  continues. One corrupt line does not discard an 8,000-line report. Warnings are counted in
  full; the first 200 are stored.
- A missing `REPORT COMPLETE` trailer means the report was cut off. Everything before the cut is
  kept and `trailer_present=false`. A trailer `lines=` count that disagrees with the parsed
  section is a warning.
- `==== VERGEO SESSION START <ts> ====` (a process start) becomes a synthetic `SESSION_START`
  event, and the `APP|`/`ENV|` lines DurableLog writes right after it become that event's fields.
- **build_sha.** The header has no git SHA, and one report can span several builds (the 11-06
  report has three). `gitSha=` appears only on `TEST_SESSION_START`. Every later event inherits
  it until the next `SESSION_START`, which clears it, because a restart may be a reinstall.
  Unknown stays `NULL`; in the historical data 12,046 events have an attributed build and 19,315
  do not.
- `alarm_id` comes from `ID=` or `alarm=` (both spellings exist in the logs).
- `line_no` is the physical line in the raw file, so `raw` + `line_no` reconstructs any event.

## Why raw reports are kept

The raw bytes are the source of truth, and the events are a derived index. The parser will
change: during development the first version turned the `APP|`/`ENV|` lines after each
`SESSION START` into warnings until the real reports showed what they were. Every report
records `parser_version`, so a future parser can re-derive events from `raw` for old reports
instead of living with what version 1 understood. `GET /reports/{id}/raw` also returns the file
exactly as uploaded, for the cases where reading it is faster than querying.

## Schema

`reports`: identity (`id` server UUID, `report_uuid` UNIQUE, `content_sha256`), `raw` (bytea,
deferred-loaded), header columns (`app_version`, `version_code`, `debug_build`, `device_model`,
`android_api`), `env` JSONB, `description`, trailer metadata, `event_count`, `parse_warning_count`,
`parse_warnings` JSONB, `window_start/end`, `status`, `fix_commits text[]`, `resolution_note`,
`received_at`, `updated_at`.

`events`: `id` (identity), `report_id` FK, `line_no`, `device_ts`, `er`, `tag`, `subtag`,
`alarm_id`, `build_sha`, `fields` JSONB.

**Why a server `id` and a separate `report_uuid`.** The primary key is ours and never changes.
`report_uuid` is a client-supplied idempotency key with a different job; keeping them apart means
a client bug or a key-derivation change cannot ripple into every events foreign key.

**Timestamps.** `device_ts` and the window columns are `timestamp without time zone`, because the
log has no offset and storing a fake UTC would be a lie. `received_at` is server time
(`timestamptz`). `line_no` is the authoritative order within a report: device wall time skews
(fixes carry negative `wallAge`) and repeats (the 11-06 report crosses the Nov 1 DST fall-back).

**Status workflow** (`app/services/triage.py`):
`new -> triaged | fixed | closed`, `triaged -> fixed | closed`, `fixed -> triaged` (regression),
`closed -> triaged` (new evidence). `fixed` requires at least one commit, because some Vergeo
fixes took several (the 11-12 ride took three). `closed` exists because some incidents end with
no code change ("not a bug", "GNSS platform limit") and needs a `resolution_note`. Invalid moves
are 409; an invalid resulting state is 422. The two invariants are also `CHECK` constraints, so a
manual `UPDATE` cannot break them.

### Indexes and why each exists

| Index | Serves |
|---|---|
| `reports.report_uuid` UNIQUE | idempotency and the concurrent-duplicate guarantee |
| `reports.content_sha256` | "have these bytes been seen" lookups |
| `events (report_id, line_no)` UNIQUE | one event per raw line; per-report timeline + its cursor; FK cascade |
| `events (tag, device_ts, id)` | cross-report search by tag in keyset order |
| `events (build_sha) WHERE build_sha IS NOT NULL` | per-build search; partial because most rows are NULL |
| `events (alarm_id) WHERE alarm_id IS NOT NULL` | one alarm across rides |
| `events USING gin (fields jsonb_path_ops)` | `fields @> '{"path":"ARRIVAL"}'` |

Not indexed on purpose: `env`, `device_model`, `app_version`. There are tens of reports, so a
scan is instant and an index would only cost writes. At 31k events most queries are fast even
without the event indexes; they matter at around 100x this size, and that should be confirmed
with `EXPLAIN ANALYZE` at that point, not assumed.

### JSONB versus columns

A value gets a real column when it is on nearly every row, filtered or sorted on often, and
stably typed: timestamp, `er`, tag, subtag, alarm, build. Everything else goes in `fields`. The
historical reports contain 125 tags with different keys, and the app adds log lines almost
weekly, so a column per key would need a migration per log line.

Costs I accept: JSONB is schema-less, so a typo in a filter key matches nothing instead of
erroring; values are strings (`"8.732"`), so numeric comparisons need a cast in SQL and the API
offers only exact matches; and GIN indexes are slower to update than B-trees (fine for
batch-written, read-mostly data).

### Field filter syntax

`field=<key>:<value>`, repeatable, ANDed. The key must match `[A-Za-z_][A-Za-z0-9_.]*`; the value is
everything after the first `:` and is an exact string match. Each filter compiles to a JSONB
containment test. Postgres does not consider `{"tier": ["a","b"]}` to contain `{"tier": "a"}`, so
each filter matches `{"k": "v"}` OR `{"k": ["v"]}`. There is no ranges, OR, or negation by design:
anything more belongs in SQL.

## Pagination

- `/reports`: **offset** (`limit`, `offset`, `total`). There are few reports, a person pages
  through them, and a total count is useful.
- `/events` and `/reports/{id}/events`: **keyset cursor**. `next_cursor` encodes the sort key of
  the last row (`(device_ts, id)` globally, `line_no` per report), and the next page is "rows after
  that key". Unlike `OFFSET 30000`, it does not rescan skipped rows, and it does not skip or repeat
  rows when new reports arrive mid-scan. The cost is no "jump to page 7" and no total count.
  `id` breaks ties because overlapping reports contain identical timestamps.

## Auth, and its limits

Two keys in `X-API-Key`, compared with `secrets.compare_digest`. The ingest key is accepted only
on `POST /reports`; the admin key on everything else.

An ingest key shipped inside an APK is **not a secret**: `apktool` or `strings` recovers it in
minutes. It stops drive-by junk, nothing more. It does not identify a user or a device, and anyone
who extracts it can upload. Before the app ships with it, the realistic next steps are rate
limiting per IP and per key, and Play Integrity attestation if abuse appears. Both are out of scope
here. The admin key never leaves the developer's machine. No secrets are in git: `.env` is
ignored, and compose falls back to labelled development values.

## Known limitations

- **Overlapping reports duplicate events.** Reports are rolling windows of the same on-device log,
  so a line can be in several reports. The backfill measured 2,049 lines shared between the two
  10-07 reports and 1,704 between 10-08 and 10-07, and one 10-07 ARRIVAL appears three times in
  `/events`. Results carry `report_id` and `line_no`, so duplicates are distinguishable, but they
  are not merged. Merging needs a stable line identity across reports (device + boot + `er` +
  tag + fields, plus a decision on what a re-parse does); that is deliberately left for later.
- `build_sha` is unknown for most events (19,315 of 31,361), because the app writes `gitSha` only
  on `TEST_SESSION_START`. Fixing that is a one-line app change: add `gitSha` to the `APP|` line it
  writes after `SESSION START`.
- Field filters are exact string matches only.
- Raw reports live in Postgres. Fine at megabytes, wrong at gigabytes (see below).
- No rate limiting, no deletion endpoint, no per-user auth. Not deployed; the app does not upload
  yet.
- Device times assume one device-local timezone; there is no per-report offset.

## Scaling path (not built)

Rough order, driven by what would actually break:

1. **Raw storage**: move `raw` to object storage (S3/GCS), keep the key and sha256 in `reports`.
   The table stays small and backups stay fast.
2. **Ingest work**: accept, store raw, return 202, and parse in a worker (a queue such as SQS/Redis
   plus a worker process). The parse is CPU-bound (0.16 s for 2 MB) and the insert is the slow part
   (about 1 s for 8.5k events); a worker absorbs bursts and makes reprocessing old reports with a
   new parser the same code path.
3. **Event volume**: use `COPY` instead of `executemany`; partition `events` by month on
   `device_ts`; review indexes with `EXPLAIN ANALYZE` under real query load.
4. **Read scaling**: a read replica for `/events` queries.
5. **API versioning**: a `/v1` prefix once there is a second client to keep compatible.
   Additive changes (new fields, new filters) are not breaking.

## Development notes

- `tests/` runs against real PostgreSQL, not SQLite: JSONB containment, GIN, CHECK constraints,
  `UNIQUE`-based race handling and `timestamp` semantics are what is being tested.
- `alembic check` (in CI and in `test_models_and_migrations_have_not_drifted`) fails if
  `models.py` changes without a migration.
- `docs/WALKTHROUGH.md` walks through the code paths.
