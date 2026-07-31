"""One-off script: set the same known password on every active employee,
so a demo can use /login directly (AUTH_MODE=password) without doing a live
/signup for each account.

Run from the project root, with the project's own venv:

    .venv/bin/python set_demo_passwords.py

Prints every email + the password so you have the full list to read off
during the demo. Safe to rerun — just resets the password again.
"""
from sqlalchemy import select

from app.db import SessionLocal, init_db
from app import models as m
from app.security import hash_password

DEMO_PASSWORD = "Demo@1234"


def main() -> None:
    init_db()
    db = SessionLocal()
    try:
        emps = list(db.execute(select(m.Employee).where(m.Employee.active.is_(True))).scalars())
        if not emps:
            print("No active employees found — nothing to set.")
            return

        for emp in emps:
            if not emp.email:
                print(f"Skipped (no email on file): {emp.name}")
                continue
            emp.password_hash = hash_password(DEMO_PASSWORD)
            role = "Super Admin" if emp.is_super_admin else ("Admin" if emp.is_admin else "Employee")
            print(f"{emp.name:<20} {emp.email:<35} {role:<12} password: {DEMO_PASSWORD}")

        db.commit()
        print(f"\nDone. Everyone above can log in at /login with their email + '{DEMO_PASSWORD}'.")
        print("Remember: this only works with AUTH_MODE=password set when you start the server.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
