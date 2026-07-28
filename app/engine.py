"""Status / variance / strike engine (PRD §5–§6).

Everything compliance-related is computed, never typed. The one exception is
admin overrides, which live on DayStatus and always win.

Rules implemented:
  * Complete  — day submitted, actual >= target - tolerance
  * Partial   — day submitted, actual < target - tolerance
  * Missing   — past working day, no submission, no covering leave
  * Leave     — approved leave covers the full (possibly reduced) target
  * Holiday / Weekend — non-working day; logged hours still count as surplus
  * variance = actual - effective_target (leave reduces the target)
  * strikes(month) = count(effective status in {Missing, Partial})
  * Imported legacy rows (source='imported') are frozen fact: never recomputed.
  * Today is never marked Missing (the day isn't over); it only gets a status
    once submitted, on leave, or imported.
"""
import datetime as dt
import json
from typing import Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import models as m
from app.models import (
    COMPLETE,
    HOLIDAY,
    LEAVE,
    MISSING,
    PARTIAL,
    STRIKE_STATUSES,
    WEEKEND,
)


# ---- config ----------------------------------------------------------------
def get_config(db: Session) -> Dict[str, str]:
    cfg = dict(m.CONFIG_DEFAULTS)
    for row in db.execute(select(m.Config)).scalars():
        cfg[row.key] = row.value
    return cfg


def cfg_int(cfg: Dict[str, str], key: str) -> int:
    try:
        return int(float(cfg.get(key) or m.CONFIG_DEFAULTS.get(key, "0")))
    except ValueError:
        return int(m.CONFIG_DEFAULTS.get(key, "0"))


def live_start(cfg: Dict[str, str]) -> Optional[dt.date]:
    v = cfg.get("live_start_date") or ""
    return dt.date.fromisoformat(v) if v else None


# ---- calendar helpers -------------------------------------------------------
def holidays_set(db: Session) -> set:
    return {h.date for h in db.execute(select(m.Holiday)).scalars()}


def is_working_day(emp: m.Employee, d: dt.date, holidays: set) -> bool:
    if d in holidays:
        return False
    return d.weekday() in emp.work_day_set


def leave_minutes_on(leaves: List[m.LeaveRecord], emp: m.Employee, d: dt.date) -> int:
    """Total approved leave minutes covering day d (None hours => full target).

    Only status == 'approved' counts — a merely-requested (pending) or
    rejected self-service leave request must not reduce the target or read
    as Leave. Every admin-entered and imported row defaults to 'approved'
    (see LeaveRecord.status), so this filter changes nothing for existing
    data; it only matters for the new self-service request flow."""
    total = 0
    for lv in leaves:
        if lv.covers(d) and lv.status == m.LEAVE_APPROVED:
            total += lv.minutes_per_day if lv.minutes_per_day is not None else emp.daily_target_minutes
    return min(total, emp.daily_target_minutes)


# ---- core day computation ---------------------------------------------------
def compute_day(
    emp: m.Employee,
    d: dt.date,
    submitted_total: Optional[int],   # None if day not submitted
    leave_min: int,
    working: bool,
    is_holiday: bool,
    tolerance_min: int,
    today: dt.date,
    break_excess_min: int = 0,  # break time beyond the configured allowance
) -> Optional[dict]:
    """Pure status computation for one (employee, day). Returns dict or None
    when there is nothing to record (e.g. blank future/pending days).

    break_excess_min extends the target: minutes taken on break beyond the
    admin-configured daily allowance (max_break_minutes) must be made up in
    logged work, same idea as leave reducing the target in the other
    direction. Zero for every historical/imported day (BreakEntry didn't
    exist before this feature), so this never touches frozen history."""
    actual = submitted_total or 0
    if not working:
        status = HOLIDAY if is_holiday else WEEKEND
        if submitted_total is None and actual == 0:
            # nothing logged on a non-working day: still record the day for
            # calendar rendering, variance 0
            return {"status": status, "actual": 0, "target": 0, "variance": 0}
        return {"status": status, "actual": actual, "target": 0, "variance": actual}

    base_target = max(0, emp.daily_target_minutes - leave_min)
    if leave_min > 0 and base_target == 0:
        # full-day leave: target 0, no variance (PRD §5) — no work expected,
        # so break policy doesn't apply on a day off
        return {"status": LEAVE, "actual": actual, "target": 0, "variance": actual}

    target = base_target + max(0, break_excess_min)

    if submitted_total is None:
        if d >= today:
            return None  # pending — the day isn't over
        if leave_min > 0:
            # partial leave but never submitted: still a missing working day
            return {"status": MISSING, "actual": 0, "target": target, "variance": -target}
        return {"status": MISSING, "actual": 0, "target": target, "variance": -target}

    status = COMPLETE if actual >= target - tolerance_min else PARTIAL
    return {"status": status, "actual": actual, "target": target, "variance": actual - target}


