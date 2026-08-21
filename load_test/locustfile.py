"""Locust load test — "will this work with 100 people using it at once."

Simulates two kinds of real traffic against a running instance of this app:

  EmployeeUser  (the vast majority): logs in once, then spends the test
    repeatedly viewing Today, logging a task-entry row, and checking My
    Month — the actual daily-use loop for the ~45+ real staff.
  AdminUser (a handful): logs in once as the one seeded admin account,
    then repeatedly loads the Dashboard and an Attendance Report — the
    two heaviest admin pages (see docs/PERFORMANCE_REVIEW.md's
    recompute_all() finding), which is exactly the traffic pattern most
    likely to expose it.

Setup (once, against whichever instance/DATABASE_URL you're testing):

    .venv/bin/python seed_test_credentials.py            # admin@example.com
    .venv/bin/python load_test/seed_load_test_users.py 60 # loadtest1..60@example.com
    AUTH_MODE=password SECRET_KEY=... .venv/bin/python -m uvicorn app.main:app --port 8127

Run (from the project root, needs `locust` installed — not in
requirements.txt on purpose, see load_test/README.md for why):

    locust -f load_test/locustfile.py --host http://localhost:8127

Then open http://localhost:8089, set "100" users / a ramp-up rate, and
start. For "does 100 concurrent users work", 100 users with most of them
EmployeeUser is the number that matters; watch the Attendance Report and
Dashboard endpoints in particular (see the docstring above).

IMPORTANT — do not point this at a login form repeatedly per request.
Every HttpUser here logs in exactly ONCE in on_start and keeps its
session cookie for the rest of the run (Locust's HttpUser wraps a
requests.Session, which persists cookies automatically) — both to behave
like a real user and because app/rate_limit.py's per-IP throttle
(IP_MAX_ATTEMPTS=200 per 10 minutes, see its own docstring) would
otherwise lock out this entire test after ~30 login POSTs, since every
simulated user shares the one real IP the test happens to run from. If
you see a wall of "Too many failed attempts" responses, that's the
throttle doing its job against a misbehaving script, not the app being
slow.
"""
import random

from locust import HttpUser, between, events, task

TEST_PASSWORD = "LoadTest123!"
ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "Password123"
LOADTEST_USER_COUNT = 60  # keep in sync with whatever you passed to seed_load_test_users.py

# Populated once at test start from the same database the target instance
# uses (see _load_pickable_ids below) — every EmployeeUser reuses these
# rather than each hitting the DB itself, and it's how Add Row gets a
# real project_id/task_type_id without scraping the Today page's HTML.
_project_id = None
_task_type_id = None


def _assert_logged_in(resp, who: str) -> None:
    """Raises a clear, specific error instead of on_start's raw
    `if "login" in resp.url` crashing with a confusing
    `TypeError: argument of type 'NoneType' is not a container` — that
    TypeError is what a *connection failure* looks like (refused, reset,
    timed out), because Locust hands back a placeholder response with
    resp.url == None rather than raising. That's a completely different
    problem from a lockout/bad-password redirect (resp.url is a real URL
    containing "login"), so tell the two apart here and say which one it
    was."""
    if resp is None or getattr(resp, "url", None) is None:
        raise RuntimeError(
            f"{who}: no response at all (connection refused/reset/timed out) — "
            "the target --host isn't reachable. Most likely the server process "
            "isn't actually running (check its terminal for a crash, or "
            "`lsof -i :<port>` for a stale/orphaned process still holding the "
            "port from a previous run) — this is not the app being slow under "
            "load, Locust never got as far as sending the login."
        )
    if "login" in resp.url:
        raise RuntimeError(
            f"{who}: login failed (ended up on {resp.url}) — did you run the "
            "seed script and set AUTH_MODE=password on the target, or is this "
            "the per-email/per-IP lockout in app/rate_limit.py? See "
            "load_test/README.md's RATE_LIMIT_DISABLED section."
        )


