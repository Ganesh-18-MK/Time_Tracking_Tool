"""Read-only reporting (Admin -> Reports: Attendance Reports / Strike Reports).

Both report types share the same cascading filter shape — Department ->
Employee -> Date range — and the same drill-down rule: pick "All Employees"
and you get one summary row per employee; pick one specific person and you
get their day-by-day detail instead. Everything here is built on top of the
existing engine.py primitives (DayStatus.effective_status, strikes_in,
recompute_all) rather than reimplementing any status/strike logic.
"""
import datetime as dt
from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import engine, models as m
from app.models import COMPLETE, HOLIDAY, LEAVE, MISSING, PARTIAL, STRIKE_STATUSES, WEEKEND
from app.util import fmt_hm, fmt_time, overtime_minutes, today_local

STATUS_ORDER = [COMPLETE, PARTIAL, MISSING, LEAVE, HOLIDAY, WEEKEND]
STATUS_LABELS = {
    COMPLETE: "Complete", PARTIAL: "Partial", MISSING: "Missing",
    LEAVE: "Leave", HOLIDAY: "Holiday", WEEKEND: "Weekend",
}

ALL_DEPARTMENTS = ""  # sentinel query-param value for "All Departments"
ALL_EMPLOYEES = 0     # sentinel query-param value for "All Employees"

# (key, label, days) — shown left-to-right as quick presets; "custom" is
# handled separately by the caller supplying explicit start/end dates.
RANGE_PRESETS = [
    ("7d", "Last 7 days", 7),
    ("30d", "Last month", 30),
    ("90d", "Last 3 months", 90),
    ("180d", "Last 6 months", 180),  # Time by Project/Task's month-trend view needs a longer default option than the other two reports; harmless extra choice for those too.
]
DEFAULT_RANGE = "7d"


def resolve_date_range(range_key: str, start: Optional[dt.date] = None,
                        end: Optional[dt.date] = None, today: Optional[dt.date] = None):
    """-> (start_date, end_date), inclusive. Falls back to the default
    preset if range_key is "custom" without both dates, or is unrecognized."""
    today = today or today_local()
    if range_key == "custom" and start and end:
        return (start, end) if start <= end else (end, start)
    for key, _label, days in RANGE_PRESETS:
        if key == range_key:
            return today - dt.timedelta(days=days - 1), today
    default_days = next(d for k, _l, d in RANGE_PRESETS if k == DEFAULT_RANGE)
    return today - dt.timedelta(days=default_days - 1), today


def _tracked_employees(db: Session) -> List[m.Employee]:
    return list(
        db.execute(
            select(m.Employee)
            .where(m.Employee.active.is_(True), m.Employee.tracked.is_(True))
            .order_by(m.Employee.name)
        ).scalars()
    )


def departments_list(db: Session) -> List[str]:
    return sorted({e.department or "—" for e in _tracked_employees(db)})


def employees_list(db: Session, department: Optional[str] = None) -> List[m.Employee]:
    emps = _tracked_employees(db)
    if department:
        emps = [e for e in emps if (e.department or "—") == department]
    return emps


def _empty_counts() -> Dict[str, int]:
    return {s: 0 for s in STATUS_ORDER}


def _ensure_fresh(db: Session, start: dt.date, end: dt.date) -> None:
    today = today_local()
    if start <= today:
        engine.recompute_all(db, start, min(end, today))


def _rows_by_employee(db: Session, start: dt.date, end: dt.date, employee_ids: List[int]) -> Dict[int, list]:
    if not employee_ids:
        return {}
    out: Dict[int, list] = {}
    for row in db.execute(
        select(m.DayStatus).where(
            m.DayStatus.employee_id.in_(employee_ids),
            m.DayStatus.date.between(start, end),
        )
    ).scalars():
        out.setdefault(row.employee_id, []).append(row)
    return out


def _punch_minutes_by_day(db: Session, start: dt.date, end: dt.date, employee_ids: List[int]) -> Dict[tuple, int]:
    """(employee_id, date) -> total *completed* Punch In/Out minutes that
    day. Only closed sessions count — a still-running punch (only possible
    for today, mid-day) isn't a finished fact yet, so it's excluded rather
    than guessed at. Feeds the Attendance Report's Overtime column; nothing
    here touches DayStatus/strikes."""
    if not employee_ids:
        return {}
    out: Dict[tuple, int] = {}
    for row in db.execute(
        select(m.PunchSession).where(
            m.PunchSession.employee_id.in_(employee_ids),
            m.PunchSession.date.between(start, end),
            m.PunchSession.punched_out_at.isnot(None),
        )
    ).scalars():
        key = (row.employee_id, row.date)
        out[key] = out.get(key, 0) + (row.duration_minutes or 0)
    return out


def _approved_overtime_ranges(db: Session, start: dt.date, end: dt.date,
                               employee_ids: List[int]) -> Dict[int, list]:
    """employee_id -> list of (start_date, end_date) OT_APPROVED ranges that
    overlap [start, end] at all (a range doesn't need to be fully inside the
    report window — a day-by-day check below only cares whether each
    specific day falls in one of these). Feeds the Attendance Report's
    "Approved overtime" figure (Ganesh's manager, 2026-08-03) — purely a
    payroll-visibility label, doesn't change the raw Overtime figure above,
    strikes, or compensation links, which all keep working off actual
    logged/punched hours regardless of approval status."""
    if not employee_ids:
        return {}
    out: Dict[int, list] = {}
    for row in db.execute(
        select(m.OvertimeApproval).where(
            m.OvertimeApproval.employee_id.in_(employee_ids),
            m.OvertimeApproval.status == m.OT_APPROVED,
            m.OvertimeApproval.start_date <= end,
            m.OvertimeApproval.end_date >= start,
        )
    ).scalars():
        out.setdefault(row.employee_id, []).append((row.start_date, row.end_date))
    return out


def _date_is_approved(ranges: list, d: dt.date) -> bool:
    return any(s <= d <= e for s, e in ranges)


