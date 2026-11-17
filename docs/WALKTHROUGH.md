# Walkthrough: how vergeo-reports works

Read this next to the code. Each section names the files and functions involved. The suggested
order is: request lifecycle → parser → transaction → models/migrations → queries → tests.

---

## 1. The request lifecycle of `POST /reports`

Follow one upload from the socket to the database.

1. **uvicorn** accepts the HTTP request and hands it to the ASGI app `app.main:app`.
2. **Routing.** `app/main.py` includes `routers/reports.py`, whose `APIRouter(prefix="/reports")`
   registers `upload_report` for `POST ""`, i.e. `POST /reports`.
3. **Dependencies run first**, before the function body:
   - `dependencies=[Depends(require_ingest_key)]` → `app/auth.py`. It reads `X-API-Key` through
     `APIKeyHeader` (which is also what puts the "Authorize" button on `/docs`), compares it with
     `secrets.compare_digest`, and raises `HTTPException(401)` on a mismatch. The handler never
     runs.
   - `db: Session = Depends(get_db)` → `app/db.py` opens a `Session` and *yields* it. Code after
     the `yield` (closing the session) runs once the response is done.
   - `settings: Settings = Depends(get_settings)` → `app/config.py`, cached with `lru_cache`.
4. **Parameter parsing.** `file: UploadFile = File(...)` and `report_uuid: UUID | None = Form()`
   tell FastAPI to parse a `multipart/form-data` body. A `report_uuid` that is not a UUID becomes
   a 422 automatically, before your code runs.
5. **Handler.** `upload_report` reads at most `max_upload_bytes + 1` bytes and calls
   `ingest_report(db, raw, report_uuid, max_bytes=...)`.
6. **Service** (`app/services/ingest.py`, section 3 below) returns `IngestResult(report, created)`
   or raises a domain exception.
7. **Mapping to HTTP.** The router turns domain exceptions into status codes:
   `ReportTooLarge` → 413, `ReportFormatError` → 422, `ReportConflict` → 409. The decorator's
   default is 201; for an idempotent retry the handler sets `response.status_code = 200`.
8. **Serialisation.** The handler returns an ORM `Report`. `response_model=ReportDetail` makes
   FastAPI validate it through Pydantic (`ConfigDict(from_attributes=True)` reads attributes off
   the object) and emit only the declared fields, so `raw` can never leak into JSON.
9. `get_db`'s `finally: session.close()` runs and returns the connection to the pool.

**Why this split:** the router knows HTTP, the service knows the business rules, the parser knows
the file format. Each can be tested without the others: `tests/test_ingest.py` calls the service
with no HTTP at all.

---

## 2. FastAPI routing and dependencies, in general

- `APIRouter(prefix=..., tags=[...])` groups endpoints. `tags` groups them on `/docs`.
- Query parameters are function arguments with defaults:
  `limit: int = Query(20, ge=1, le=100)` gives validation (422 outside 1-100) and docs.
  `alias="from"` exists because `from` is a Python keyword.
- `tag: list[str] | None = Query(None)` accepts a repeated parameter: `?tag=A&tag=B`.
- Path parameters (`report_id: uuid.UUID`) are validated by type, so `/reports/abc` gives 422,
  not a database error.
- Dependencies are the place for cross-cutting concerns (auth, the DB session). Tests can
  replace one: `test_health_503_when_database_unreachable` overrides `get_db` with a session
  that raises.

## 3. Pydantic validation (`app/schemas.py`)

- **Response models** (`ReportSummary`, `ReportDetail`, `EventOut`, `EventPage`) define the
  public contract and the OpenAPI schema. `ReportDetail` inherits from `ReportSummary` and adds
  fields.
- **Request model** `ReportPatch`:
  - `extra="forbid"` turns an unknown field into a 422 instead of silently ignoring it;
  - `CommitSha` is an `Annotated[str, StringConstraints(...)]`: strip, lower-case, then match
    7-40 hex characters. Pydantic checks the pattern *before* lower-casing, which is why the
    pattern accepts `A-F` (a bug found by a test);
  - `body.model_fields_set` tells "field omitted" apart from "field sent as null". That's how
    `resolution_note: null` means "clear it", while omitting it means "leave it".

## 4. The parser (`app/parser/`)

Pure Python with no FastAPI or SQLAlchemy imports. Read the files in this order.

**`errors.py`** defines two levels of failure:
- `ReportFormatError` means the whole report is unusable, and becomes a 422;
- `MalformedEventLine(line_no, reason)` is a problem with one line, and becomes a warning.

**`events.py` → `parse_event_line(line, line_no)`**
1. If the line doesn't start with a year (`^\d{4}-`), return **None**. It's intentionally not an
   event.
2. Split on `|`. Token 0 must parse as `%Y-%m-%d %H:%M:%S.%f`, token 1 must be `er=<int>`, and
   token 2 must match `[A-Z][A-Z0-9_]*`. If any of these fail, raise `MalformedEventLine`.
