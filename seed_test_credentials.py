"""One-off script: create/update two fixed local test accounts so you can
log in to the admin and employee dashboards with the same known
email+password every time, without running /signup first.

    Admin:    admin@example.com    / Password123   (Super Admin)
    Employee: employee@example.com / Password123   (Employee)

Run from the project root, with the project's own venv:

    .venv/bin/python seed_test_credentials.py

Then start the app with AUTH_MODE=password set (password login is disabled
in dev mode's pick-a-user screen — see app/routes/auth.py):

    AUTH_MODE=password SECRET_KEY=local-test-secret .venv/bin/python -m uvicorn app.main:app --port 8127

Idempotent — safe to rerun any time; it just resets the password (and
role) back to these values. Local testing only — these example.com
accounts should never exist in the live Azure database.
"""
from sqlalchemy import select

from app.db import SessionLocal, init_db
from app import models as m
from app.security import hash_password
from app.util import next_employee_code

TEST_PASSWORD = "Password123"

ACCOUNTS = [
    dict(name="Test Admin", email="admin@example.com", department="Operations",
         designation="Admin (test)", is_admin=True, is_super_admin=True),
    dict(name="Test Employee", email="employee@example.com", department="Operations",
         designation="Employee (test)", is_admin=False, is_super_admin=False),
]


def main() -> None:
    init_db()
    db = SessionLocal()
    try:
        for acc in ACCOUNTS:
            emp = db.execute(
                select(m.Employee).where(m.Employee.email == acc["email"])
            ).scalar_one_or_none()
            if emp is None:
                emp = m.Employee(
                    name=acc["name"],
                    email=acc["email"],
                    department=acc["department"],
                    designation=acc["designation"],
                    employee_code=next_employee_code(db),
                    active=True,
                )
                db.add(emp)
                db.flush()  # so next_employee_code() sees this row if called again below
                print(f"Created {acc['name']} <{acc['email']}>")
            else:
                print(f"Found existing {acc['name']} <{acc['email']}> — resetting password + role")

            emp.is_admin = acc["is_admin"]
            emp.is_super_admin = acc["is_super_admin"]
            emp.active = True
            emp.tracked = not acc["is_admin"]  # admin accounts excluded from compliance runs, same Roster default
            emp.password_hash = hash_password(TEST_PASSWORD)

        db.commit()
        print(f"\nDone. Log in at /login with either email above + '{TEST_PASSWORD}'")
        print("(needs AUTH_MODE=password set when you start the server — dev mode's")
        print("pick-a-user screen ignores passwords entirely).")
    finally:
        db.close()


if __name__ == "__main__":
    main()