# ---- recompute + materialize -------------------------------------------------
def recompute_employee(
    db: Session,
    emp: m.Employee,
    start: dt.date,
    end: dt.date,
    cfg: Optional[Dict[str, str]] = None,
    today: Optional[dt.date] = None,
) -> int:
    """Rebuild computed DayStatus rows for one employee over [start, end].
    Imported rows are left untouched; override fields are preserved."""
    cfg = cfg or get_config(db)
    today = today or dt.date.today()
    tolerance = cfg_int(cfg, "tolerance_minutes")
    ls = live_start(cfg)
    if ls and start < ls:
        start = ls
    if start > end:
        return 0

    holidays = holidays_set(db)
    subs = {
        s.date: s
        for s in db.execute(
            select(m.DaySubmission).where(
                m.DaySubmission.employee_id == emp.id,
                m.DaySubmission.date.between(start, end),
            )
        ).scalars()
    }
    leaves = list(
        db.execute(
            select(m.LeaveRecord).where(
                m.LeaveRecord.employee_id == emp.id,
                m.LeaveRecord.start_date <= end,
                m.LeaveRecord.end_date >= start,
            )
        ).scalars()
    )
    max_break = cfg_int(cfg, "max_break_minutes")
    break_totals: Dict[dt.date, int] = {}
    for b in db.execute(
        select(m.BreakEntry).where(
            m.BreakEntry.employee_id == emp.id,
            m.BreakEntry.date.between(start, end),
            m.BreakEntry.end_minute.isnot(None),
        )
    ).scalars():
        break_totals[b.date] = break_totals.get(b.date, 0) + b.duration_minutes
    existing = {
        r.date: r
        for r in db.execute(
            select(m.DayStatus).where(
                m.DayStatus.employee_id == emp.id,
                m.DayStatus.date.between(start, end),
            )
        ).scalars()
    }
    comp_dates = compensated_dates(db, emp.id)

    n = 0
    d = start
    while d <= end:
        row = existing.get(d)
        if row is not None and row.source == "imported":
            d += dt.timedelta(days=1)
            continue  # legacy fact is frozen
        if emp.start_date and d < emp.start_date:
            d += dt.timedelta(days=1)
            continue
        sub = subs.get(d)
        res = compute_day(
            emp,
            d,
            sub.total_minutes if sub else None,
            leave_minutes_on(leaves, emp, d),
            is_working_day(emp, d, holidays),
            d in holidays,
            tolerance,
            today,
            max(0, break_totals.get(d, 0) - max_break),
        )
        if res is None:
            if row is not None:
                db.delete(row)
            d += dt.timedelta(days=1)
            continue
        if row is None:
            row = m.DayStatus(employee_id=emp.id, date=d)
            db.add(row)
        row.status = res["status"]
        row.actual_minutes = res["actual"]
        row.target_minutes = res["target"]
        row.variance_minutes = res["variance"]
        row.source = "computed"
        row.compensated = d in comp_dates
        row.computed_at = dt.datetime.utcnow()
        n += 1
        d += dt.timedelta(days=1)
    db.commit()
    return n


def recompute_all(db: Session, start: dt.date, end: dt.date) -> int:
    cfg = get_config(db)
    n = 0
    emps = db.execute(
        select(m.Employee).where(m.Employee.active.is_(True), m.Employee.tracked.is_(True))
    ).scalars()
    for emp in emps:
        n += recompute_employee(db, emp, start, end, cfg)
    return n


# ---- compensation ------------------------------------------------------------
def compensated_dates(db: Session, employee_id: int) -> set:
    """Shortfall dates whose CompensationLink is fully covered."""
    out = set()
    for link in db.execute(
        select(m.CompensationLink).where(m.CompensationLink.employee_id == employee_id)
    ).scalars():
        if link.fully_compensated:
            out.add(link.shortfall_date)
    return out


def evaluate_link(db: Session, link: m.CompensationLink) -> None:
    """A link is fully compensated when the linked surplus days' positive
    variance covers the shortfall day's deficit."""
    short_row = db.execute(
        select(m.DayStatus).where(
            m.DayStatus.employee_id == link.employee_id,
            m.DayStatus.date == link.shortfall_date,
        )
    ).scalar_one_or_none()
    deficit = 0
    if short_row is not None and (short_row.variance_minutes or 0) < 0:
        deficit = -(short_row.variance_minutes or 0)
    surplus = 0
    for iso in json.loads(link.surplus_dates or "[]"):
        srow = db.execute(
            select(m.DayStatus).where(
                m.DayStatus.employee_id == link.employee_id,
                m.DayStatus.date == dt.date.fromisoformat(iso),
            )
        ).scalar_one_or_none()
        if srow is not None and (srow.variance_minutes or 0) > 0:
            surplus += srow.variance_minutes
    link.fully_compensated = deficit > 0 and surplus >= deficit
    if short_row is not None:
        short_row.compensated = link.fully_compensated
    db.commit()