@events.test_start.add_listener
def _load_pickable_ids(environment, **kwargs):
    """Runs once per test, in the locust process itself (not simulated
    HTTP traffic) — reads directly from the app's own DATABASE_URL to
    find one approved, active Project and TaskType to log time against.
    Requires running locust from a checkout that can import `app` and
    reach the same database the target --host is actually serving from
    (true for local/staging; if you're pointing --host at a shared
    staging server, run locust from a machine with that same
    DATABASE_URL set in its environment)."""
    global _project_id, _task_type_id
    from sqlalchemy import select

    from app import models as m
    from app.db import SessionLocal, init_db

    init_db()
    db = SessionLocal()
    try:
        project = db.execute(
            select(m.Project).where(m.Project.active.is_(True), m.Project.status == m.LIST_APPROVED)
        ).scalars().first()
        task_type = db.execute(
            select(m.TaskType).where(m.TaskType.active.is_(True), m.TaskType.status == m.LIST_APPROVED)
        ).scalars().first()
        if project is None or task_type is None:
            raise RuntimeError(
                "No approved Project/TaskType found — add at least one via Admin > Projects & Tasks "
                "before running this load test."
            )
        _project_id, _task_type_id = project.id, task_type.id
        print(f"Load test will log time against project_id={_project_id}, task_type_id={_task_type_id}")
    finally:
        db.close()


class EmployeeUser(HttpUser):
    weight = 9  # ~90% of simulated traffic — matches the real employee:admin ratio
    wait_time = between(2, 6)  # a real person pauses between clicks; hammering with 0 wait tests nothing realistic

    def on_start(self):
        n = random.randint(1, LOADTEST_USER_COUNT)
        self.email = f"loadtest{n}@example.com"
        resp = self.client.post(
            "/login/employee",
            data={"email": self.email, "password": TEST_PASSWORD},
            name="/login/employee",
        )
        # Fail loudly and specifically instead of quietly running 0 real
        # requests, or crashing with a confusing TypeError on a connection
        # failure (see _assert_logged_in's docstring).
        _assert_logged_in(resp, f"EmployeeUser login for {self.email}")
        # Start each simulated day partway through the morning so repeated
        # Add Row calls have somewhere to go without immediately colliding
        # with each other within the same run.
        self.next_start_minute = 540  # 9:00 AM

    @task(5)
    def view_today(self):
        self.client.get("/today", name="/today")

    @task(3)
    def add_entry(self):
        start = self.next_start_minute
        end = min(start + random.choice([15, 30, 45, 60]), 1439)
        self.next_start_minute = end + random.choice([0, 5, 10])  # occasional small gap, exercises gap_flags too
        if self.next_start_minute >= 1380:  # stop before running off the end of the day
            self.next_start_minute = 540

        def fmt(m):
            return f"{m // 60:02d}:{m % 60:02d}"

        import datetime as dt
        today_iso = dt.date.today().isoformat()
        self.client.post(
            "/entries",
            data={
                "date": today_iso,
                "project_id": _project_id,
                "task_type_id": _task_type_id,
                "details": "Load test entry",
                "start_time": fmt(start),
                "end_time": fmt(end),
            },
            name="/entries [POST]",
        )

    @task(2)
    def view_my_month(self):
        self.client.get("/my-month", name="/my-month")


class AdminUser(HttpUser):
    weight = 1  # ~10% of simulated traffic
    wait_time = between(3, 8)

    def on_start(self):
        resp = self.client.post(
            "/login/admin",
            data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
            name="/login/admin",
        )
        _assert_logged_in(resp, "AdminUser login")

    @task(3)
    def view_dashboard(self):
        # The heaviest single page in the app today — recomputes DayStatus
        # for every active/tracked employee over the whole visible month on
        # every load (see docs/PERFORMANCE_REVIEW.md). The one to watch.
        self.client.get("/admin", name="/admin (Dashboard)")

    @task(1)
    def view_attendance_report(self):
        self.client.get("/admin/reports/attendance?range=30d", name="/admin/reports/attendance")
