"""
One-off hard-delete: trim tms_demo.db down to exactly one employee per
department group.

Rule (per Ganesh, 2026-07-30):
  - Group ACTIVE employees by department (blank department is its own group).
  - Per group, keep the admin with the lowest id if any admin exists there,
    else keep the employee with the lowest id.
  - Every other active employee, and ALL 30 already-deactivated employees,
    are hard-deleted: the employees row plus every dependent row
    (task_entries, day_submissions, leave_records, day_statuses,
    compensation_links, break_entries, support_queries) that points at
    them via employee_id.
  - audit_log is left untouched — it references actors by name string, not
    a foreign key, and is a log, not employee data.

Only touches tms_demo.db (the anonymized demo database used for the Friday
demo). tms.db (the real/dev database) currently has 0 employees, so it's
left alone.

Run: python3 trim_demo_employees.py
"""
import shutil
import sqlite3
from collections import defaultdict
from datetime import datetime

DB = "tms_demo.db"
CHILD_TABLES = [
    "task_entries", "day_submissions", "leave_records",
    "day_statuses", "compensation_links", "break_entries", "support_queries",
]

backup = f"tms_demo.db.bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
shutil.copyfile(DB, backup)
print(f"Backup written: {backup}")

con = sqlite3.connect(DB)
cur = con.cursor()

cur.execute("select id, name, department, is_admin from employees where active=1 order by department, id")
active = cur.fetchall()
by_dept = defaultdict(list)
for row in active:
    by_dept[row[2]].append(row)

keep_ids = set()
for dept, emps in by_dept.items():
    keeper = next((e for e in emps if e[3] == 1), emps[0])
    keep_ids.add(keeper[0])

cur.execute("select id from employees")
all_ids = {r[0] for r in cur.fetchall()}
delete_ids = sorted(all_ids - keep_ids)

print(f"Total employees before: {len(all_ids)}")
print(f"Keeping {len(keep_ids)} (one per department group): {sorted(keep_ids)}")
print(f"Hard-deleting {len(delete_ids)} employees and their dependent rows...")

placeholders = ",".join("?" for _ in delete_ids)
child_counts = {}
for t in CHILD_TABLES:
    cur.execute(f"select count(*) from {t} where employee_id in ({placeholders})", delete_ids)
    n = cur.fetchone()[0]
    child_counts[t] = n
    cur.execute(f"delete from {t} where employee_id in ({placeholders})", delete_ids)

cur.execute(f"delete from employees where id in ({placeholders})", delete_ids)

con.commit()

cur.execute("select count(*) from employees")
print(f"Total employees after: {cur.fetchone()[0]}")
print("Dependent rows deleted:")
for t, n in child_counts.items():
    print(f"  {t}: {n}")

cur.execute("select id, name, department, is_admin from employees order by department, id")
print("\nRemaining roster:")
for row in cur.fetchall():
    print(f"  id={row[0]:>3} {row[1]:<20} dept={row[2] or '(blank)':<18} admin={bool(row[3])}")

con.close()