3. If the next token has no `=`, it is the `subtag` (`DIAG2|FIX`).
4. `parse_fields` handles the rest. `token.partition("=")` splits on the first `=`, so
   `a=b=c` → key `a`, value `b=c`. A repeated key turns into a list, and a bare token goes to
   `_bare`.

**`report.py` → `parse_report(raw: bytes)`**
1. Decode the bytes as UTF-8 with `errors="replace"`. If anything had to be replaced, add a
   warning.
2. Split on `\n` and strip any `\r`. The resulting index is the physical line number.
3. Check the fixed header in lines 1-3, and parse `APP|` with `parse_app_line`.
4. Find the diagnostics header. Everything between line 4 and that header is the description,
   except a final `ENV|` line, which is parsed by `parse_env_line`.
5. `_parse_diagnostics` walks the rest of the file:
   - **trailer** → parse it, compare its `lines=` with the number of lines counted, then stop;
   - **`SESSION START` marker** → emit a synthetic `SESSION_START` event, clear `build_sha`,
     and remember the event so the next `APP|`/`ENV|` lines fold into its fields;
   - **anything else** → `parse_event_line`:
     - a `MalformedEventLine` or an unexpected `None` becomes a warning;
     - a `TEST_SESSION_START` updates `build_sha`;
     - every event becomes `IndexedEvent(event, build_sha, alarm_id)`.
6. If the loop reaches the end without a trailer, the report was truncated. The events parsed so
   far are kept and a warning is added.

`ParsedReport.window_start/end` are the min and max of `device_ts`, not the first and last lines,
because wall time can go backwards.

Try it yourself:
```python
from app.parser import parse_report

r = parse_report(open("local_data/some_report.txt", "rb").read())
r.app, r.env, len(r.events), r.warnings[:3], r.events[0]
```

## 5. The SQLAlchemy session and transaction boundaries

- `app/db.py` creates one `engine`, a connection pool, at import. `SessionLocal()` makes a
  `Session`, which is a unit of work bound to one connection while it is in use.
- **Autobegin.** A session has no explicit `BEGIN`. The first statement starts a transaction,
  which lasts until `commit()` or `rollback()`.
- In `ingest_report`, the boundaries are:
  ```text
  parse_report(raw)                       # no transaction yet
  find_by_report_uuid(...)                # SELECT -> transaction begins
  session.add(report); session.flush()    # INSERT reports (still uncommitted)
  session.execute(insert(Event.__table__), rows)   # one executemany INSERT
  session.commit()                        # both tables become visible together
  ```
  `except IntegrityError` → `rollback()`, and the report row disappears too. Then it re-reads
  (the lost-race path). `except BaseException` → `rollback()` and re-raise.
