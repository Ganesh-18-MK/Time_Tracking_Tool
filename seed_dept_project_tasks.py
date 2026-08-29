"""
One-off demo-data seeder for the Projects & Tasks tree.

Links 3 existing projects to each real department, and 3 existing tasks to
each of those 27 projects, purely so the Department -> Project -> Task tree
UI has something to show while testing (today every department shows
"0 project(s)" because tms_demo.db has 0 ProjectDepartment / ProjectTask
rows seeded yet, even though 303 Projects / 38 Tasks already exist).

Safe to re-run: uses INSERT OR IGNORE against the real unique indexes
(sqlite_autoindex_project_departments_1 on (project_id, department),
sqlite_autoindex_project_tasks_1 on (project_id, task_type_id)), so running
it twice just no-ops the second time instead of duplicating links.

Run this yourself, from the project folder (note the trailing space in the
folder name -- keep the quotes):

    cd "/Users/Ganesh/Projects/mk-timekeeping-poc-main "
    python3 seed_dept_project_tasks.py

(Per project convention, I never write directly to tms_demo.db from the
sandbox -- SQLite journal cleanup fails there and has corrupted the file
before. This script is meant to be run by you, locally.)
"""
import sqlite3
import datetime as dt

DB_PATH = "tms_demo.db"
CREATED_BY = "demo-seed-script"
PROJECTS_PER_DEPT = 3
TASKS_PER_PROJECT = 3


def main():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    # Same department set the tree UI actually shows: reports.departments_list()
    # only counts active + tracked employees, which is why the live screenshot
    # showed 9 groups even though the raw employees table has 12 distinct
    # department strings (Front Desk / Operations / Load Test drop out here).
    cur.execute(
        """
        SELECT DISTINCT department FROM employees
        WHERE active = 1 AND tracked = 1
          AND department IS NOT NULL AND department != ''
        ORDER BY department
        """
    )
    departments = [row[0] for row in cur.fetchall()]

    cur.execute("SELECT id FROM projects WHERE active = 1 ORDER BY id")
    project_ids = [row[0] for row in cur.fetchall()]

    cur.execute("SELECT id FROM task_types WHERE active = 1 ORDER BY id")
    task_ids = [row[0] for row in cur.fetchall()]

    if not departments:
        print("No active+tracked departments found -- nothing to do.")
        return
    need = len(departments) * PROJECTS_PER_DEPT
    if len(project_ids) < need:
        print(f"Only {len(project_ids)} active projects exist, need {need}. Aborting.")
        return
    if len(task_ids) < TASKS_PER_PROJECT:
        print(f"Only {len(task_ids)} active tasks exist, need {TASKS_PER_PROJECT}. Aborting.")
        return

    now = dt.datetime.utcnow().isoformat(sep=" ", timespec="seconds")

    dept_links_added = 0
    task_links_added = 0
    proj_cursor = 0
    seeded_projects = []

    for dept in departments:
        chosen = project_ids[proj_cursor: proj_cursor + PROJECTS_PER_DEPT]
        proj_cursor += PROJECTS_PER_DEPT
        for pid in chosen:
            cur.execute(
                """
                INSERT OR IGNORE INTO project_departments
                    (project_id, department, created_by, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (pid, dept, CREATED_BY, now),
            )
            if cur.rowcount:
                dept_links_added += 1
            seeded_projects.append(pid)

    # 3 tasks per seeded project, cycling through the active task list so
    # different projects get some variety instead of the same 3 every time.
    for i, pid in enumerate(seeded_projects):
        offset = (i * TASKS_PER_PROJECT) % len(task_ids)
        picks = [task_ids[(offset + j) % len(task_ids)] for j in range(TASKS_PER_PROJECT)]
        # de-dupe in the unlikely case len(task_ids) < TASKS_PER_PROJECT * 2 wraps onto itself
        picks = list(dict.fromkeys(picks))
        for tid in picks:
            cur.execute(
                """
                INSERT OR IGNORE INTO project_tasks
                    (project_id, task_type_id, created_by, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (pid, tid, CREATED_BY, now),
            )
            if cur.rowcount:
                task_links_added += 1

    con.commit()
    con.close()

    print(f"Departments seeded: {len(departments)} ({', '.join(departments)})")
    print(f"Project-department links added: {dept_links_added}")
    print(f"Project-task links added: {task_links_added}")
    print("Done. Restart the app (or just reload /admin/lists) to see the tree filled in.")


if __name__ == "__main__":
    main()
