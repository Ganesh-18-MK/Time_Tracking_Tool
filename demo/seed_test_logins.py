"""Set known, fixed test logins on the demo database (tms_demo.db) so
reviewers don't have to run /signup by hand before poking at the app.

    .venv/bin/python -m demo.seed_test_logins

    Admin:    admin@example.com    / Password123
    Employee: employee@example.com / Password123

Safe to re-run any time — always resets both accounts to the same
email/password and touches nothing else (no leave/break/task data is
affected). Also called automatically from make_demo_db.py so a freshly
regenerated demo database gets these two logins for free.
"""
import os
import sqlite3
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from app.security import hash_password  # noqa: E402

DEMO = os.path.join(BASE, "tms_demo.db")
TEST_PASSWORD = "Password123"
ADMIN_EMAIL = "admin@example.com"
EMPLOYEE_EMAIL = "employee@example.com"


def _ensure_column(cur, table: str, col: str, coltype: str) -> None:
    """Additive-only, same idea as app.db._add_missing_columns — this script
    can run against a demo db copied from a tms.db whose schema predates a
    given column (e.g. password_hash), so don't assume it's there."""
    cols = {r[1] for r in cur.execute(f"PRAGMA table_info({table})")}
    if col not in cols:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")


def seed(cur) -> None:
    """Point both a real admin row and a real employee row at the fixed
    test credentials. Picks the lowest-id active admin/employee rather than
    inventing new rows, so the account still shows up naturally in the
    roster/dashboard under its existing (demo-anonymized) name."""
    _ensure_column(cur, "employees", "password_hash", "VARCHAR(200)")
    pw_hash = hash_password(TEST_PASSWORD)

    admin = cur.execute(
        "SELECT id, name FROM employees WHERE is_admin=1 AND active=1 ORDER BY id LIMIT 1"
    ).fetchone()
    employee = cur.execute(
        "SELECT id, name FROM employees WHERE is_admin=0 AND active=1 ORDER BY id LIMIT 1"
    ).fetchone()

    if admin:
        cur.execute(
            "UPDATE employees SET email=?, password_hash=? WHERE id=?",
            (ADMIN_EMAIL, pw_hash, admin[0]),
        )
        print(f"Admin login ready    — {ADMIN_EMAIL} / {TEST_PASSWORD}  ({admin[1]})")
    if employee:
        cur.execute(
            "UPDATE employees SET email=?, password_hash=? WHERE id=?",
            (EMPLOYEE_EMAIL, pw_hash, employee[0]),
        )
        print(f"Employee login ready — {EMPLOYEE_EMAIL} / {TEST_PASSWORD}  ({employee[1]})")


def main() -> int:
    if not os.path.exists(DEMO):
        print("tms_demo.db not found — run:  .venv/bin/python -m demo.make_demo_db")
        return 1
    con = sqlite3.connect(DEMO)
    try:
        seed(con.cursor())
        con.commit()
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
