"""Seed the app database from the three legacy artifacts (PRD §9).

    .venv/bin/python -m legacy.import_legacy

Imports:
  1. Compliance sheet  -> roster + departments + frozen historical DayStatus
                          (+ sheet strike values kept for the acceptance test,
                           + auto-detected company holidays)
  2. Leave Tracker     -> LeaveRecord history, per-day extra/short variance,
                          per-person target hints, GONE (departed) flags
  3. Divya Task Summary (pre-extracted cache) -> TaskEntry history, real
                          Project/Task dropdown lists from the hidden List sheet

Everything imported is marked source='imported' / imported=True and is FROZEN:
the engine never recomputes it. Live computation starts at live_start_date.
Legacy oddities never crash the import — they land in cache/import_report.json.
"""
import datetime as dt
import difflib
import json
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from app import models as m  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402

try:
    from legacy.ods_reader import iter_rows
except ImportError:  # run as a script from legacy/
    from ods_reader import iter_rows

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMPLIANCE = os.path.join(BASE, "Task Summary Compliance.ods")
LEAVE_TRACKER = os.path.join(BASE, "Leave Tracker.ods")
TASK_CACHE = os.path.join(BASE, "legacy", "cache", "divya")
REPORT_PATH = os.path.join(BASE, "legacy", "cache", "import_report.json")

TODAY = dt.date.today()  # live computation starts at import day; history is frozen
KNOWN_SECTIONS = {"others", "part time support", "team leads", "gone"}
ADMINS = ["Norine", "Steve", "Mary"]

report = defaultdict(list)