def surplus_links_by_date(db: Session, employee_id: int) -> Dict[dt.date, m.CompensationLink]:
    out: Dict[dt.date, m.CompensationLink] = {}
    for link in db.execute(
        select(m.CompensationLink).where(m.CompensationLink.employee_id == employee_id)
    ).scalars():
        for iso in json.loads(link.surplus_dates or "[]"):
            out[dt.date.fromisoformat(iso)] = link
    return out


# ---- reporting ---------------------------------------------------------------
def month_range(year: int, month: int):
    first = dt.date(year, month, 1)
    last = (first.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
    return first, last


def statuses_for_month(db: Session, year: int, month: int) -> Dict[int, Dict[dt.date, m.DayStatus]]:
    first, last = month_range(year, month)
    out: Dict[int, Dict[dt.date, m.DayStatus]] = {}
    for row in db.execute(
        select(m.DayStatus).where(m.DayStatus.date.between(first, last))
    ).scalars():
        out.setdefault(row.employee_id, {})[row.date] = row
    return out


def strikes_in(rows, comp_erases: bool) -> int:
    return sum(
        1
        for r in rows
        if r.effective_status(comp_erases) in STRIKE_STATUSES and not r.strike_exempt
    )


def strikes_for_month(db: Session, employee_id: int, year: int, month: int, cfg=None) -> int:
    cfg = cfg or get_config(db)
    first, last = month_range(year, month)
    rows = db.execute(
        select(m.DayStatus).where(
            m.DayStatus.employee_id == employee_id,
            m.DayStatus.date.between(first, last),
        )
    ).scalars()
    return strikes_in(rows, cfg.get("comp_erases_strike") == "1")


def running_ledger(db: Session, emp: m.Employee, start: dt.date, end: dt.date) -> List[dict]:
    """Per-day variance with running net balance (PRD §5). Unknown variance
    (legacy rows without extra/short data) shows as None and doesn't move
    the balance."""
    rows = list(
        db.execute(
            select(m.DayStatus)
            .where(m.DayStatus.employee_id == emp.id, m.DayStatus.date.between(start, end))
            .order_by(m.DayStatus.date)
        ).scalars()
    )
    out = []
    balance = 0
    for r in rows:
        if r.variance_minutes is not None:
            balance += r.variance_minutes
        out.append({"row": r, "balance": balance})
    return out


def today_attendance(
    db: Session, cfg: Optional[Dict[str, str]] = None, today: Optional[dt.date] = None
) -> Dict[str, list]:
    """Live 'who's doing what today' view for the admin dashboard landing
    page. Deliberately NOT DayStatus/compute_day — those never mark today
    Missing because the day isn't over yet (see module docstring), so a
    DayStatus row for today usually doesn't exist until end of day. This
    looks at what's on record *right now*: any time logged, an open break,
    or approved leave covering today.

    Employees not scheduled to work today (weekend/holiday) are reported
    separately in 'off_today' so they don't inflate a 'not yet logged'
    count admins can't actually act on."""
    cfg = cfg or get_config(db)
    today = today or dt.date.today()
    holidays = holidays_set(db)
    emps = list(
        db.execute(
            select(m.Employee)
            .where(m.Employee.active.is_(True), m.Employee.tracked.is_(True))
            .order_by(m.Employee.department, m.Employee.name)
        ).scalars()
    )

    leaves_by_emp: Dict[int, List[m.LeaveRecord]] = {}
    for lv in db.execute(
        select(m.LeaveRecord).where(
            m.LeaveRecord.start_date <= today,
            m.LeaveRecord.end_date >= today,
            m.LeaveRecord.status == m.LEAVE_APPROVED,
        )
    ).scalars():
        leaves_by_emp.setdefault(lv.employee_id, []).append(lv)

    logged_emp_ids = {
        row[0]
        for row in db.execute(
            select(m.TaskEntry.employee_id).where(m.TaskEntry.date == today).distinct()
        ).all()
    }
    active_break_emp_ids = {
        b.employee_id
        for b in db.execute(
            select(m.BreakEntry).where(
                m.BreakEntry.date == today, m.BreakEntry.end_minute.is_(None)
            )
        ).scalars()
    }

    logged, on_leave, not_yet, off_today = [], [], [], []
    for e in emps:
        if not is_working_day(e, today, holidays):
            off_today.append(e)
            continue
        emp_leaves = leaves_by_emp.get(e.id, [])
        leave_min = leave_minutes_on(emp_leaves, e, today) if emp_leaves else 0
        full_day_leave = leave_min > 0 and (e.daily_target_minutes - leave_min) <= 0
        if full_day_leave:
            on_leave.append(e)
        elif e.id in logged_emp_ids or e.id in active_break_emp_ids:
            logged.append(e)
        else:
            not_yet.append(e)
    return {"logged": logged, "on_leave": on_leave, "not_yet": not_yet, "off_today": off_today}


def day_total_minutes(db: Session, employee_id: int, d: dt.date) -> int:
    total = db.execute(
        select(func.sum(m.TaskEntry.end_minute - m.TaskEntry.start_minute)).where(
            m.TaskEntry.employee_id == employee_id, m.TaskEntry.date == d
        )
    ).scalar()
    return int(total or 0)
