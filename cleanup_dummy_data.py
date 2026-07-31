"""One-off script: remove the dummy employee/admin + dummy project/task added
by seed_dummy_data.py, keeping only the Super Admin account — renamed to a
real name + email you can actually sign up with — so you have one login to
go live with and bulk-upload real employees, projects, and tasks from.

Run from the project root, with the project's own venv:

    .venv/bin/python cleanup_dummy_data.py

Only touches the exact rows seed_dummy_data.py created (matched by email /
name) — safe to run even if you've already started adding real data,
since it won't touch anything else.
"""
from sqlalchemy import select

from app.db import SessionLocal, init_db
from app import models as m

KEEP_EMAIL = "priya.nair@mkimmigrationlaw.com"
NEW_NAME = "Ganesh Bendi"
NEW_EMAIL = "ganesh@mkimmigrationlaw.com"
REMOVE_EMAILS = ["ravi.menon@mkimmigrationlaw.com", "asha.kapoor@mkimmigrationlaw.com"]
REMOVE_PROJECT = "Client Onboarding – Acme Corp"
REMOVE_TASK = "Document Review"


def main() -> None:
    init_db()
    db = SessionLocal()
    try:
        kept = db.execute(select(m.Employee).where(m.Employee.email == KEEP_EMAIL)).scalar_one_or_none()
        if not kept:
            print(f"Didn't find {KEEP_EMAIL} — nothing to keep as the login. Stopping without changes.")
            return

        kept.name = NEW_NAME
        kept.email = NEW_EMAIL
        # password_hash stays NULL — sign up at /signup with NEW_EMAIL once
        # AUTH_MODE=password is live to set a real password.
        print(f"Renamed kept admin: {NEW_NAME} <{NEW_EMAIL}>")

        for email in REMOVE_EMAILS:
            emp = db.execute(select(m.Employee).where(m.Employee.email == email)).scalar_one_or_none()
            if emp:
                db.delete(emp)
                print(f"Removed employee: {emp.name} <{email}>")
            else:
                print(f"Not found (already removed?): {email}")

        proj = db.execute(select(m.Project).where(m.Project.name == REMOVE_PROJECT)).scalar_one_or_none()
        if proj:
            db.delete(proj)
            print(f"Removed project: {REMOVE_PROJECT}")

        task = db.execute(select(m.TaskType).where(m.TaskType.name == REMOVE_TASK)).scalar_one_or_none()
        if task:
            db.delete(task)
            print(f"Removed task type: {REMOVE_TASK}")

        db.commit()
        print(f"\nDone. Only {kept.name} ({kept.employee_code}, Super Admin) remains — log in as them to onboard real data.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
