# Load testing — "will 100 concurrent users work"

Companion to [docs/PERFORMANCE_REVIEW.md](../docs/PERFORMANCE_REVIEW.md), which has the
code-level findings and the fixes already applied. This is the tool to actually measure it
against a real running instance instead of guessing.

## Why this isn't wired into requirements.txt / CI

`locust` is a real, somewhat heavy dependency (pulls in gevent, a web UI, etc.) that has no
reason to ship with the production app or slow down every `pip install` for the rest of the
team. Install it in a throwaway virtualenv (or add it to a personal `requirements-dev.txt`)
only when you're about to run a load test:

```bash
pip install locust
```

## One-time setup, against whichever instance/DATABASE_URL you're testing

```bash
.venv/bin/python seed_test_credentials.py             # admin@example.com / Password123
.venv/bin/python load_test/seed_load_test_users.py 60  # loadtest1..60@example.com / LoadTest123!
```

Both scripts are idempotent and safe to rerun. **Local or staging only** — never run either
against a production `DATABASE_URL`; both docstrings say the same thing for the same reason.

The target instance needs `AUTH_MODE=password` set (dev mode's pick-a-user login doesn't
check passwords, so the seeded accounts' passwords wouldn't mean anything there) and needs
at least one approved, active Project and TaskType already in the roster (Admin > Projects &
Tasks) for `locustfile.py`'s Add Row task to log time against.

## Running it

```bash
locust -f load_test/locustfile.py --host http://localhost:8127
```

Open http://localhost:8089, set the number of users (100, to answer the actual question) and
a ramp-up rate (don't set this to 100/second — no real rollout would spike that fast either;
10-20/second is more realistic), and start. Watch:

- **Response time percentiles**, not just the average — a p95/p99 that's much worse than the
  median means some requests are queuing behind something (a lock, a full connection pool, a
  saturated thread pool), which the average alone hides.
- **`/admin (Dashboard)` and `/admin/reports/attendance` specifically** — see
  docs/PERFORMANCE_REVIEW.md's `recompute_all()` finding. If any endpoint is going to degrade
  first under load, the reasoning in that doc says it'll be one of these two.
- **Failure rate** — but read failures before assuming they're a performance problem. A 400
  from `/entries [POST]` because two simulated users' randomized times happened to overlap is
  the app's own validation working correctly, not a bug; a 500, a connection error, or a
  request that times out is the real signal.

## The one thing that will silently ruin a run

`app/rate_limit.py`'s per-IP login throttle allows 30 login attempts per 10 minutes from one
IP (see its own docstring — added 2026-08-17 after a TPRM traffic flag). Every simulated user
in this test shares the one real IP the Locust process runs from. `locustfile.py` already
logs each simulated user in exactly **once**, in `on_start`, and reuses that session's cookie
for the rest of the run — never re-login per request/task. If you modify the script and add a
login call inside a `@task`, you will trip that lockout almost immediately, and every
subsequent request will fail with "Too many failed attempts" — which will look exactly like
the app falling over under load, but isn't.

## Reading the results back into a decision

This sandbox that built these tools has no network access and can't install `fastapi`/
`sqlalchemy`/`locust` itself, so none of these numbers have actually been generated yet.
Run this against a local instance first (cheap, fast signal on obvious regressions), then
against whatever staging/production-equivalent environment matters, and share the Locust
summary (or the CSV export via `--csv`) back — from there the next round of tuning (worker
count, thread-pool size, `DB_POOL_SIZE`/`DB_MAX_OVERFLOW`, or finally tackling
`recompute_all()`'s cost) can be aimed at whatever the numbers actually show instead of
guessed at.