- **`flush` versus `commit`.** `flush` sends the SQL inside the open transaction (so the report
  row exists for the events' foreign key). `commit` makes the work permanent.
- **`expire_on_commit=False`** keeps attribute values readable after commit, so the router can
  serialise the report without another `SELECT`.
- **Why Core for events.** ORM bulk insert splits batches by NULL pattern (1,404 statements for
  one real report). `insert(Event.__table__)` sends one `executemany`.

## 6. ORM models (`app/models.py`)

- `class Report(Base)` declares columns with `Mapped[...]` type hints. `Mapped[str | None]` means
  nullable; `Mapped[str]` means NOT NULL. Alembic reads this.
- Notable column options:
  - `deferred=True` on `raw` means `SELECT` statements skip it unless asked (`undefer(Report.raw)`
    in the `/raw` endpoint);
  - `server_default=func.now()` means the database fills it in;
  - `__mapper_args__ = {"eager_defaults": True}` fetches those defaults back with
    `INSERT ... RETURNING`.
- `status` uses `Enum(..., native_enum=False, create_constraint=True)`, which is a `VARCHAR`
  plus a `CHECK` constraint instead of a Postgres `ENUM` type, so it's easier to change later.
- `__table_args__` holds the table-level rules:
  - `CheckConstraint`s for "fixed needs a commit" and "closed needs a note";
  - the composite unique constraint and the indexes, including a partial index
    (`postgresql_where`) and GIN with `jsonb_path_ops`.
- The `relationship` between `Report` and `Event` exists for navigation. The cascade is done by
  the database (`ondelete="CASCADE"`, `passive_deletes=True`).

## 7. Migrations (`alembic/`)

- `alembic/env.py` points Alembic at `Base.metadata` and reads `DATABASE_URL` through the app's
  settings. The tests override the URL with `cfg.set_main_option("sqlalchemy.url", ...)`.
- `0001_initial_reports_and_events.py` was produced by `alembic revision --autogenerate`, then
  reviewed. Reviewing it caught three model mistakes:
  1. `raw` was nullable because `deferred()` hid the type hint;
  2. there was no identity on `events.id`;
  3. `status` values were unconstrained.

  Always read autogenerated migrations.
- Commands: `alembic upgrade head`, `alembic downgrade base`, `alembic check`.
- Postgres runs DDL inside transactions, so a failing migration leaves no half-built schema.
- The Docker image runs `alembic upgrade head` before uvicorn. That's fine for one instance; with
  several replicas it should be a separate release step, so that replicas don't race to migrate.

## 8. Postgres and JSONB in practice

Connect and try the queries the API generates:

```sql
-- what /events?tag=TRIGGER_ALLOWED&field=path:ARRIVAL runs, roughly
SELECT device_ts, build_sha, fields->>'dist' AS dist, fields->>'rad' AS rad
FROM events
WHERE tag = 'TRIGGER_ALLOWED'
  AND (fields @> '{"path": "ARRIVAL"}' OR fields @> '{"path": ["ARRIVAL"]}')
ORDER BY device_ts, id;

-- numeric comparisons need a cast, because values are stored as the strings the app logged
SELECT count(*) FROM events WHERE tag = 'DIAG2' AND subtag = 'FIX'
  AND (fields->>'acc')::float > 50;

-- is the GIN index used? (at 31k rows the planner may prefer a scan; that is fine)
EXPLAIN ANALYZE SELECT * FROM events WHERE fields @> '{"reason": "TIME_BACKSTOP"}';
```

- `->>` returns text, and `@>` is containment.
- `jsonb_path_ops` supports only `@>`, but its index is smaller.
- The `OR` exists because `{"k": ["v"]}` does not contain `{"k": "v"}`.

## 9. Docker networking (`Dockerfile`, `docker-compose.yml`)

- The image installs `requirements.txt` *before* copying the code, so that layer is cached and
  rebuilt only when dependencies change.
- It runs as a non-root user (`USER vergeo`) and has a `HEALTHCHECK` that calls `/health`.
- Compose puts both services on a private network with built-in DNS. The API reaches the
  database at host **`db`**, the service name, port 5432. Inside the api container, `localhost`
  would mean the api container itself, which is the classic first mistake.
- Only the API port is published (`8000:8000`); Postgres isn't reachable from the host unless you
  uncomment its `ports`.
- `depends_on: condition: service_healthy` waits until `pg_isready` passes. "Container started"
  is not enough, because Postgres takes a few seconds to accept connections, and the API's
  migration would race it.
- The data lives in the named volume `pgdata`. `docker compose down` keeps it, and
  `docker compose down -v` deletes it.

## 10. Tests (`tests/`)

- **`conftest.py`** sets the environment variables *before* importing the app (settings are
  read at import), then:
  - drops and recreates the schema;
  - runs `alembic upgrade head` once per session, refusing any database whose name lacks "test";
  - truncates the tables after each test.
- **`factory.py`** builds synthetic reports in the exact `ProblemReport.assemble()` layout. Real
  reports stay out of git; `fixtures/sample_lines.txt` holds six real lines, one of each shape.
- Layers:
  - `test_parser_*`: pure functions, no database;
  - `test_ingest.py`: the service on real Postgres, including the deterministic race (a stale
    first lookup), a real two-thread race with a `threading.Barrier`, rollback tests that inject
    a failure mid-batch, and the statement-count regression test;
  - `test_api_*`: through `TestClient` (in-process HTTP): auth matrix, filters, pagination,
    triage rules, JSONB filters, `/health` with a broken database, OpenAPI paths, and migration
    drift (`command.check`).
- Run a subset: `pytest tests/test_parser_events.py -q`, or `pytest -k arrival -q`.

## 11. CI (`.github/workflows/ci.yml`)

On every push and pull request:
1. Start a `postgres:16-alpine` **service container** with a health check, reachable from the job
   at `localhost:5432`.
2. Install Python 3.12 with pip caching keyed on `requirements-dev.txt`.
3. `ruff check .` and `ruff format --check .`
4. `alembic upgrade head`, `alembic downgrade base`, `alembic upgrade head`, `alembic check`. A
   broken migration fails here, before the tests, with a clear message.
5. `pytest -q`

Any failing step fails the job.

---

## Exercises (to check your own understanding)

1. Add `?has_warnings=true` to `GET /reports`. Which file changes, and which layer does the
   filter belong in?
2. Make a fixed report's `fix_commits` impossible to shrink, only grow. Should this be enforced in
   `triage.py`, the database, or both?
3. Upload the same real report twice concurrently with `curl ... &`. Which log lines and status
   codes do you expect?
4. Write the SQL that finds every `ALARM_EVAL` within 60 s before each ARRIVAL trigger in the
   same report. Should it be an endpoint?
5. Add `gitSha` to the APP line in Vergeo's `SESSION START` block. Which parser lines change so
   that `build_sha` is known for every event?