def norm(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def cell_str(c) -> str:
    if isinstance(c.value, str):
        return c.value.strip()
    return (c.text or "").strip()


ISO_DUR = re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?$")


def iso_dur_minutes(v):
    if not isinstance(v, str):
        return None
    mm = ISO_DUR.match(v)
    if not mm:
        return None
    return int(mm.group(1) or 0) * 60 + int(mm.group(2) or 0) + (1 if float(mm.group(3) or 0) >= 30 else 0)


DUR_TOKEN = re.compile(r"(\d+(?:\.\d+)?)\s*(h\w*|m\w*)", re.IGNORECASE)


def text_duration_minutes(text: str):
    """'4 hours 30 min' / '1hr18min' / '43 min' / 'LOGIN-... Hours-8.10 hrs ...'
    -> minutes. Numbers like 8.10 next to an h-word read as h:mm (legacy habit)."""
    total = 0.0
    found = False
    for num, unit in DUR_TOKEN.findall(text or ""):
        u = unit.lower()
        if u.startswith("h"):
            if "." in num:
                h, frac = num.split(".", 1)
                if len(frac) == 2 and int(frac) <= 59:  # h.mm convention
                    total += int(h) * 60 + int(frac)
                else:
                    total += float(num) * 60
            else:
                total += int(num) * 60
            found = True
        elif u.startswith("m"):
            total += float(num)
            found = True
    return int(round(total)) if found and total > 0 else None


# ---------------------------------------------------------------------------
# 1. Compliance sheet
# ---------------------------------------------------------------------------
def classify_token(token: str, is_weekend: bool):
    """-> (status, actual_minutes | None) or None to skip. Strike semantics
    mirror the sheet's own formula exactly: only N / PARTIAL are strikes."""
    t = token.strip()
    u = t.upper()
    if u in ("Y", "WORKING"):
        return (m.COMPLETE, None)
    if u == "N":
        return (m.MISSING, None)
    if u == "PARTIAL":
        return (m.PARTIAL, None)
    if u in ("LEAVE", "LE", "ML"):
        return (m.LEAVE, None)
    if u == "HOLIDAY":
        return (m.HOLIDAY, None)
    if u in ("TRAVEL", "ABSENT"):
        return (m.LEAVE, None)  # excused in the sheet (not counted by COUNTIF)
    if "POLICY" in u or u == "COMPLETION" or "%" in u:
        return None  # banner/junk cells
    dur = text_duration_minutes(t)
    if dur is not None:
        # free-text hours: weekend rows are extra work; weekday rows were
        # noted-but-not-struck in the sheet, so they read Complete here
        if is_weekend:
            return (m.WEEKEND, dur)
        return (m.COMPLETE, dur)
    return None


class PersonAgg:
    def __init__(self, display):
        self.display = display
        self.dept = ""
        self.desig = ""
        self.dept_month = None
        self.tokens = {}          # date -> raw token
        self.sheet_strikes = {}   # 'YYYY-MM' -> float from the sheet's formula
        self.strike_start = {}    # 'YYYY-MM' -> first date the sheet's own
                                  # COUNTIF range covers (policy start mid-April)


def col_letters_to_idx(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


COUNTIF_START = re.compile(r"COUNTIF\(\[\.([A-Z]+)\d+")


def parse_compliance(path):
    people = {}
    order = []
    sheet_headers = {}
    current_dept = {}

    for sheet, idx, cells in iter_rows(path, max_cols=40):
        if not cells:
            continue
        if idx == 0:
            date_cols, strikes_col = {}, None
            for j, c in enumerate(cells):
                if c.value_type == "date":
                    date_cols[j] = dt.date.fromisoformat(str(c.value)[:10])
                elif "STRIKES" in cell_str(c).upper():
                    strikes_col = j
            sheet_headers[sheet] = (date_cols, strikes_col)
            current_dept[sheet] = ""
            continue
        if sheet not in sheet_headers:
            continue
        date_cols, strikes_col = sheet_headers[sheet]
        name = cell_str(cells[0]) if cells else ""
        if not name:
            continue
        dept = cell_str(cells[1]) if len(cells) > 1 else ""
        desig = cell_str(cells[2]) if len(cells) > 2 else ""
        tokens = {}
        for j, d in date_cols.items():
            if j < len(cells):
                tok = cell_str(cells[j])
                if tok:
                    tokens[d] = tok
        strikes_val = None
        strike_start = None
        if strikes_col is not None and strikes_col < len(cells):
            sc = cells[strikes_col]
            if isinstance(sc.value, float):
                strikes_val = sc.value
            if sc.formula:
                mm = COUNTIF_START.search(sc.formula)
                if mm:
                    strike_start = date_cols.get(col_letters_to_idx(mm.group(1)))

        if norm(name) in KNOWN_SECTIONS or (
            not dept and not desig and not tokens and strikes_val is None and name.isupper()
        ):
            current_dept[sheet] = name.title() if name.isupper() else name
            continue

        if dept:
            current_dept[sheet] = dept

        key = norm(name)
        agg = people.get(key)
        if agg is None:
            agg = people[key] = PersonAgg(name)
            order.append(key)
        month_key = tokens and min(tokens).strftime("%Y-%m")
        eff_dept = dept or current_dept[sheet]
        # newest month with data wins for dept/designation
        if eff_dept and (tokens or not agg.dept):
            mk = month_key or "0000-00"
            if agg.dept_month is None or mk >= agg.dept_month:
                agg.dept, agg.dept_month = eff_dept, mk
        if desig and not agg.desig:
            agg.desig = desig
        overlap = set(tokens) & set(agg.tokens)
        if overlap:
            report["duplicate_rows_merged"].append(
                {"sheet": sheet, "name": name, "dates": [d.isoformat() for d in sorted(overlap)]}
            )
        agg.tokens.update(tokens)
        ym = f"{sheet.split('_')[1]}-{dt.datetime.strptime(sheet.split('_')[0], '%B').month:02d}"
        if strikes_val is not None:
            # keep the row's value; if duplicated rows both had formulas, keep max (data row)
            agg.sheet_strikes[ym] = max(strikes_val, agg.sheet_strikes.get(ym, -1))
        if strike_start is not None:
            agg.strike_start[ym] = strike_start
    return people, order


# ---------------------------------------------------------------------------
# 2. Leave tracker
# ---------------------------------------------------------------------------
def parse_leave_tracker(path):
    """-> per-tab: leave rows, extra/short per date, target hints; + GONE set."""
    tabs = {}
    gone_after = set()
    seen_gone = False
    tab_order = []

    current = None
    for sheet, idx, cells in iter_rows(path, max_cols=44):
        if sheet != current:
            current = sheet
            tab_order.append(sheet)
            if norm(sheet) == "gone":
                seen_gone = True
                continue
            tabs[sheet] = {"leaves": [], "varmap": {}, "target": None, "extra_blocks": []}
            if seen_gone:
                gone_after.add(sheet)
        if norm(sheet) == "gone":
            continue
        tab = tabs[sheet]
        texts = [cell_str(c) for c in cells]

        # 'extra'/'short' header pairs mark the monthly variance blocks
        for j in range(len(cells) - 1):
            if texts[j].lower() == "extra" and texts[j + 1].lower() == "short":
                tab["extra_blocks"].append((j - 1, j, j + 1))

        # target hints: 'Standard work hours: Total 7.5'
        mt = re.search(r"standard work hours:\s*total\s*([\d.]+)", texts[0].lower()) if texts else None
        if mt:
            tab["target"] = int(round(float(mt.group(1)) * 60))

        # left block: leave/incident rows have a date in col1
        if len(cells) > 1 and cells[1].value_type == "date":
            when = dt.date.fromisoformat(str(cells[1].value)[:10])
            ltype = texts[0] if texts and texts[0] in ("Casual", "Sick", "Vacation") else "Other"
            minutes = None
            if len(cells) > 2:
                hv = cells[2]
                if isinstance(hv.value, float):
                    minutes = int(round(hv.value * 60))
                elif hv.value_type == "time":
                    minutes = iso_dur_minutes(hv.value)
                elif cell_str(hv):
                    report["unparsed_leave_hours"].append({"tab": sheet, "date": when.isoformat(), "value": cell_str(hv)})
            note = " | ".join(x for x in texts[3:5] if x) if len(texts) > 3 else ""
            tab["leaves"].append({"date": when, "type": ltype, "minutes": minutes, "note": note})

        # variance blocks: date at (block_date_col), durations in extra/short cols
        for dcol, ecol, scol in tab["extra_blocks"]:
            if 0 <= dcol < len(cells) and cells[dcol].value_type == "date":
                d = dt.date.fromisoformat(str(cells[dcol].value)[:10])
                extra = iso_dur_minutes(cells[ecol].value) if ecol < len(cells) else None
                short = iso_dur_minutes(cells[scol].value) if scol < len(cells) else None
                if extra or short:
                    prev_e, prev_s = tab["varmap"].get(d, (0, 0))
                    tab["varmap"][d] = (max(prev_e, extra or 0), max(prev_s, short or 0))
    return tabs, gone_after


def merge_name_variants(people, order):
    """The sheets spell the same person differently across months
    ('Haswatha'/'Haswathi', 'Maha Lakshmi'/'Mahalakshmi', 'Sreenivasan'/
    'Srinivasan Jayamoorthy'). Merge variants conservatively: only when the
    two records never overlap in time AND the names agree strongly — space-
    stripped equality, a ≥6-char common prefix, or a shared ≥6-char word
    (surnames like 'jayamoorthy'). This keeps genuinely different people
    apart (Krithika vs Karthika share only a 1-char prefix)."""

    def months(agg):
        return {d.strftime("%Y-%m") for d in agg.tokens}

    def strong_match(a, b):
        if a.replace(" ", "") == b.replace(" ", ""):
            return True
        pref = os.path.commonprefix([a.replace(" ", ""), b.replace(" ", "")])
        if len(pref) >= 6:
            return True
        wa = {w for w in a.split() if len(w) >= 6}
        wb = {w for w in b.split() if len(w) >= 6}
        return bool(wa & wb)

    merged_into = {}
    keys = [k for k in order if k in people]
    for i, ka in enumerate(keys):
        if ka in merged_into:
            continue
        for kb in keys[i + 1:]:
            if kb in merged_into or ka == kb:
                continue
            a, b = people[ka], people[kb]
            if not strong_match(ka, kb):
                continue
            if months(a) & months(b):
                continue  # both have data in the same month => different people
            # merge b into a; keep the display/dept of the later record
            report["name_variants_merged"].append({"kept": a.display, "merged": b.display})
            a.tokens.update(b.tokens)
            for ym, v in b.sheet_strikes.items():
                a.sheet_strikes[ym] = max(v, a.sheet_strikes.get(ym, -1))
            a.strike_start.update(b.strike_start)
            if b.dept_month and (a.dept_month is None or b.dept_month > a.dept_month):
                a.dept, a.dept_month, a.display = b.dept, b.dept_month, b.display
            if b.desig and not a.desig:
                a.desig = b.desig
            merged_into[kb] = ka
            del people[kb]
    order[:] = [k for k in order if k in people]

    # junk rows that are neither people nor sections ('Mail Room / Print')
    for k in list(people):
        if "/" in k and not people[k].tokens:
            report["junk_name_rows_dropped"].append(people[k].display)
            del people[k]
            order.remove(k)


def match_tab_to_person(tab_name, people_keys):
    t = norm(tab_name).rstrip("_")
    if t in people_keys:
        return t
    for k in people_keys:
        if k.startswith(t) or t.startswith(k) or t in k.split() or k.split()[0] == t:
            return k
    # tracker tabs use first names with drifting spellings ('Surendhar' vs
    # 'Surendar Lakshmanan') — fuzzy-match on first words too
    firsts = {k: k.split()[0] for k in people_keys}
    best, best_ratio = None, 0.0
    for k, first in firsts.items():
        r = difflib.SequenceMatcher(None, t, first).ratio()
        if r > best_ratio:
            best, best_ratio = k, r
    if best_ratio >= 0.85:
        return best
    close = difflib.get_close_matches(t, list(people_keys), n=1, cutoff=0.72)
    return close[0] if close else None


# ---------------------------------------------------------------------------
# 3. Divya task history (from the pre-extracted cache)
# ---------------------------------------------------------------------------
def normalize_day_times(entries):
    """Legacy sheets use 12-hour clock values with no AM/PM (12:00 -> 1:00
    afternoons). Reconstruct a monotone timeline per day, +12h when a time
    steps backwards. This is what kills the 13-hour-for-1-hour-task rows."""
    out = []
    prev_end = None
    for e in entries:
        s, en = e["start_min"], e["end_min"]
        if s is None or en is None:
            report["task_rows_skipped_no_times"].append(e)
            continue
        if prev_end is not None:
            while s + 30 < prev_end and s + 720 < 1440:
                s += 720
        while en <= s:
            if en + 720 <= 1440:
                en += 720
            else:
                en = 1440
                break
        if en > 1440:
            en = 1440
        out.append({**e, "start_min": s, "end_min": en})
        prev_end = en
    return out


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    init_db()
    db = SessionLocal()
    if db.execute(select(m.Employee)).first() is not None:
        print("Database is not empty — refusing to import. Delete tms.db and retry.")
        return 1

    # ---- compliance ---------------------------------------------------------
    print("Parsing compliance sheet ...")
    people, order = parse_compliance(COMPLIANCE)
    merge_name_variants(people, order)
    print(f"  {len(people)} distinct people after variant merge")

    # ---- leave tracker -------------------------------------------------------
    print("Parsing leave tracker ...")
    tabs, gone_tabs = parse_leave_tracker(LEAVE_TRACKER)
    gone_keys = set()
    tab_match = {}
    for tab in tabs:
        k = match_tab_to_person(tab, set(people))
        tab_match[tab] = k
        if k is None:
            report["tracker_tab_unmatched"].append(tab)
        elif tab in gone_tabs:
            gone_keys.add(k)

    # ---- create employees -----------------------------------------------------
    emp_by_key = {}
    for key in order:
        agg = people[key]
        start = min(agg.tokens) if agg.tokens else None
        has_july = any(d.month == 7 and d.year == 2026 for d in agg.tokens)
        active = key not in gone_keys or has_july
        target = 480
        for tab, k in tab_match.items():
            if k == key and tabs[tab]["target"]:
                target = tabs[tab]["target"]
        emp = m.Employee(
            name=agg.display,
            department=agg.dept,
            designation=agg.desig,
            daily_target_minutes=target,
            start_date=start,
            active=active,
            tracked=True,
            notes="imported from legacy compliance sheet",
        )
        db.add(emp)
        emp_by_key[key] = emp
    db.commit()

    # tracker-only people (departed before the compliance sheet started)
    for tab, k in tab_match.items():
        if k is None and norm(tab) != "gone":
            emp = m.Employee(
                name=tab, active=False, tracked=True,
                notes="imported from leave tracker only (GONE section)"
                if tab in gone_tabs else "imported from leave tracker only",
            )
            db.add(emp)
            emp_by_key[norm(tab)] = emp
            tab_match[tab] = norm(tab)
            report["tracker_only_people"].append(tab)
    db.commit()

    # ---- auto-detect company holidays (many people marked Holiday same date) --
    holiday_votes = Counter()
    for agg in people.values():
        for d, tok in agg.tokens.items():
            if tok.strip().upper() == "HOLIDAY":
                holiday_votes[d] += 1
    company_holidays = {d for d, n in holiday_votes.items() if n >= 5}
    for d in sorted(company_holidays):
        db.add(m.Holiday(date=d, name="Imported from compliance sheet"))
    db.commit()
    print(f"  auto-detected {len(company_holidays)} company holidays")

    # ---- frozen DayStatus rows -------------------------------------------------
    # The sheet's own COUNTIF ranges define what counted as a strike (April's
    # start at the 15th — policy effective mid-month). Tokens before a row's
    # range keep their true status but are marked strike_exempt.
    modal_start = {}
    start_votes = defaultdict(Counter)
    for agg in people.values():
        for ym, d in agg.strike_start.items():
            start_votes[ym][d] += 1
    for ym, votes in start_votes.items():
        modal_start[ym] = votes.most_common(1)[0][0]

    n_status = n_exempt = 0
    for key, agg in people.items():
        emp = emp_by_key[key]
        for d, tok in agg.tokens.items():
            cls = classify_token(tok, d.weekday() >= 5)
            if cls is None:
                report["tokens_skipped"].append({"name": agg.display, "date": d.isoformat(), "token": tok})
                continue
            status, actual = cls
            if d in company_holidays and status in (m.HOLIDAY, m.WEEKEND):
                status = m.HOLIDAY
            variance = None
            if status == m.WEEKEND and actual:
                variance = actual
            ym = d.strftime("%Y-%m")
            range_start = agg.strike_start.get(ym) or modal_start.get(ym)
            exempt = bool(
                status in m.STRIKE_STATUSES and range_start is not None and d < range_start
            )
            if exempt:
                n_exempt += 1
            db.add(
                m.DayStatus(
                    employee_id=emp.id, date=d, status=status,
                    actual_minutes=actual, target_minutes=None if status in (m.WEEKEND, m.HOLIDAY) else emp.daily_target_minutes,
                    variance_minutes=variance, source="imported", imported_token=tok,
                    strike_exempt=exempt,
                )
            )
            n_status += 1
    db.commit()
    print(f"  {n_status} frozen day-status rows ({n_exempt} pre-policy strike-exempt)")

    # ---- leave records + variance merge ----------------------------------------
    n_leave = n_var = 0
    for tab, key in tab_match.items():
        if key is None or key not in emp_by_key:
            continue
        emp = emp_by_key[key]
        for lv in tabs.get(tab, {}).get("leaves", []):
            db.add(
                m.LeaveRecord(
                    employee_id=emp.id, start_date=lv["date"], end_date=lv["date"],
                    type=lv["type"], minutes_per_day=lv["minutes"],
                    note=lv["note"], entered_by="legacy-import", imported=True,
                )
            )
            n_leave += 1
        rows = {
            r.date: r
            for r in db.execute(
                select(m.DayStatus).where(m.DayStatus.employee_id == emp.id)
            ).scalars()
        }
        for d, (extra, short) in tabs.get(tab, {}).get("varmap", {}).items():
            net = (extra or 0) - (short or 0)
            row = rows.get(d)
            if row is not None:
                row.variance_minutes = net
                if row.actual_minutes is None and row.status == m.WEEKEND and extra:
                    row.actual_minutes = extra
                n_var += 1
            elif net != 0:
                if d.weekday() >= 5 or net > 0:
                    db.add(
                        m.DayStatus(
                            employee_id=emp.id, date=d,
                            status=m.WEEKEND if d.weekday() >= 5 else m.COMPLETE,
                            actual_minutes=extra or None, target_minutes=None,
                            variance_minutes=net, source="imported",
                            imported_token=f"tracker extra/short {extra or 0}/{short or 0}",
                        )
                    )
                    n_var += 1
                else:
                    # a weekday shortfall the compliance sheet never marked —
                    # do not fabricate strikes the sheet doesn't have
                    report["tracker_variance_unanchored"].append(
                        {"tab": tab, "date": d.isoformat(), "net_minutes": net}
                    )
    db.commit()
    print(f"  {n_leave} leave records, {n_var} variance merges")

    # ---- Divya task history + dropdown lists ------------------------------------
    n_entries = 0
    meta_path = TASK_CACHE + ".meta.json"
    if os.path.exists(meta_path):
        meta = json.load(open(meta_path))
        projects, task_types = {}, {}

        def get_project(name):
            name = name.strip() or "Others"
            if name not in projects:
                p = db.execute(select(m.Project).where(m.Project.name == name)).scalar_one_or_none()
                if p is None:
                    p = m.Project(name=name)
                    db.add(p)
                    db.flush()
                projects[name] = p
            return projects[name]

        def get_task(name):
            name = name.strip() or "Other Misc. Tasks"
            if name not in task_types:
                t = db.execute(select(m.TaskType).where(m.TaskType.name == name)).scalar_one_or_none()
                if t is None:
                    t = m.TaskType(name=name)
                    db.add(t)
                    db.flush()
                task_types[name] = t
            return task_types[name]

        # the hidden List sheet feeds the real dropdowns (PRD §4)
        for row in meta.get("other_sheets", {}).get("List", []):
            if not row:
                continue
            if len(row) >= 1 and len(row[0]) > 2:
                get_project(row[0])
            if len(row) >= 2 and len(row[1]) > 2:
                get_task(row[1])
        db.commit()
        print(f"  dropdowns from List sheet: {len(projects)} projects, {len(task_types)} tasks")

        divya_key = match_tab_to_person("Divya", set(emp_by_key))
        divya = emp_by_key.get(divya_key)
        if divya is not None and os.path.exists(TASK_CACHE + ".jsonl"):
            by_date = defaultdict(list)
            with open(TASK_CACHE + ".jsonl") as f:
                for line in f:
                    rec = json.loads(line)
                    by_date[rec["date"]].append(rec)
            for date_iso in sorted(by_date):
                d = dt.date.fromisoformat(date_iso)
                entries = normalize_day_times(by_date[date_iso])
                total = 0
                for e in entries:
                    db.add(
                        m.TaskEntry(
                            employee_id=divya.id, date=d,
                            project_id=get_project(e["project"]).id,
                            task_type_id=get_task(e["task"]).id,
                            details=e["details"] or "(imported, no details)",
                            start_minute=e["start_min"], end_minute=e["end_min"],
                            imported=True,
                        )
                    )
                    total += e["end_min"] - e["start_min"]
                    n_entries += 1
                if total > 0:
                    db.add(
                        m.DaySubmission(
                            employee_id=divya.id, date=d, total_minutes=total,
                            submitted_at=dt.datetime.combine(d, dt.time(18, 0)), locked=True,
                        )
                    )
            db.commit()
        print(f"  {n_entries} task entries for Divya")
    else:
        print("  (task cache missing — run legacy/extract_tasks.py first; skipping tasks)")

    # ---- admins + config ----------------------------------------------------------
    for name in ADMINS:
        key = norm(name)
        emp = emp_by_key.get(key)
        if emp is None:
            emp = m.Employee(name=name, notes="admin account (PRD §3)")
            db.add(emp)
            emp_by_key[key] = emp
        emp.is_admin = True
        emp.tracked = False
        emp.active = True
        if name == "Steve":
            emp.email = "steve.kennedy18@gmail.com"
    db.commit()

    for k, v in m.CONFIG_DEFAULTS.items():
        if db.get(m.Config, k) is None:
            db.add(m.Config(key=k, value=v))
    db.commit()
    db.get(m.Config, "live_start_date").value = TODAY.isoformat()
    db.commit()

    # ---- acceptance data + report ---------------------------------------------------
    sheet_strikes = {
        people[k].display: people[k].sheet_strikes for k in people if people[k].sheet_strikes
    }
    out = {
        "imported_at": dt.datetime.utcnow().isoformat(),
        "employees": len(emp_by_key),
        "day_statuses": n_status,
        "leave_records": n_leave,
        "task_entries": n_entries,
        "company_holidays": [d.isoformat() for d in sorted(company_holidays)],
        "sheet_strikes": sheet_strikes,
        "report": {k: v for k, v in report.items()},
    }
    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    with open(REPORT_PATH, "w") as f:
        json.dump(out, f, indent=1, default=str)

    db.add(
        m.AuditLog(
            actor="legacy-import", action="import",
            entity="all", detail=json.dumps({k: len(v) for k, v in report.items()}),
        )
    )
    db.commit()
    print(f"Import complete. Report: {REPORT_PATH}")
    for k, v in report.items():
        print(f"  report[{k}]: {len(v)} item(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
