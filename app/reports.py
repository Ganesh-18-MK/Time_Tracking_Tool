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
from app.util import overtime_minutes

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
]
DEFAULT_RANGE = "7d"


def resolve_date_range(range_key: str, start: Optional[dt.date] = None,
                        end: Optional[dt.date] = None, today: Optional[dt.date] = None):
    """-> (start_date, end_date), inclusive. Falls back to the default
    preset if range_key is "custom" without both dates, or is unrecognized."""
    today = today or dt.date.today()
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
    today = dt.date.today()
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


def _scope_employees(db: Session, department: Optional[str], employee_id: Optional[int]) -> List[m.Employee]:
    emps = employees_list(db, department)
    if employee_id:
        emps = [e for e in emps if e.id == employee_id]
    return emps


def attendance_report(db: Session, start: dt.date, end: dt.date,
                       department: Optional[str] = None, employee_id: Optional[int] = None) -> dict:
    """{"mode": "daily", "employee": Employee, "rows": [{"date","status","overtime"}]}
    when one specific employee is selected, else {"mode": "summary",
    "rows": [{"employee","department","counts","attendance_pct","overtime_minutes"}]}.

    "overtime" / "overtime_minutes" comes from completed Punch In/Out
    sessions vs. each day's already-computed target (DayStatus.target_minutes,
    which already includes leave and break-allowance adjustments) — see
    app/util.py overtime_minutes. Purely additive to the report; doesn't
    change status/strike counting at all."""
    _ensure_fresh(db, start, end)
    cfg = engine.get_config(db)
    comp_erases = cfg.get("comp_erases_strike") == "1"
    emps = _scope_employees(db, department, employee_id)
    emp_ids = [e.id for e in emps]
    by_emp = _rows_by_employee(db, start, end, emp_ids)
    punch_by_day = _punch_minutes_by_day(db, start, end, emp_ids)

    if employee_id and len(emps) == 1:
        emp = emps[0]
        rows = sorted(by_emp.get(emp.id, []), key=lambda r: r.date)
        daily = [
            {
                "date": r.date,
                "status": r.effective_status(comp_erases),
                "overtime": overtime_minutes(punch_by_day.get((emp.id, r.date), 0), r.target_minutes),
            }
            for r in rows
        ]
        return {"mode": "daily", "employee": emp, "rows": daily}

    summary = []
    for e in emps:
        counts = _empty_counts()
        emp_overtime = 0
        for r in by_emp.get(e.id, []):
            eff = r.effective_status(comp_erases)
            if eff in counts:
                counts[eff] += 1
            emp_overtime += overtime_minutes(punch_by_day.get((e.id, r.date), 0), r.target_minutes)
        expected = counts[COMPLETE] + counts[PARTIAL] + counts[MISSING]
        pct = round(100 * counts[COMPLETE] / expected, 1) if expected else None
        summary.append({
            "employee": e, "department": e.department or "—", "counts": counts,
            "attendance_pct": pct, "overtime_minutes": emp_overtime,
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
