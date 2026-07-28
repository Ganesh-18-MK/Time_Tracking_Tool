"""Build tms_demo.db — a fully anonymized copy of the real database.

    .venv/bin/python -m demo.make_demo_db

Keeps everything that makes the demo compelling (5 months of real status
patterns, strike distributions, variance ledgers, compensation links, task-log
shapes) while replacing every identifying string:

  * employee names/emails/notes  -> fictional roster (no first-name collisions
    with the real one — verified by the leak scan at the end)
  * client/project names         -> fictional companies
  * task details                 -> generic per-task-type phrases
  * leave notes                  -> generic per-leave-type phrases
  * audit log                    -> wiped, single demo marker row
  * admins                       -> Dana Whitmore / Chris Calloway / Pat Emerson

Statuses, dates, tokens (Y/N/PARTIAL/durations), targets, and config are kept
verbatim — none of them identify a person once the text fields are replaced.
"""
import os
import re
import shutil
import sqlite3
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL = os.path.join(BASE, "tms.db")
DEMO = os.path.join(BASE, "tms_demo.db")

FIRSTS = [
    "Ananya", "Rohan", "Ishaan", "Zara", "Farah", "Arjun", "Nisha", "Tara",
    "Vikram", "Meera", "Sana", "Ravi", "Anjali", "Varun", "Rhea", "Imran",
    "Fatima", "Leela", "Naveen", "Pooja", "Rahul", "Sneha", "Vivek", "Alok",
    "Bina", "Chirag", "Disha", "Esha", "Gaurav", "Indira", "Kunal", "Lata",
    "Neha", "Omar", "Parth", "Ritu", "Sahil", "Tanvi", "Uma", "Vandana",
    "Zoya", "Aditya", "Bhavna", "Ekta", "Girish", "Hansa", "Ila", "Jatin",
    "Kalpana", "Mohit", "Nandini", "Pallavi", "Qasim", "Rekha", "Tejas",
    "Ojas", "Falguni", "Devika", "Manish", "Shreya", "Ketan", "Vidya",
    "Ashray", "Bindu", "Chetna",
]
LASTS = [
    "Sharma", "Patel", "Menon", "Gupta", "Bose", "Fernandes", "Pillai",
    "Das", "Kulkarni", "Joshi", "Verma", "Nair", "Reddy", "Ghosh", "Sinha",
    "Chopra", "Malhotra", "Banerjee", "Mistry", "Saxena",
]
ADMIN_NAMES = ["Dana Whitmore", "Chris Calloway", "Pat Emerson"]

CO_A = ["Bluepeak", "Northwind", "Cedarline", "Quantex", "Silverbrook",
        "Halcyon", "Vantage", "Orionis", "Pinnacle", "Lumenar", "Graystone",
        "Fernhill", "Copperleaf", "Brightmark", "Stellune", "Harborview",
        "Kestrel", "Moraine", "Tidewater", "Juniper"]
CO_B = ["Consulting", "Technologies", "Solutions", "Systems", "Labs",
        "Group", "Software", "Analytics", "Partners", "Digital"]
CO_C = ["Inc.", "LLC", "Corp", "Co."]

TASK_DETAILS = {
    "check emails": "Inbox review and client replies",
    "send email": "Sent follow-up email to client",
    "reply to email": "Replied to pending client emails",
    "internal meetings": "Internal team sync call",
    "meeting with clients": "Client status call",
    "client calls": "Client call and notes",
    "call with team members": "Coordination call with team",
    "update planner/excel": "Updated case planner entries",
    "other misc. tasks": "Case processing and admin work",
}
LEAVE_NOTES = {
    "Casual": "Personal day",
    "Sick": "Unwell",
    "Vacation": "Planned vacation",
    "Other": "Excused absence (demo note)",
}