def _scope_employees(db: Session, department: Optional[str], employee_id: Optional[int]) -> List[m.Employee]:
    emps = employees_list(db, department)
    if employee_id:
        emps = [e for e in emps if e.id == employee_id]
    return emps


def projects_list(db: Session) -> List[m.Project]:
    return list(
        db.execute(select(m.Project).where(m.Project.active.is_(True)).order_by(m.Project.name)).scalars()
    )


def task_types_list(db: Session) -> List[m.TaskType]:
    return list(
        db.execute(select(m.TaskType).where(m.TaskType.active.is_(True)).order_by(m.TaskType.name)).scalars()
    )


def _months_between(start: dt.date, end: dt.date) -> List[tuple]:
    """[(year, month), ...] for every calendar month touching [start, end],
    inclusive of both ends — the columns of the trend table below."""
    months = []
    y, mo = start.year, start.month
    while (y, mo) <= (end.year, end.month):
        months.append((y, mo))
        mo += 1
        if mo > 12:
            mo, y = 1, y + 1
    return months


def time_by_activity_report(
    db: Session, start: dt.date, end: dt.date,
    department: Optional[str] = None, employee_ids: Optional[List[int]] = None,
    project_ids: Optional[List[int]] = None, task_type_ids: Optional[List[int]] = None,
) -> dict:
    """Total logged time per employee, broken down by calendar month — one
    report answering both things Ganesh's manager asked for (2026-08-06):
    "total time spent on a Project or set of projects for one or a set of
    employees" (leave task_type_ids blank, pick project_ids) and "time spent
    on an activity per employee... trend... July vs August" (leave
    project_ids blank, pick task_type_ids — e.g. every member of a
    department against one Task). Both filters are optional and independent;
    picking both narrows to that exact project+task combination.

    Reads straight from TaskEntry (the same "computed, never typed"
    end_minute - start_minute duration everything else uses) — there's no
    DayStatus/strike concept for "time on a project", so unlike
    attendance_report/strikes_report there's no _ensure_fresh() recompute
    step here; TaskEntry rows are just summed as logged.

    {"months": [(year, month), ...], "rows": [{"employee", "department",
    "by_month": {(year, month): minutes}, "total": minutes}, ...] sorted by
    total descending (busiest first), "grand_total": minutes}. Employees
    with zero matching minutes still appear (with all-zero months) — the
    point of "for every member of Team A" is seeing who ISN'T logging time
    against something too, not just who is."""
    emps = employees_list(db, department)
    if employee_ids:
        wanted = set(employee_ids)
        emps = [e for e in emps if e.id in wanted]
    emp_ids = [e.id for e in emps]
    months = _months_between(start, end)

    by_emp_month: Dict[tuple, int] = {}
    if emp_ids:
        q = select(m.TaskEntry).where(
            m.TaskEntry.employee_id.in_(emp_ids),
            m.TaskEntry.date.between(start, end),
        )
        if project_ids:
            q = q.where(m.TaskEntry.project_id.in_(project_ids))
        if task_type_ids:
            q = q.where(m.TaskEntry.task_type_id.in_(task_type_ids))
        for row in db.execute(q).scalars():
            key = (row.employee_id, row.date.year, row.date.month)
            by_emp_month[key] = by_emp_month.get(key, 0) + row.duration_minutes

    rows = []
    grand_total = 0
    for e in emps:
        by_month = {}
        total = 0
        for ym in months:
            minutes = by_emp_month.get((e.id, ym[0], ym[1]), 0)
            by_month[ym] = minutes
            total += minutes
        rows.append({"employee": e, "department": e.department or "—", "by_month": by_month, "total": total})
        grand_total += total
    rows.sort(key=lambda r: -r["total"])
    return {"months": months, "rows": rows, "grand_total": grand_total}


def time_by_project_report(
    db: Session, start: dt.date, end: dt.date,
    department: Optional[str] = None, employee_ids: Optional[List[int]] = None,
    project_ids: Optional[List[int]] = None, task_type_ids: Optional[List[int]] = None,
) -> dict:
    """Companion view to time_by_activity_report() above, same page/same
    filters (Ganesh, 2026-08-24: "for each project how much time is taking
    and how time is splitting into each task of that particular project") —
    where that one answers "who worked on what, by month", this one answers
    "which project ate the time, and what was actually done on it". Same
    TaskEntry source, same department/employee/project/task filter
    semantics (department and employee_ids narrow *whose* time counts,
    project_ids/task_type_ids narrow *which* logged rows count at all) —
    deliberately kept identical to time_by_activity_report's filtering so
    the two tables on one page always describe the same slice of data.

    Unlike time_by_activity_report, a project with zero matching minutes
    is NOT included — there are ~300 Project/Employer rows org-wide
    (client names, mostly), so listing every one regardless of activity
    (the "show zero-minute employees too" convention that report uses,
    with ~45 employees) would make this table mostly empty rows instead
    of useful. Employee scoping still narrows *whose* time counts, same
    as above; it just doesn't force a zero row for an inactive project.

    {"projects": [{"project": Project, "total": minutes,
    "tasks": [{"task": TaskType, "minutes": minutes}, ...] sorted by
    minutes descending}, ...] sorted by total descending, "grand_total":
    minutes}."""
    emps = employees_list(db, department)
    if employee_ids:
        wanted = set(employee_ids)
        emps = [e for e in emps if e.id in wanted]
    emp_ids = [e.id for e in emps]

    by_project_task: Dict[tuple, int] = {}
    project_totals: Dict[int, int] = {}
    if emp_ids:
        q = select(m.TaskEntry).where(
            m.TaskEntry.employee_id.in_(emp_ids),
            m.TaskEntry.date.between(start, end),
        )
        if project_ids:
            q = q.where(m.TaskEntry.project_id.in_(project_ids))
        if task_type_ids:
            q = q.where(m.TaskEntry.task_type_id.in_(task_type_ids))
        for row in db.execute(q).scalars():
            key = (row.project_id, row.task_type_id)
            by_project_task[key] = by_project_task.get(key, 0) + row.duration_minutes
            project_totals[row.project_id] = project_totals.get(row.project_id, 0) + row.duration_minutes

    if not project_totals:
        return {"projects": [], "grand_total": 0, "chart_data": []}

    # One query for every Project/TaskType actually referenced, rather than
    # N+1 lookups per row above — projects_list()/task_types_list() only
    # return *active* ones, but a project logged against historically could
    # since have been deactivated, so this reads them directly by id instead.
    proj_rows = list(
        db.execute(select(m.Project).where(m.Project.id.in_(project_totals.keys()))).scalars()
    )
    projects_by_id = {p.id: p for p in proj_rows}
    task_ids = {tid for (_pid, tid) in by_project_task.keys()}
    task_rows = list(db.execute(select(m.TaskType).where(m.TaskType.id.in_(task_ids))).scalars())
    tasks_by_id = {t.id: t for t in task_rows}

    projects = []
    grand_total = 0
    for pid, total in project_totals.items():
        proj = projects_by_id.get(pid)
        if proj is None:  # deleted row, shouldn't happen (Project is soft-delete-only) but don't 500 on it
            continue
        tasks = [
            {"task": tasks_by_id[tid], "minutes": mins}
            for (ppid, tid), mins in by_project_task.items()
            if ppid == pid and tid in tasks_by_id
        ]
        tasks.sort(key=lambda t: -t["minutes"])
        projects.append({"project": proj, "total": total, "tasks": tasks})
        grand_total += total
    projects.sort(key=lambda p: -p["total"])
    # Plain (name, minutes) pairs, already sorted busiest-first — a
    # JSON-safe mirror of `projects` above for the pie chart's `|tojson`
    # (the Project/TaskType ORM objects in `projects` itself aren't
    # directly serializable). Kept as a return value rather than built in
    # the template so there's exactly one place that decides "busiest
    # first" ordering for this report.
    chart_data = [{"name": p["project"].name, "minutes": p["total"]} for p in projects]
    return {"projects": projects, "grand_total": grand_total, "chart_data": chart_data}


