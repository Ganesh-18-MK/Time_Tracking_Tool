"""One-off script: create N throwaway employee accounts for load testing
(see load_test/locustfile.py), all sharing one known password.

seed_test_credentials.py (project root) intentionally only creates two
fixed accounts for manual dev login — this is a separate script rather
than an extension of that one, because a real concurrency test needs many
DISTINCT employee_id rows. Pointing 100 simulated concurrent users at the
same single employee account wouldn't test concurrency at all — it would
mostly test this app's own single-active-timer/one-punch-session-per-day
uniqueness constraints fighting each other, which is a different (and
much less interesting) failure mode than "can the app serve 100 different
real people at once."

Run from the project root, with the project's own venv, against
whichever DATABASE_URL you're about to load-test:

    .venv/bin/python load_test/seed_load_test_users.py [count]

count defaults to 60 (comfortably above the "100 concurrent users"
question, since not every simulated user is mid-request at the exact
same instant). Idempotent — safe to rerun; it only ever tops up to
`count` and resets the shared password.

Local/staging testing only. Like seed_test_credentials.py, these
loadtest+N@example.com accounts should never exist in the live database —
don't run this against a production DATABASE_URL.
"""
import sys

from sqlalchemy import select

from app import models as m
from app.db import SessionLocal, init_db
from app.security import hash_password
from app.util import next_employee_code

TEST_PASSWORD = "LoadTest123!"
EMAIL_DOMAIN = "example.com"
DEFAULT_COUNT = 60


def main(count: int) -> None:
    init_db()
    db = SessionLocal()
    try:
        created, updated = 0, 0
        for i in range(1, count + 1):
            email = f"loadtest{i}@{EMAIL_DOMAIN}"
            emp = db.execute(select(m.Employee).where(m.Employee.email == email)).scalar_one_or_none()
            if emp is None:
                emp = m.Employee(
                    name=f"Load Test {i}",
                    email=email,
                    department="Load Test",
                    designation="Load Test",
                    employee_code=next_employee_code(db),
                    active=True,
                    tracked=False,  # excluded from real compliance/strike runs, same as other test/admin accounts
                )
                db.add(emp)
                db.flush()
                created += 1
            else:
                updated += 1
            emp.is_admin = False
            emp.is_super_admin = False
            emp.active = True
            emp.password_hash = hash_password(TEST_PASSWORD)
        db.commit()
        print(f"Done: {created} created, {updated} already existed (password reset). Total: {count}")
        print(f"Login pattern: loadtest<1..{count}>@{EMAIL_DOMAIN} / '{TEST_PASSWORD}'")
        print("Needs AUTH_MODE=password on whichever instance you're pointing the load test at.")
    finally:
        db.close()


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_COUNT
    main(n)
