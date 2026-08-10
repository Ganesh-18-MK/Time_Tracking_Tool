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
from app.util import overtime_minutes, today_local

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
        for r in by_emp.get(e.id, []):
            eff = r.effective_status(comp_erases)
            if eff in counts:
                counts[eff] += 1
            day_overtime = overtime_minutes(punch_by_day.get((e.id, r.date), 0), r.target_minutes)
            emp_overtime += day_overtime
            if _date_is_approved(emp_ranges, r.date):
                emp_approved_overtime += day_overtime
        expected = counts[COMPLETE] + counts[PARTIAL] + counts[MISSING]
        pct = round(100 * counts[COMPLETE] / expected, 1) if expected else None
        summary.append({
            "employee": e, "department": e.department or "—", "counts": counts,
            "attendance_pct": pct, "overtime_minutes": emp_overtime,
            "approved_overtime_minutes": emp_approved_overtime,
        })
    return {"mode": "summary", "rows": summary}


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
        strikes = engine.strikes_in(by_emp.get(e.id, []), comp_erases)
        summary.append({"employee": e, "department": e.department or "—", "strikes": strikes})
    summary.sort(key=lambda r: -r["strikes"])
    return {"mode": "summary", "rows": summary}