def fake_company(i: int) -> str:
    a = CO_A[i % len(CO_A)]
    b = CO_B[(i // len(CO_A)) % len(CO_B)]
    c = CO_C[(i // (len(CO_A) * len(CO_B))) % len(CO_C)]
    return f"{a} {b} {c}"


def main() -> int:
    if not os.path.exists(REAL):
        print("tms.db not found — run the importer first.")
        return 1
    shutil.copyfile(REAL, DEMO)
    con = sqlite3.connect(DEMO)
    cur = con.cursor()

    # ---- remember the real strings so we can prove they're gone ------------
    real_names = [r[0] for r in cur.execute("SELECT name FROM employees")]
    real_projects = [r[0] for r in cur.execute("SELECT name FROM projects")]
    real_words = {
        w for n in real_names for w in re.split(r"[\s./]+", n) if len(w) >= 3
    }

    # ---- employees ----------------------------------------------------------
    rows = cur.execute(
        "SELECT id, is_admin FROM employees ORDER BY is_admin DESC, id"
    ).fetchall()
    admin_i = emp_i = 0
    mapping = {}
    for emp_id, is_admin in rows:
        if is_admin and admin_i < len(ADMIN_NAMES):
            new = ADMIN_NAMES[admin_i]
            admin_i += 1
        else:
            new = f"{FIRSTS[emp_i % len(FIRSTS)]} {LASTS[emp_i % len(LASTS)]}"
            if emp_i >= len(FIRSTS):  # ensure uniqueness past one cycle
                new += f" {emp_i // len(FIRSTS) + 1}"
            emp_i += 1
        mapping[emp_id] = new
        email = new.lower().replace(" ", ".") + "@example.com"
        cur.execute(
            "UPDATE employees SET name=?, email=?, notes='' WHERE id=?",
            (new, email, emp_id),
        )

    # ---- junk free-text in department/designation (sheet artifacts) ---------
    name_pat = re.compile(
        "|".join(r"\b" + re.escape(w) + r"\b" for w in sorted(real_words, key=len, reverse=True)),
        re.IGNORECASE,
    )
    for emp_id, dept, desig in cur.execute(
        "SELECT id, department, designation FROM employees"
    ).fetchall():
        new_dept = "Operations" if (dept and name_pat.search(dept)) else dept
        new_desig = "" if (desig and name_pat.search(desig)) else desig
        if (new_dept, new_desig) != (dept, desig):
            cur.execute(
                "UPDATE employees SET department=?, designation=? WHERE id=?",
                (new_dept, new_desig, emp_id),
            )

    # ---- projects -----------------------------------------------------------
    for i, (pid,) in enumerate(
        cur.execute("SELECT id FROM projects ORDER BY id").fetchall()
    ):
        cur.execute("UPDATE projects SET name=? WHERE id=?", (fake_company(i), pid))

    # ---- task details (generic per task type) -------------------------------
    for tid, tname in cur.execute("SELECT id, name FROM task_types").fetchall():
        detail = TASK_DETAILS.get(tname.strip().lower(), "Case processing work")
        cur.execute(
            "UPDATE task_entries SET details=? WHERE task_type_id=?", (detail, tid)
        )

    # ---- leave notes / actor names ------------------------------------------
    for ltype, note in LEAVE_NOTES.items():
        cur.execute("UPDATE leave_records SET note=? WHERE type=?", (note, ltype))
    cur.execute(
        "UPDATE leave_records SET note=? WHERE type NOT IN (?,?,?,?)",
        (LEAVE_NOTES["Other"], *LEAVE_NOTES.keys()),
    )
    cur.execute("UPDATE leave_records SET entered_by='demo-import'")
    cur.execute("UPDATE compensation_links SET linked_by='Dana Whitmore', note='Made up via extra hours'")
    cur.execute("UPDATE day_statuses SET override_by='Dana Whitmore' WHERE override_by != ''")

    # ---- audit log ----------------------------------------------------------
    cur.execute("DELETE FROM audit_log")
    cur.execute(
        "INSERT INTO audit_log (at, actor, action, entity, entity_id, detail) "
        "VALUES (datetime('now'), 'demo-setup', 'anonymize', 'all', '', "
        "'{\"note\": \"demo database — all names, clients, and notes are fictional\"}')"
    )
    con.commit()

    # ---- leak scan: no real name-word or client may survive -----------------
    leaks = []
    text_cols = [
        ("employees", ["name", "email", "notes", "department", "designation"]),
        ("projects", ["name"]),
        ("task_types", ["name"]),
        ("task_entries", ["details"]),
        ("leave_records", ["note", "entered_by", "type"]),
        ("compensation_links", ["note", "linked_by"]),
        ("day_statuses", ["imported_token", "override_by", "override_reason"]),
        ("audit_log", ["actor", "detail"]),
        ("holidays", ["name"]),
    ]
    # skip generic words that legitimately appear (departments, tokens)
    skip = {"part", "time", "team", "lead", "front", "desk", "operations",
            "others", "support", "the", "and"}
    checks = sorted(w for w in real_words if w.lower() not in skip)
    for table, cols in text_cols:
        for col in cols:
            for (val,) in cur.execute(
                f"SELECT {col} FROM {table} WHERE {col} IS NOT NULL AND {col} != ''"
            ):
                low = str(val).lower()
                for w in checks:
                    if re.search(r"\b" + re.escape(w.lower()) + r"\b", low):
                        leaks.append((table, col, w, str(val)[:60]))
    for p in real_projects:
        key = p.lower().split(",")[0].strip()
        if len(key) >= 5:
            for (val,) in cur.execute("SELECT name FROM projects"):
                if key in val.lower():
                    leaks.append(("projects", "name", key, val))

    con.close()
    if leaks:
        print(f"LEAK SCAN FAILED — {len(leaks)} real string(s) survived:")
        for hit in leaks[:20]:
            print("  ", hit)
        os.remove(DEMO)
        return 1
    print(f"tms_demo.db written — {len(mapping)} people anonymized, "
          f"{len(real_projects)} clients renamed, leak scan clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
