# Performance review — "will this work with 100 concurrent users"

2026-08-21, prompted by Steve's question about concurrent load. Code-level review plus the
safe fixes that could be applied without a live load test (this environment can't install
`fastapi`/`sqlalchemy`/`locust` or reach a network, so nothing here has been measured against
real traffic yet — see [load_test/README.md](../load_test/README.md) for the tool to do that
and feed real numbers back into the next round).

## Applied now (safe, no behavior change, no compliance-math risk)

**Response compression (`app/main.py`).** Added Starlette's `GZipMiddleware`. Compresses every
text response — HTML pages, `/static` CSS/JS, XLSX/CSV exports — over 1KB before it goes over
the wire. Pure transport-layer plumbing, touches no route/query/template logic, so it doesn't
fall under CLAUDE.md's "changes to engine.py/validation.py need a pytest + verify_strikes
re-run" rule. Likely the single biggest win for *perceived* "slow loading" specifically —
Reports pages in particular render large HTML tables.

**DB connection pool sizing (`app/db.py`).** SQLAlchemy's defaults are `pool_size=5,
max_overflow=10` (15 connections total per process) on the Postgres path. Every route in this
app is a plain `def`, not `async def`, so FastAPI runs each one in a worker thread — AnyIO's
thread-pool limiter defaults to 40 concurrent threads per process. 15 < 40 means, under real
concurrent load, requests 16 through 40 would queue for a free DB connection even though
there was thread capacity to run them. Raised to `pool_size=20, max_overflow=20` (40 total,
matching the thread-pool default), both env-overridable (`DB_POOL_SIZE`/`DB_MAX_OVERFLOW`) so
they can be tuned against real load-test numbers without a redeploy. Also added
`pool_pre_ping=True`, which drops and transparently replaces a connection Postgres has
silently closed instead of surfacing a confusing mid-request error — a real cause of
intermittent "it just failed for no reason" reports the current code was exposed to, unrelated
to concurrency but the same file and worth fixing at the same time. Only applied on the
Postgres path — SQLite has no equivalent pool concept worth sizing (it's a single-writer file
lock regardless of pool settings).

## Found, documented, needs your call before I touch it

**`Procfile` runs a single process, no `--workers`.** Currently:
`web: uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers`. `gunicorn` is
already in requirements.txt but unused by the Procfile that actually ships — that looks like
leftover intent from `deploy_gcp.sh` (a different, evidently-abandoned deploy path) rather than
something wired up for the current Render hosting. More worker processes is the standard fix
for "single CPU-bound process can't use more than one core" — but I did **not** change this
myself, because of a real trade-off that's your call, not a safe default:

`app/rate_limit.py`'s login-lockout counters (both the per-email and the per-IP layer) are
plain in-process Python dicts — the module's own docstring already flags that these don't
survive Cloud Run autoscaling to multiple *instances*. The identical problem exists across
multiple worker *processes* on one instance too: each gunicorn worker is a separate OS
process with its own memory, so 4 workers means a determined attacker (or just unlucky
request routing) could get up to 4x the intended attempt budget before any single worker's
counter trips. Turning on multiple workers without also moving those counters somewhere
shared (Postgres — already the prod DB, per `app/db.py`'s own comment — or a host-level
control like Cloud Armor / Render's own rate limiting, both of which the rate_limit.py
docstring already names as the fix) would trade "maybe slow under 100 users" for "weaker
brute-force protection," which isn't a trade I'll make silently on a system handling real
payroll-adjacent data.

**Recommendation, in order:** (1) run the load test with the current single-worker Procfile
first — GZip + the pool fix above may already be enough at 100 users, since this is a
server-rendered app doing simple, well-indexed queries, not heavy compute; (2) if the numbers
say single-worker is genuinely the ceiling, move `rate_limit.py`'s counters into Postgres
*before* or *alongside* adding `--workers` to the Procfile, not after.

**`app/routes/admin.py`'s Dashboard and every Reports page call `engine.recompute_all()` on
every page load** (`_ensure_fresh()` in `app/reports.py`, and the Dashboard route directly),
rebuilding `DayStatus` for every active/tracked employee over the whole visible date range,
every single time the page is opened — including by a second admin, or the same admin
refreshing. The code that does this already flags the trade-off in its own comment:
`# live months stay fresh on load (cheap at this scale; nightly job optional)`. That was a
correct, deliberate call at ~45 staff with occasional admin page loads. Under 100 concurrent
users, especially if several are admins pulling reports around the same time, this is the one
piece of business logic (as opposed to plumbing) most likely to show up as a real bottleneck —
it's O(employees × days) of Python-level computation plus a `db.commit()` per employee, on
every single view. I deliberately did **not** rewrite this: it lives in `app/engine.py`, which
CLAUDE.md flags as needing a real `pytest` + `legacy.verify_strikes` run (168/168) after any
change, and this sandbox can't run either — a subtle mistake in strike-counting logic is a
compliance bug, not just a performance one, and isn't something to risk shipping unverified.
If the load test shows this is the actual ceiling, the fix the code already anticipated is
right there in its own comment: move the recompute off the request path onto a scheduled
nightly job (or invalidate-on-write instead of recompute-on-every-read), and only recompute
on-demand for the narrow "today, if it's within the live window" case that genuinely needs to
be fresh every time.

## Checked, no action needed

Foreign-key/lookup columns used in report/dashboard queries (`employee_id`, `date`,
`project_id`, `task_type_id`, etc. across `TaskEntry`, `DayStatus`, `BreakEntry`,
`PunchSession`, `LeaveRecord`, `OvertimeApproval`) already have `index=True` in
`app/models.py`. The batch-query helpers in `app/reports.py`
(`_rows_by_employee`/`_punch_minutes_by_day`/`_approved_overtime_ranges`) already use a single
`IN (...)` query plus in-Python dict aggregation per report, not a per-employee query loop —
no N+1 pattern found there.
