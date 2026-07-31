"""One-off script: seed 3 test employees (one of each role) + 1 project +
1 task type, so the dev login screen has cards to click and the Projects &
Tasks dropdowns aren't empty.

Run from the project root, with the project's own venv (needs the real
app package — this can't be run from Claude's sandbox):

    .venv/bin/python seed_dummy_data.py

Safe on the current blank tms.db. Refuses to run (no-op, prints a message)
if any of these emails already exist, so re-running it by accident won't
create duplicates or clobber real data later.
"""
import datetime as dt

from sqlalchemy import select

from app.db import SessionLocal, init_db
from app import models as m
from app.util import next_employee_code, role_to_flags

EMPLOYEES = [
    # name, email, department, designation, role
    ("Asha Kapoor", "asha.kapoor@mkimmigrationlaw.com", "Immigration", "Paralegal", "employee"),
    ("Ravi Menon", "ravi.menon@mkimmigrationlaw.com", "Immigration", "Team Lead", "admin"),
    ("Priya Nair", "priya.nair@mkimmigrationlaw.com", "Operations", "Operations Manager", "super_admin"),
]

PROJECT_NAME = "Client Onboarding – Acme Corp"
TASK_NAME = "Document Review"


def main() -> None:
    init_db()
    db = SessionLocal()
    try:
        emails = [e[1] for e in EMPLOYEES]
        existing = list(
            db.execute(select(m.Employee).where(m.Employee.email.in_(emails))).scalars()
        )
        if existing:
            print("Already seeded — found existing employee(s):")
            for e in existing:
                print(f"  - {e.name} <{e.email}>")
            print("Not adding anything. Delete these rows first if you want to reseed.")
            return

        for name, email, dept, designation, role in EMPLOYEES:
            is_admin, is_super_admin = role_to_flags(role)
            emp = m.Employee(
                name=name,
                email=email,
                department=dept,
                designation=designation,
                daily_target_minutes=480,
                work_days="0,1,2,3,4",
                start_date=dt.date.today(),
                employee_code=next_employee_code(db),
                active=True,
                is_admin=is_admin,
                is_super_admin=is_super_admin,
                tracked=not is_admin,  # admin accounts excluded from compliance runs, same as Roster's default
            )
            db.add(emp)
            db.commit()
            print(f"Added {name} ({role}) — {emp.employee_code}")

        if not db.execute(select(m.Project).where(m.Project.name == PROJECT_NAME)).scalar_one_or_none():
            db.add(m.Project(name=PROJECT_NAME, active=True))
            print(f"Added project: {PROJECT_NAME}")

        if not db.execute(select(m.TaskType).where(m.TaskType.name == TASK_NAME)).scalar_one_or_none():
            db.add(m.TaskType(name=TASK_NAME, active=True))
            print(f"Added task type: {TASK_NAME}")

        db.commit()
        print("\nDone. Restart the app (or just refresh /login) to see the cards.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