def time_filters_summary(
    db: Session, department: Optional[str], employee_ids: Optional[List[int]],
    project_ids: Optional[List[int]], task_type_ids: Optional[List[int]],
) -> dict:
    """Human-readable description of exactly which filters produced a given
    Time by Project/Task report (Ganesh's manager, 2026-08-06: "hard to
    tell... would you add something that shows the Employees/Depts/
    Projects/Tasks that were used" — the result table alone doesn't say
    whether a project filter was actually applied or everyone's showing).
    Resolves ids back to names so both the on-screen report and the
    exported file are self-describing even opened cold, out of context —
    which is the whole point ("helpful when viewing the data later")."""
    def _names(model, ids):
        if not ids:
            return None
        return [
            row.name for row in db.execute(
                select(model).where(model.id.in_(ids)).order_by(model.name)
            ).scalars()
        ]

    emp_names = _names(m.Employee, employee_ids)
    project_names = _names(m.Project, project_ids)
    task_names = _names(m.TaskType, task_type_ids)
    return {
        "department": department or "All Departments",
        "employees": ", ".join(emp_names) if emp_names else "All Employees",
        "projects": ", ".join(project_names) if project_names else "All Projects",
        "tasks": ", ".join(task_names) if task_names else "All Tasks",
    }


def time_kpis(result: dict, project_result: dict) -> List[dict]:
    """Report-level KPI tiles for the redesigned Time by Project/Task report
    (Ganesh, 2026-08-29) — pure post-processing over the two already-
    computed report dicts, no extra DB access. Unlike Attendance/Strikes
    this report has no day-by-day shape (it's month buckets), so it gets
    tiles only, no day-strip — the existing By Project / by-employee tables
    below stay as they are."""
    rows = result["rows"]
    if not rows:
        return []
    active = sum(1 for r in rows if r["total"] > 0)
    top_project = project_result["projects"][0] if project_result["projects"] else None
    return [
        {"label": "Total logged", "value": result["grand_total"], "hm": True, "variant": ""},
        {"label": "Employees with time", "value": active, "sub": f"of {len(rows)} in this filter", "variant": ""},
        {"label": "Projects touched", "value": len(project_result["projects"]), "variant": ""},
        {"label": "Top project", "value": top_project["project"].name if top_project else "—",
         "sub": (top_project["total"] // 60 and f"{top_project['total'] // 60}h {top_project['total'] % 60}m") if top_project else "",
         "variant": ""},
    ]


def attendance_report(db: Session, start: dt.date, end: dt.date,
                       department: Optional[str] = None, employee_id: Optional[int] = None) -> dict:
    """{"mode": "daily", "employee": Employee, "rows": [{"date","status","overtime"}]}
    when one specific employee is selected, else {"mode": "summary",
    "rows": [{"employee","department","counts","attendance_pct","overtime_minutes"}]}.

    "overtime" / "overtime_minutes" comes from completed Punch In/Out
    sessions vs. each day's already-computed target (DayStatus.target_minutes,
    which already includes leave and break-allowance adjustments) — see
    app/util.py overtime_minutes. Purely additive to the report; doesn't
    change status/strike counting at all.

    "approved_overtime" / "approved_overtime_minutes" is the same overtime
    figure, but only counting days that fall inside one of that employee's
    OT_APPROVED OvertimeApproval date ranges (Ganesh's manager, 2026-08-03)
    — a whole day's overtime counts as approved or it doesn't; approval is
    date-range granularity, not minute granularity. This is shown alongside
    the raw overtime figure, never in place of it — unapproved overtime
    still shows up in "overtime", it's just not counted as payable here."""
    _ensure_fresh(db, start, end)
    cfg = engine.get_config(db)
    comp_erases = cfg.get("comp_erases_strike") == "1"
    emps = _scope_employees(db, department, employee_id)
    emp_ids = [e.id for e in emps]
    by_emp = _rows_by_employee(db, start, end, emp_ids)
    punch_by_day = _punch_minutes_by_day(db, start, end, emp_ids)
    approved_ranges = _approved_overtime_ranges(db, start, end, emp_ids)

    if employee_id and len(emps) == 1:
        emp = emps[0]
        rows = sorted(by_emp.get(emp.id, []), key=lambda r: r.date)
        emp_ranges = approved_ranges.get(emp.id, [])
        daily = [
            {
                "date": r.date,
                "status": r.effective_status(comp_erases),
                "overtime": overtime_minutes(punch_by_day.get((emp.id, r.date), 0), r.target_minutes),
                "approved_overtime": (
                    overtime_minutes(punch_by_day.get((emp.id, r.date), 0), r.target_minutes)
                    if _date_is_approved(emp_ranges, r.date) else 0
                ),
            }
            for r in rows
        ]
        return {"mode": "daily", "employee": emp, "rows": daily}

    summary = []
    for e in emps:
        counts = _empty_counts()
        emp_overtime = emp_approved_overtime = 0
        emp_ranges = approved_ranges.get(e.id, [])
        # daily: same {date, status} shape as daily-detail mode's own rows,
        # kept here too (Ganesh, 2026-08-29) so the summary table's
        # day-strip mini calendar doesn't need a second query — this
        # report already loads every DayStatus row in range per employee
        # via by_emp above, this just doesn't throw the per-day detail away.
        daily = []
        emp_rows = sorted(by_emp.get(e.id, []), key=lambda r: r.date)
        for r in emp_rows:
            eff = r.effective_status(comp_erases)
            if eff in counts:
                counts[eff] += 1
            day_overtime = overtime_minutes(punch_by_day.get((e.id, r.date), 0), r.target_minutes)
            emp_overtime += day_overtime
            if _date_is_approved(emp_ranges, r.date):
                emp_approved_overtime += day_overtime
            daily.append({"date": r.date, "status": eff})
        expected = counts[COMPLETE] + counts[PARTIAL] + counts[MISSING]
        pct = round(100 * counts[COMPLETE] / expected, 1) if expected else None
        summary.append({
            "employee": e, "department": e.department or "—", "counts": counts,
            "attendance_pct": pct, "overtime_minutes": emp_overtime,
            "approved_overtime_minutes": emp_approved_overtime, "daily": daily,
        })
    # Rows needing action sort to the top (Ganesh, 2026-08-29, matching the
    # mockup) — anyone with at least one Missing day in range first, then
    # alphabetical within each group; a stable sort so the alphabetical
    # order from employees_list() above is preserved as the tiebreaker.
    summary.sort(key=lambda r: (0 if r["counts"][MISSING] > 0 else 1, r["employee"].name))
    return {"mode": "summary", "rows": summary}


def attendance_kpis(result: dict) -> List[dict]:
    """Report-level KPI tiles for the redesigned Attendance report (Ganesh,
    2026-08-29, matching a pasted mockup) — pure post-processing over
    attendance_report()'s own already-computed result, no extra DB access.
    Summary mode gets team-wide tiles; daily mode (one employee selected)
    gets that person's own. Returns [] for an empty result rather than
    tiles full of zeroes-that-look-like-real-zeroes."""
    if result["mode"] == "daily":
        rows = result["rows"]
        if not rows:
            return []
        counts = _empty_counts()
        overtime = 0
        for r in rows:
            if r["status"] in counts:
                counts[r["status"]] += 1
            overtime += r["overtime"] or 0
        expected = counts[COMPLETE] + counts[PARTIAL] + counts[MISSING]
        pct = round(100 * counts[COMPLETE] / expected, 1) if expected else None
        return [
            {"label": "Attendance", "value": f"{pct}%" if pct is not None else "—", "variant": ""},
            {"label": "Days missing", "value": counts[MISSING],
             "variant": "alertk" if counts[MISSING] else ""},
            {"label": "Days partial", "value": counts[PARTIAL],
             "variant": "warnk" if counts[PARTIAL] else ""},
            {"label": "Overtime logged", "value": overtime, "hm": True, "variant": ""},
        ]

    rows = result["rows"]
    if not rows:
        return []
    total_counts = _empty_counts()
    total_overtime = 0
    employees_missing = 0
    with_department = 0
    for r in rows:
        for s, n in r["counts"].items():
            total_counts[s] += n
        total_overtime += r["overtime_minutes"]
        if r["counts"][MISSING] > 0:
            employees_missing += 1
        if r["department"] != "—":
            with_department += 1
    expected = total_counts[COMPLETE] + total_counts[PARTIAL] + total_counts[MISSING]
    team_pct = round(100 * total_counts[COMPLETE] / expected, 1) if expected else None
    return [
        {"label": "Team attendance", "value": f"{team_pct}%" if team_pct is not None else "—", "variant": ""},
        {"label": "Days not logged", "value": total_counts[MISSING],
         "sub": f"across {employees_missing} employee{'s' if employees_missing != 1 else ''}" if total_counts[MISSING] else "",
         "variant": "alertk" if total_counts[MISSING] else ""},
        {"label": "Overtime logged", "value": total_overtime, "hm": True, "variant": ""},
        {"label": "Department set", "value": f"{with_department} / {len(rows)}",
         "sub": f"{len(rows) - with_department} profile{'s' if len(rows) - with_department != 1 else ''} incomplete" if with_department < len(rows) else "",
         "variant": "warnk" if with_department < len(rows) else ""},
    ]


def task_log_overtime_report(db: Session, start: dt.date, end: dt.date, employee_ids: List[int]) -> List[dict]:
    """Who worked overtime, driven by logged TASK-LOG hours
    (DayStatus.variance_minutes — the same "surplus" figure the
    Compensation Links picker already sums per employee) instead of
    completed Punch In/Out time. Backs Overtime Management's "Who worked
    overtime" table (Ganesh, 2026-08-25: "overtime from punch in punched
    out time is not requere... we are considering only based on task log
    times") — a deliberately separate function from attendance_report()
    rather than a flag on it, since Reports -> Attendance's own "Overtime"
    column stays Punch-based on purpose (that's what its docstring/PRD
    grounding is actually about); this answers a different question ("who
    logged more task-log hours than their target") for this one table only.

    Reuses _approved_overtime_ranges()/_date_is_approved() unchanged — the
    approved/not-yet-approved split is still whole-day, OvertimeApproval-
    range based, just applied against variance-minutes instead of
    punch-minutes. Employees with zero task-log surplus in range are
    excluded (same as the table's previous Punch-based behavior), and
    takes an explicit employee_ids list rather than department/employee_id
    like attendance_report() does, since Overtime Management scopes by
    led_by() (per-person "reports to me"), not by department."""
    if not employee_ids:
        return []
    _ensure_fresh(db, start, end)
    approved_ranges = _approved_overtime_ranges(db, start, end, employee_ids)
    by_emp = _rows_by_employee(db, start, end, employee_ids)
    rows = []
    for emp_id, statuses in by_emp.items():
        total = 0
        approved_total = 0
        emp_ranges = approved_ranges.get(emp_id, [])
        emp = None
        for r in statuses:
            emp = r.employee
            v = r.variance_minutes or 0
            if v <= 0:
                continue
            total += v
            if _date_is_approved(emp_ranges, r.date):
                approved_total += v
        if total > 0 and emp is not None:
            rows.append({
                "employee": emp, "department": emp.department or "—",
                "overtime_minutes": total, "approved_overtime_minutes": approved_total,
            })
    rows.sort(key=lambda r: r["overtime_minutes"], reverse=True)
    return rows


def strikes_report(db: Session, start: dt.date, end: dt.date,
                    department: Optional[str] = None, employee_id: Optional[int] = None) -> dict:
    """{"mode": "daily", "employee": Employee, "rows": [{"date","status"}],
    "total": int} (the actual strike days) when one specific employee is
    selected, else {"mode": "summary", "rows": [{"employee","department",
    "strikes"}]} sorted worst-first."""
    _ensure_fresh(db, start, end)
    cfg = engine.get_config(db)
    comp_erases = cfg.get("comp_erases_strike") == "1"
    emps = _scope_employees(db, department, employee_id)
    by_emp = _rows_by_employee(db, start, end, [e.id for e in emps])

    if employee_id and len(emps) == 1:
        emp = emps[0]
        rows = sorted(by_emp.get(emp.id, []), key=lambda r: r.date)
        strike_days = [
            {"date": r.date, "status": r.effective_status(comp_erases)}
            for r in rows
            if r.effective_status(comp_erases) in STRIKE_STATUSES and not r.strike_exempt
        ]
        return {"mode": "daily", "employee": emp, "rows": strike_days, "total": len(strike_days)}

    summary = []
    for e in emps:
        emp_rows = sorted(by_emp.get(e.id, []), key=lambda r: r.date)
        strikes = engine.strikes_in(emp_rows, comp_erases)
        # daily: same day-strip shape as the redesigned Attendance report
        # (Ganesh, 2026-08-29) — a strike day gets its real status color
        # (missing/partial), everything else collapses to "none" so the
        # strip reads as "here's where the strikes are", not a second copy
        # of the Attendance report's own strip.
        daily = [
            {"date": r.date,
             "status": r.effective_status(comp_erases) if (r.effective_status(comp_erases) in STRIKE_STATUSES and not r.strike_exempt) else "none"}
            for r in emp_rows
        ]
        summary.append({"employee": e, "department": e.department or "—", "strikes": strikes, "daily": daily})
    summary.sort(key=lambda r: -r["strikes"])
    return {"mode": "summary", "rows": summary}


def strikes_kpis(result: dict) -> List[dict]:
    """Report-level KPI tiles for the redesigned Strikes report (Ganesh,
    2026-08-29) — same pure-post-processing shape as attendance_kpis()."""
    if result["mode"] == "daily":
        return [{"label": "Strikes in range", "value": result["total"],
                  "variant": "alertk" if result["total"] else ""}]
    rows = result["rows"]
    if not rows:
        return []
    total_strikes = sum(r["strikes"] for r in rows)
    with_strikes = sum(1 for r in rows if r["strikes"] > 0)
    worst = max(rows, key=lambda r: r["strikes"]) if total_strikes else None
    return [
        {"label": "Total strikes", "value": total_strikes, "variant": "alertk" if total_strikes else ""},
        {"label": "Employees with strikes", "value": with_strikes,
         "sub": f"of {len(rows)} tracked", "variant": "warnk" if with_strikes else ""},
        {"label": "Worst this range", "value": f"{worst['employee'].name} ({worst['strikes']})" if worst else "—",
         "variant": ""},
    ]


# ---- Developer Usage Report (Ganesh, 2026-08-21) -----------------------------
def feature_usage_report(db: Session, start: dt.date, end: dt.date) -> dict:
    """Adoption of the 3 ways to log a task row — Plan for the Day / Auto
    time capture / Add Row, see app/models.py's ENTRY_METHOD_* and
    TaskEntry.entry_method — plus Punch In/Out, over a date range.

    'Adoption' here deliberately means "% of active tracked employees who
    used it at least once in the range," not "% of rows by method" (a
    Ganesh decision, 2026-08-21) — the question was about how many
    PEOPLE have picked up a given way of working, not which method
    produces more rows once someone has. `entry_method` is only stamped
    on rows created from 2026-08-21 onward (see TaskEntry's docstring) —
    every older/imported row has entry_method NULL and is correctly
    excluded from every method's count here, not folded into any one of
    them; a date range entirely before that ships everyone at 0%, which
    is accurate, not a bug to work around."""
    emps = _tracked_employees(db)
    emp_ids = [e.id for e in emps]
    total = len(emp_ids)

    used_by_method: Dict[str, set] = {k: set() for k in m.ENTRY_METHODS}
    if emp_ids:
        rows = db.execute(
            select(m.TaskEntry.employee_id, m.TaskEntry.entry_method)
            .where(
                m.TaskEntry.employee_id.in_(emp_ids),
                m.TaskEntry.date.between(start, end),
                m.TaskEntry.entry_method.isnot(None),
            )
            .distinct()
        ).all()
        for emp_id, method in rows:
            if method in used_by_method:
                used_by_method[method].add(emp_id)

    used_punch: set = set()
    if emp_ids:
        punch_rows = db.execute(
            select(m.PunchSession.employee_id)
            .where(m.PunchSession.employee_id.in_(emp_ids), m.PunchSession.date.between(start, end))
            .distinct()
        ).all()
        used_punch = {r[0] for r in punch_rows}

    def _pct(n: int) -> float:
        return round(100 * n / total, 1) if total else 0.0

    methods = [
        {
            "key": k,
            "label": m.ENTRY_METHOD_LABELS[k],
            "count": len(used_by_method[k]),
            "pct": _pct(len(used_by_method[k])),
        }
        for k in m.ENTRY_METHODS
    ]
    punch = {"count": len(used_punch), "pct": _pct(len(used_punch))}

    employee_rows = [
        {
            "employee": e,
            "used": {k: e.id in used_by_method[k] for k in m.ENTRY_METHODS},
            "punch": e.id in used_punch,
        }
        for e in emps
    ]

    return {
        "total_employees": total,
        "methods": methods,
        "punch": punch,
        "employees": employee_rows,
    }


# ---- Task Logs Report (Ganesh, 2026-08-29) -----------------------------------
# Employee-wise raw task log, redesigned per a pasted mockup: each day's real
# TaskEntry rows (the messy free-typed "1.did X 2.did Y" details admins were
# struggling to read) plus an "unplanned" flag on any entry that doesn't
# match something the employee actually planned that day, plus an optional
# LLM-generated 4-5 bullet summary. Same "All Employees -> summary, one
# person -> day-by-day detail" drill-down convention as Attendance/Strikes
# above, and deliberately so: it's also what keeps this report's LLM cost
# bounded — summaries are only ever generated for the one employee/day an
# admin has actually opened, one day at a time via an explicit "Generate"
# click (see /admin/reports/tasklogs/summarize in app/routes/reports.py),
# never eagerly for a whole date range on page load. A synchronous request
# calling an LLM 30 times in a loop for "last month" would be slow and
# expensive for no reason nobody asked for yet.
def _planned_pairs_by_date(db: Session, employee_id: int, start: dt.date, end: dt.date) -> Dict[dt.date, set]:
    """date -> set of (project_id, task_type_id) this employee had ANY
    PlannedTask for that day, any status (planned/running/paused/done) —
    "did they plan this project+task at all today", not "is it still
    sitting unstarted". Carried-forward copies (PlannedTask.carried_at,
    see app/models.py) land on their own new date already, so they're
    naturally counted on the day they actually apply to."""
    out: Dict[dt.date, set] = {}
    for row in db.execute(
        select(m.PlannedTask.date, m.PlannedTask.project_id, m.PlannedTask.task_type_id).where(
            m.PlannedTask.employee_id == employee_id,
            m.PlannedTask.date.between(start, end),
        )
    ).all():
        d, pid, tid = row
        out.setdefault(d, set()).add((pid, tid))
    return out


def rule_based_day_summary(rows: list) -> List[str]:
    """Deterministic, no-API-call replacement for the original LLM-backed
    summary (Ganesh, 2026-08-29: "without LLM cant we generate summary" —
    answered yes, then asked to replace it outright rather than keep both).
    One bullet per project worked that day, naming the project, its total
    time, and the distinct tasks logged under it — this satisfies the
    original ask ("4-5 bullets highlighting project names") without any
    network call, API key, cost, or failure mode, at the cost of reading
    as a flat list rather than natural prose. Capped at 5 project bullets
    (a 6th+ collapses into one "+N more projects" line) — same top-N-plus-
    other instinct the "By Project" pie chart already uses, so a day spread
    across many small projects doesn't produce an unreadably long list.
    Returns [] for a day with no entries (nothing to summarize) rather than
    None — there's no "generation failed" state anymore, so callers/
    templates never need to distinguish "empty" from "errored"."""
    if not rows:
        return []
    order: List[int] = []
    by_project: Dict[int, dict] = {}
    for r in sorted(rows, key=lambda r: r.start_minute):
        pid = r.project_id
        if pid not in by_project:
            order.append(pid)
            by_project[pid] = {"name": r.project.name if r.project else "—", "minutes": 0, "tasks": []}
        entry = by_project[pid]
        entry["minutes"] += r.duration_minutes
        task_name = r.task_type.name if r.task_type else "—"
        if task_name not in entry["tasks"]:
            entry["tasks"].append(task_name)
    bullets = []
    TOP_N = 5
    shown = order[:TOP_N]
    for pid in shown:
        p = by_project[pid]
        bullets.append(f"{p['name']} ({fmt_hm(p['minutes'])}): {', '.join(p['tasks'])}")
    remaining = order[TOP_N:]
    if remaining:
        extra_minutes = sum(by_project[pid]["minutes"] for pid in remaining)
        bullets.append(f"+{len(remaining)} more project(s) ({fmt_hm(extra_minutes)})")
    return bullets


def daily_task_log_report(db: Session, start: dt.date, end: dt.date,
                           department: Optional[str] = None, employee_id: Optional[int] = None) -> dict:
    """{"mode": "daily", "employee": Employee, "days": [{"date", "entries":
    [{"entry": TaskEntry, "unplanned": bool}, ...], "unplanned_count",
    "total_minutes", "summary": {"source": "ai" | "rule", "text": str | None,
    "bullets": List[str] | None, "error": str | None}}, ...]} when one
    employee is selected (most recent day first — unlike
    Attendance/Strikes' oldest-first daily mode, admins open this one to
    read today's or yesterday's actual work, not audit a whole range in
    order), else {"mode": "summary", "rows": [{"employee", "department",
    "entries", "unplanned_count", "days_logged"}, ...]} sorted by
    unplanned_count descending (busiest-to-flag first) — no LLM calls at
    all in summary mode, see this section's own module-level comment."""
    emps = _scope_employees(db, department, employee_id)
    emp_ids = [e.id for e in emps]
    if not emp_ids:
        return {"mode": "daily" if employee_id else "summary", "employee": None, "days": [], "rows": []}

    entries = list(
        db.execute(
            select(m.TaskEntry).where(
                m.TaskEntry.employee_id.in_(emp_ids),
                m.TaskEntry.date.between(start, end),
            )
        ).scalars()
    )
    by_emp_date: Dict[tuple, list] = {}
    for r in entries:
        by_emp_date.setdefault((r.employee_id, r.date), []).append(r)
    planned_by_emp: Dict[int, Dict[dt.date, set]] = {
        e.id: _planned_pairs_by_date(db, e.id, start, end) for e in emps
    }

    if employee_id and len(emps) == 1:
        emp = emps[0]
        dates = sorted({d for (eid, d) in by_emp_date if eid == emp.id}, reverse=True)
        planned = planned_by_emp[emp.id]
        # AI day summaries (Ganesh, 2026-08-31) — stored once per day on
        # DaySubmission at Submit Day time (see llm_summary.py +
        # submit_day() in app/routes/employee.py), one query for the whole
        # visible range rather than one per day. A day with no
        # DaySubmission row at all (not yet submitted, or from before this
        # feature existed) has nothing here and falls through to
        # rule_based_day_summary() below exactly as before this feature.
        subs_by_date = {
            row.date: row
            for row in db.execute(
                select(m.DaySubmission).where(
                    m.DaySubmission.employee_id == emp.id,
                    m.DaySubmission.date.between(start, end),
                )
            ).scalars()
        }
        days = []
        for d in dates:
            rows = sorted(by_emp_date[(emp.id, d)], key=lambda r: r.start_minute)
            planned_pairs = planned.get(d, set())
            day_entries = [
                {"entry": r, "unplanned": (r.project_id, r.task_type_id) not in planned_pairs}
                for r in rows
            ]
            sub = subs_by_date.get(d)
            if sub and sub.summary_text:
                summary = {"source": "ai", "text": sub.summary_text, "bullets": None, "error": None}
            else:
                # Surface WHY there's no AI summary (Ganesh, 2026-08-31,
                # after seeing a submitted day still show only the
                # rule-based bullets with no way to tell whether that's
                # because GEMINI_API_KEY isn't set, the day predates this
                # feature, or the call actually failed) — sub.summary_error
                # is set by _generate_day_summary() (app/routes/
                # employee.py) every time summarize_day() doesn't return
                # text, including "GEMINI_API_KEY not set" itself, so this
                # one field answers all three cases without digging into
                # the database. None (not just falsy) specifically means
                # "no DaySubmission row for this day at all" — a day from
                # before this feature existed, or one that was never
                # actually submitted through submit_day() (e.g. seeded
                # demo data) — which reads differently from a real,
                # attempted-and-failed generation.
                summary = {
                    "source": "rule", "text": None,
                    "bullets": rule_based_day_summary(rows),
                    "error": sub.summary_error if sub else None,
                }
            days.append({
                "date": d,
                "entries": day_entries,
                "unplanned_count": sum(1 for e in day_entries if e["unplanned"]),
                "total_minutes": sum(r.duration_minutes for r in rows),
                "summary": summary,
            })
        return {"mode": "daily", "employee": emp, "days": days}

    summary = []
    for e in emps:
        e_dates = {d for (eid, d) in by_emp_date if eid == e.id}
        total_entries = 0
        unplanned = 0
        for d in e_dates:
            rows = by_emp_date[(e.id, d)]
            planned_pairs = planned_by_emp[e.id].get(d, set())
            total_entries += len(rows)
            unplanned += sum(1 for r in rows if (r.project_id, r.task_type_id) not in planned_pairs)
        summary.append({
            "employee": e, "department": e.department or "—",
            "entries": total_entries, "unplanned_count": unplanned, "days_logged": len(e_dates),
        })
    summary.sort(key=lambda r: -r["unplanned_count"])
    return {"mode": "summary", "rows": summary}


def task_log_export_rows(db: Session, start: dt.date, end: dt.date,
                          department: Optional[str] = None, employee_id: Optional[int] = None) -> List[dict]:
    """Flat, ungrouped rows behind both Task Logs downloads (Ganesh,
    2026-08-29: "i want download report option as well like project wise
    and employee wise") — one dict per TaskEntry in the filtered
    department/employee/date-range scope, with the same "unplanned" rule
    daily_task_log_report() uses (no matching PlannedTask that exact date).
    The two .xlsx export routes in app/routes/reports.py just sort/group
    this same list two different ways (by employee, by project) rather than
    running two independent queries that could quietly drift apart."""
    emps = _scope_employees(db, department, employee_id)
    emp_ids = [e.id for e in emps]
    if not emp_ids:
        return []
    entries = list(
        db.execute(
            select(m.TaskEntry)
            .where(m.TaskEntry.employee_id.in_(emp_ids), m.TaskEntry.date.between(start, end))
            .order_by(m.TaskEntry.date, m.TaskEntry.start_minute)
        ).scalars()
    )
    planned_by_emp: Dict[int, Dict[dt.date, set]] = {
        e.id: _planned_pairs_by_date(db, e.id, start, end) for e in emps
    }
    rows = []
    for r in entries:
        planned_pairs = planned_by_emp.get(r.employee_id, {}).get(r.date, set())
        rows.append({
            "employee": r.employee, "date": r.date, "project": r.project, "task": r.task_type,
            "details": r.details, "client": r.client,
            "start_minute": r.start_minute, "end_minute": r.end_minute,
            "duration_minutes": r.duration_minutes,
            "unplanned": (r.project_id, r.task_type_id) not in planned_pairs,
        })
    return rows


def compliance_trend_report(
    db: Session, emps: List[m.Employee], weeks: int, threshold: int,
    comp_erases: bool, today: dt.date, target_pct: int,
) -> dict:
    """Dashboard's "Compliance Trend" card (Ganesh, 2026-08-30, from a pasted
    mockup: a weekly line chart, a dashed "Target N%" reference line, the
    current week's point called out, and a one-line explanation whenever
    the most recent week is a drop from the one before). Flagged as a
    follow-up when the "By Project" bar chart's own mockup pair was built
    the same day ("needs a week-over-week historical rollup nothing in
    this app computes yet") — this is that rollup.

    `emps` is whatever employee set the caller has already scoped (Dashboard
    passes its own `all_emps` — org-wide for a Super Admin, department-only
    for a Team Lead, matching the mockup's "your teams" framing either way).

    Weeks are Monday-Sunday, same convention as
    app/routes/employee.py's `_week_summary()`. The most recent week is
    "this week" — Monday through `today`, i.e. possibly partial — matching
    Dashboard's own live "today" snapshot elsewhere on the page; every
    earlier week is Monday through Sunday in full.

    "Compliant" for one employee in one week is exactly My Month's own
    compliant/at-risk definition (`strikes_in(that week's rows, comp_erases)
    < threshold`), just evaluated over a single week's DayStatus rows
    instead of a whole month's — reusing the existing Config strike
    threshold rather than inventing a second, week-scoped one. Since
    `strike_threshold` (default 5) was designed as a monthly tolerance, a
    single bad day rarely pushes a week over it on its own — this trend is
    a signal for genuinely serious weeks, not a sensitive day-to-day
    tracker, and it will often sit near 100%. That's an accepted
    consequence of reusing the existing config value (confirmed with
    Ganesh) rather than a bug.

    Known, accepted gap: an employee with zero DayStatus rows in a given
    week (e.g. a week before they were hired, or one recompute_all() hasn't
    reached) reads as compliant for that week (strikes_in([]) == 0) — the
    same "vacuous truth" reporting.py's other functions don't special-case
    for pre-hire dates either. At this org's size/tenure mix the effect is
    small; flagged here rather than silently assumed away.

    Root cause ("N people in {department} account for the whole drop") is
    computed for real, not guessed: whoever was compliant last week but
    ISN'T this week is the "newly non-compliant" set; if every one of them
    sits in the same department, name it and the count exactly like the
    mockup. If they're merely concentrated (>50%) in one department, say so
    without claiming the whole drop. Otherwise, say the drop is spread out
    rather than naming a department that wouldn't actually explain most of
    it. No note at all when the most recent week isn't a drop (pct held or
    rose) — a card that only speaks up when something needs attention is
    the same instinct behind "Needs attention" elsewhere on this page.

    {"points": [{"week_start", "week_end", "pct", "compliant", "total"}, ...]
    oldest-first ending with "this week" (real dt.date objects — for any
    future server-side/prose use), "chart_data": the same points as plain
    JSON-safe {"label", "pct", "compliant", "total"} dicts for the
    template's `|tojson` (see time_by_project_report()'s own chart_data for
    the same pattern), "target_pct": int, "drop_note": str | None}."""
    if not emps:
        return {"points": [], "chart_data": [], "target_pct": target_pct, "drop_note": None}

    emp_ids = [e.id for e in emps]
    this_monday = today - dt.timedelta(days=today.weekday())
    week_starts = [this_monday - dt.timedelta(weeks=(weeks - 1 - i)) for i in range(weeks)]
    range_start = week_starts[0]

    rows_by_emp: Dict[int, List[m.DayStatus]] = {}
    for row in db.execute(
        select(m.DayStatus).where(
            m.DayStatus.employee_id.in_(emp_ids),
            m.DayStatus.date.between(range_start, today),
        )
    ).scalars():
        rows_by_emp.setdefault(row.employee_id, []).append(row)

    points = []
    compliant_sets: List[set] = []
    for wk_start in week_starts:
        wk_end = min(wk_start + dt.timedelta(days=6), today)
        compliant_ids = set()
        for e in emps:
            wk_rows = [r for r in rows_by_emp.get(e.id, []) if wk_start <= r.date <= wk_end]
            if engine.strikes_in(wk_rows, comp_erases) < threshold:
                compliant_ids.add(e.id)
        pct = round(len(compliant_ids) / len(emps) * 100, 1)
        points.append({
            "week_start": wk_start, "week_end": wk_end, "pct": pct,
            "compliant": len(compliant_ids), "total": len(emps),
        })
        compliant_sets.append(compliant_ids)

    drop_note = None
    if len(points) >= 2 and points[-1]["pct"] < points[-2]["pct"]:
        dropped_ids = compliant_sets[-2] - compliant_sets[-1]
        if dropped_ids:
            emp_by_id = {e.id: e for e in emps}
            by_dept: Dict[str, int] = {}
            for eid in dropped_ids:
                dept = emp_by_id[eid].department or "—"
                by_dept[dept] = by_dept.get(dept, 0) + 1
            total_dropped = len(dropped_ids)
            top_dept, top_n = max(by_dept.items(), key=lambda kv: kv[1])
            noun = "person" if top_n == 1 else "people"
            if top_n == total_dropped:
                drop_note = f"{top_n} {noun} in {top_dept} account for the whole drop."
            elif top_n > total_dropped / 2:
                drop_note = f"{top_n} of {total_dropped} newly non-compliant people are in {top_dept}."
            else:
                drop_note = f"Spread across {len(by_dept)} departments — no single team stands out."

    # Plain (label, pct) pairs, oldest-first — a JSON-safe mirror of `points`
    # above for the chart's `|tojson` (dt.date objects in `points` itself
    # aren't directly serializable — same reasoning as time_by_project_
    # report()'s own chart_data next to its ORM-referencing `projects`).
    chart_data = [{"label": p["week_start"].strftime("%b %d"), "pct": p["pct"],
                   "compliant": p["compliant"], "total": p["total"]} for p in points]
    return {"points": points, "chart_data": chart_data, "target_pct": target_pct, "drop_note": drop_note}
