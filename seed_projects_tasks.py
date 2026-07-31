"""One-off script: add 2 dummy Projects + 2 dummy Task Types to the
Projects & Tasks dropdown lists, for testing.

Run from the project root, with the project's own venv:

    .venv/bin/python seed_projects_tasks.py

Idempotent — skips (and prints a note) for any name that already exists,
so it's safe to run more than once.
"""
from sqlalchemy import select

from app.db import SessionLocal, init_db
from app import models as m

PROJECTS = [
    "Client Onboarding – Acme Corp",
    "H-1B Visa Filing – Beta Industries",
]

TASKS = [
    "Document Review",
    "Client Communication",
]


def main() -> None:
    init_db()
    db = SessionLocal()
    try:
        for name in PROJECTS:
            if db.execute(select(m.Project).where(m.Project.name == name)).scalar_one_or_none():
                print(f"Already exists, skipped: {name}")
                continue
            db.add(m.Project(name=name, active=True))
            print(f"Added project: {name}")

        for name in TASKS:
            if db.execute(select(m.TaskType).where(m.TaskType.name == name)).scalar_one_or_none():
                print(f"Already exists, skipped: {name}")
                continue
            db.add(m.TaskType(name=name, active=True))
            print(f"Added task type: {name}")

        db.commit()
        print("\nDone. Refresh the Dropdown lists admin page to see them.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
