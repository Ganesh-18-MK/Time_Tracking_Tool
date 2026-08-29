"""
Companion to seed_dept_project_tasks.py.

Deactivates every project that is NOT one of the 27 seeded into a
department (instead of deleting them), per your choice to avoid orphaning
the 30 task_entries / 15 planned_tasks / 1 active_task_timer rows that
currently point at some of those other 276 projects.

What this does NOT do, on purpose:
- Does not delete any row anywhere -- fully reversible (just flip `active`
  back with SQL, or re-run this after re-linking a project to a department).
- Does not touch project_departments / project_tasks / task_entries /
  planned_tasks / active_task_timers at all.

Effect once you run it:
- The 27 seeded projects are the ONLY ones employees can pick from anywhere
  in the app (Today page Add Row / Auto timer / Plan for the Day, admin
  task-assignment combo) -- those pickers already filter to active=True.
- The admin Projects & Tasks page (/admin/lists) will still LIST all 303
  projects (that page shows inactive projects too, badged "Inactive", by
  design -- so an admin can Reactivate one later). If you also want the
  other 276 fully hidden from that page, say so and I'll add a filter --
  didn't want to change that behavior without asking first.

Run this yourself, from the project folder (note the trailing space in the
folder name -- keep the quotes), after running seed_dept_project_tasks.py:

    cd "/Users/Ganesh/Projects/mk-timekeeping-poc-main "
    python3 deactivate_other_projects.py
"""
import sqlite3

DB_PATH = "tms_demo.db"


def main():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    cur.execute("SELECT DISTINCT project_id FROM project_departments")
    keep_ids = [row[0] for row in cur.fetchall()]

    if not keep_ids:
        print("No project_departments rows found -- run seed_dept_project_tasks.py first. Aborting.")
        con.close()
        return

    placeholders = ",".join("?" for _ in keep_ids)
    cur.execute(
        f"SELECT COUNT(*) FROM projects WHERE active = 1 AND id NOT IN ({placeholders})",
        keep_ids,
    )
    to_deactivate = cur.fetchone()[0]

    cur.execute(
        f"UPDATE projects SET active = 0 WHERE active = 1 AND id NOT IN ({placeholders})",
        keep_ids,
    )
    con.commit()
    con.close()

    print(f"Kept active: {len(keep_ids)} projects (the ones linked to a department)")
    print(f"Deactivated: {to_deactivate} other projects")
    print("Done. Restart the app (or reload any page) to see the effect.")


if __name__ == "__main__":
    main()
