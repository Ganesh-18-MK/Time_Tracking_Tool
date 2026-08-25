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
from app.util import today_local


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
def holidays_set(db: Session, location: Optional[str] = None) -> set:
    """Every Holiday date in the table — one shared company-wide calendar
    (Ganesh, 2026-08-14: reverted the 2026-08-12 per-country split — the
    team wants a single common list for US and India employees alike, not
    two separate ones). `location` is accepted but ignored; kept only so
    existing call sites (and the Holiday.location column itself, still
    present for schema-safety reasons — see Holiday's docstring) don't need
    to change shape. Every call site should just call holidays_set(db)."""
    return {h.date for h in db.execute(select(m.Holiday)).scalars()}


def is_working_day(emp: m.Employee, d: dt.date, holidays: set) -> bool:
    """`holidays` is the shared company-wide set from holidays_set(db) — same
    for every employee regardless of location. Not resolved inside this
    function so a caller can compute it once outside a date-iteration loop."""
    if d in holidays:
        return False
    return d.weekday() in emp.work_day_set


def leave_minutes_on(leaves: List[m.LeaveRecord], emp: m.Employee, d: dt.date) -> int:
    """Total approved leave minutes covering day d (None hours => full target).

    Only status == 'approved' counts — a merely-requested (pending) or
    rejected self-service leave request must not reduce the target or read
    as Leave. Every admin-entered and imported row defaults to 'approved'
    (see LeaveRecord.status), so this filter changes nothing for existing
    data; it only matters for the new self-service request flow.

    approved_minutes_per_day (Leave Management V2, 2026-08-21) wins over
    minutes_per_day when set — a partial approval (requirement 6: admin
    approves fewer hours than requested) must reduce the day's target by
    only what was actually approved, not the original ask, or the
    compliance math would silently ignore the partial-approval decision
    entirely. NULL on every pre-existing row (new column, additive-only),
    so this is a no-op for anything that isn't a V2 partial approval."""
    total = 0
    for lv in leaves:
        if lv.covers(d) and lv.status == m.LEAVE_APPROVED:
            if lv.approved_minutes_per_day is not None:
                total += lv.approved_minutes_per_day
            elif lv.minutes_per_day is not None:
                total += lv.minutes_per_day
            else:
                total += emp.daily_target_minutes
    return min(total, emp.daily_target_minutes)


# The three typed entitlement columns on Employee, each paired with the
# LEAVE_TYPES value it tracks — "Other" has no entitlement column, so it's
# deliberately excluded from leave_balance() below (matches the old static
# balances table, which only ever showed these three).
_LEAVE_ENTITLEMENT_FIELDS = {
    "Casual": "casual_leave_days",
    "Sick": "sick_leave_days",
    "Vacation": "vacation_leave_days",
}


def leave_balance(db: Session, emp: m.Employee, year: Optional[int] = None) -> Dict[str, Dict[str, float]]:
    """Remaining leave balance per type for one calendar year (manager
    request, 2026-08-10: approving a leave request should actually count
    against the employee's annual number, not just display it).

    "Used" is computed live from approved LeaveRecords whose date range
    overlaps that year — the identical day-by-day partial-day fraction math
    My Month's leave_totals already uses (app/routes/employee.py's my_leave/
    my_month: a half-day counts as 0.5, each day capped at 1.0 even if
    minutes_per_day somehow exceeds the daily target). Nothing is stored or
    decremented — recomputed fresh every call — so a calendar-year boundary
    resets the balance automatically with no separate reset job: once
    Jan 1 arrives, "used" for the new year starts back at 0 against that
    year's entitlement number. Pending/rejected requests never count (same
    LEAVE_APPROVED-only rule as leave_minutes_on above).

    Returns {type: {"entitlement": int, "used": float, "remaining": float}}
    for Casual/Sick/Vacation. An unset entitlement (NULL column) reads as 0,
    same convention as the Employee model docstring."""
    year = year or today_local().year
    y_start, y_end = dt.date(year, 1, 1), dt.date(year, 12, 31)
    entitlements = {t: getattr(emp, field) or 0 for t, field in _LEAVE_ENTITLEMENT_FIELDS.items()}
    used = {t: 0.0 for t in entitlements}
    rows = db.execute(
        select(m.LeaveRecord).where(
            m.LeaveRecord.employee_id == emp.id,
            m.LeaveRecord.start_date <= y_end,
            m.LeaveRecord.end_date >= y_start,
            m.LeaveRecord.status == m.LEAVE_APPROVED,
        )
    ).scalars()
    for lv in rows:
        if lv.type not in used:
            continue  # "Other" or any custom type has nothing to track against
        d = max(lv.start_date, y_start)
        while d <= min(lv.end_date, y_end):
            per_day = lv.minutes_per_day if lv.minutes_per_day is not None else emp.daily_target_minutes
            frac = per_day / emp.daily_target_minutes if emp.daily_target_minutes else 1
            used[lv.type] += min(frac, 1.0)
            d += dt.timedelta(days=1)
    return {
        t: {
            "entitlement": entitlements[t],
            "used": round(used[t], 2),
            "remaining": round(entitlements[t] - used[t], 2),
        }
        for t in entitlements
    }


# ---- Leave Management V2 (Ganesh, 2026-08-21) -------------------------------
# See docs/LEAVE_MANAGEMENT_PLAN.md for the full design. Split into pure
# date/number math (no DB, no Employee object) and thin Session-aware
# wrappers around it, same "compute_day is pure, recompute_employee is the
# DB-touching wrapper" shape already used above — the pure functions can be
# unit tested directly against the minutes table in the plan's §2 without a
# database or fastapi/sqlalchemy installed.

def full_months_elapsed(start: dt.date, as_of: dt.date) -> int:
    """How many calendar months are entirely contained in [start, as_of].
    A month only counts once its LAST day has passed — joining/starting
    mid-month never earns a partial month (docs/LEAVE_MANAGEMENT_PLAN.md
    §3: "Recommended: only full completed months count"). E.g. start =
    2026-01-15 -> the first candidate month is February; February counts
    once as_of >= 2026-02-28."""
    if as_of < start:
        return 0
    y, mo = start.year, start.month
    if start.day != 1:
        mo += 1
        if mo > 12:
            mo = 1
            y += 1
    count = 0
    while True:
        next_y, next_mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
        month_end = dt.date(next_y, next_mo, 1) - dt.timedelta(days=1)
        if month_end > as_of:
            break
        count += 1
        y, mo = next_y, next_mo
    return count


def years_of_service_on(start_date: dt.date, as_of: dt.date) -> float:
    """Date-accurate (not flat days/365) years of tenure as of a given
    date. Used only to pick a Planned Time accrual band, never stored."""
    if as_of <= start_date:
        return 0.0
    return (as_of - start_date).days / 365.25


def _planned_days_per_year(cfg: Dict[str, str], years: float) -> int:
    if years < 2:
        key = "planned_days_year_0_2"
    elif years < 5:
        key = "planned_days_year_2_5"
    else:
        key = "planned_days_year_5_plus"
    return cfg_int(cfg, key)


def planned_time_accrued_minutes_pure(
    start_date: dt.date,
    probation_days: int,
    daily_target_minutes: int,
    cfg: Dict[str, str],
    as_of: dt.date,
) -> int:
    """Total Planned Time minutes earned to date — a running total, never
    reset each January (unlike leave_balance() above, by design: Planned
    Time is earned over an employee's whole tenure, not a per-calendar-
    year pool). Accrual starts the first full month after probation ends
    (never backdated into the probation window itself); the band (and
    therefore the monthly rate) is picked per-month from that month's own
    years-of-service, so a long-tenured employee's early months accrue at
    the lower band and later months at whatever band applies then —
    matches "the longer someone's worked here, the more they earn"
    literally, not just as a one-time lookup at call time.
    Rounds to the nearest whole minute (not floor) so a non-8h daily
    target doesn't get systematically shortchanged by fractional minutes
    every month — see docs/LEAVE_MANAGEMENT_PLAN.md §2 for why the 8h-day
    numbers land exactly on 360/440/520 without any rounding at all."""
    accrual_start = start_date + dt.timedelta(days=probation_days)
    months = full_months_elapsed(accrual_start, as_of)
    if months <= 0:
        return 0
    total = 0
    y, mo = accrual_start.year, accrual_start.month
    if accrual_start.day != 1:
        mo += 1
        if mo > 12:
            mo = 1
            y += 1
    for _ in range(months):
        next_y, next_mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
        month_end = dt.date(next_y, next_mo, 1) - dt.timedelta(days=1)
        years = years_of_service_on(start_date, month_end)
        days_per_year = _planned_days_per_year(cfg, years)
        total += int(round(days_per_year * daily_target_minutes / 12))
        y, mo = next_y, next_mo
    return total


def planned_time_accrued_minutes(
    db: Session, emp: m.Employee, cfg: Optional[Dict[str, str]] = None, as_of: Optional[dt.date] = None
) -> int:
    cfg = cfg or get_config(db)
    as_of = as_of or today_local()
    if emp.start_date is None:
        return 0
    probation_days = emp.probation_days if emp.probation_days is not None else cfg_int(cfg, "probation_days_default")
    return planned_time_accrued_minutes_pure(emp.start_date, probation_days, emp.daily_target_minutes, cfg, as_of)


def is_probation_active(emp: m.Employee, as_of: dt.date, cfg: Dict[str, str]) -> bool:
    """Requirement: block Planned Time during the waiting period (other
    types stay available — see LEAVE_TYPES_NO_PROBATION_BLOCK in
    models.py, checked by the caller, not here)."""
    if emp.start_date is None:
        return False
    probation_days = emp.probation_days if emp.probation_days is not None else cfg_int(cfg, "probation_days_default")
    return as_of < emp.start_date + dt.timedelta(days=probation_days)


def required_notice_working_days(days_requested: int) -> int:
    """Planned Time notice period (docs/LEAVE_MANAGEMENT_PLAN.md, decided
    2026-08-20): 1 day -> 2 working days' notice, 2-3 days -> 7 working
    days, 4+ days -> 3 weeks. The first two tiers are stated in working
    days explicitly; "3 weeks" is treated as 15 working days (3 x a
    standard 5-day week) for consistency with the other two tiers rather
    than 21 *calendar* days, which would be a different, unstated unit —
    flag this interpretation if a literal calendar-weeks reading was
    actually intended."""
    if days_requested <= 1:
        return 2
    if days_requested <= 3:
        return 7
    return 15


def notice_period_satisfied(
    submitted_on: dt.date, leave_start: dt.date, days_requested: int, emp: m.Employee, holidays: set
) -> bool:
    """Working days strictly between submission and the leave's start
    date, counted with the same is_working_day()/holidays_set() every
    other compliance calculation in this file uses — a holiday or
    employee's own non-working day never counts toward satisfying
    notice."""
    required = required_notice_working_days(days_requested)
    d = submitted_on + dt.timedelta(days=1)
    count = 0
    while d < leave_start:
        if is_working_day(emp, d, holidays):
            count += 1
        d += dt.timedelta(days=1)
    return count >= required


def effective_leave_type(emp: m.Employee, requested_type: str) -> str:
    """Requirement: no paid leave while on a PIP — every leave type
    becomes Unpaid Time for someone with is_on_pip=True, decided at the
    moment of request (never retroactively rewriting a leave record if
    the PIP flag changes later — same frozen-history spirit as everything
    else in this file). Special Paid Time is a management grant, not
    something an employee requests, so PIP has nothing to override there."""
    if getattr(emp, "is_on_pip", False) and requested_type != m.LEAVE_SPECIAL_PAID:
        return m.LEAVE_UNPAID
    return requested_type


def leave_balance_v2(
    db: Session, emp: m.Employee, as_of: Optional[dt.date] = None, cfg: Optional[Dict[str, str]] = None
) -> Dict[str, Dict[str, Optional[int]]]:
    """Used/Pending/Remaining per LEAVE_TYPES_V2 type, all in integer
    minutes (templates convert to hours for display via the `hm` filter,
    same as everywhere else). Only Planned Time (accrued) and Special
    Paid Time (granted, see SpecialPaidGrant) have a real capped
    entitlement — Unplanned/Unpaid/Bereavement Time have no pool to run
    out of (PDF: available on request, not accrued), so their
    "entitlement"/"remaining" are None rather than a fabricated number;
    Used/Pending are still tracked for all five so an employee can see
    what they've taken.

    "Used" sums approved_minutes_per_day (falling back to the originally
    requested minutes_per_day only if a decision was made without setting
    it, and to the employee's own daily target if that's also unset — same
    None-means-full-day convention as leave_minutes_on above) — so a
    partial approval (requirement 6) is reflected correctly instead of
    quietly still counting the full original request. "Pending" sums
    still-requested rows at their originally requested amount — held out
    of "remaining" the moment they're requested, matching the "used and
    pending shown separately" requirement, not hidden inside a single
    balance number."""
    cfg = cfg or get_config(db)
    as_of = as_of or today_local()

    entitlement: Dict[str, Optional[int]] = {t: None for t in m.LEAVE_TYPES_V2}
    entitlement[m.LEAVE_PLANNED] = planned_time_accrued_minutes(db, emp, cfg, as_of)
    granted = db.execute(
        select(func.sum(m.SpecialPaidGrant.minutes)).where(m.SpecialPaidGrant.employee_id == emp.id)
    ).scalar() or 0
    entitlement[m.LEAVE_SPECIAL_PAID] = granted

    used = {t: 0 for t in m.LEAVE_TYPES_V2}
    pending = {t: 0 for t in m.LEAVE_TYPES_V2}
    rows = db.execute(
        select(m.LeaveRecord).where(
            m.LeaveRecord.employee_id == emp.id,
            m.LeaveRecord.type.in_(m.LEAVE_TYPES_V2),
        )
    ).scalars()
    for lv in rows:
        days = (lv.end_date - lv.start_date).days + 1
        if lv.status == m.LEAVE_APPROVED:
            if lv.approved_minutes_per_day is not None:
                per_day = lv.approved_minutes_per_day
            elif lv.minutes_per_day is not None:
                per_day = lv.minutes_per_day
            else:
                per_day = emp.daily_target_minutes
            used[lv.type] = used.get(lv.type, 0) + per_day * days
        elif lv.status == m.LEAVE_REQUESTED:
            per_day = lv.minutes_per_day if lv.minutes_per_day is not None else emp.daily_target_minutes
            pending[lv.type] = pending.get(lv.type, 0) + per_day * days

    out: Dict[str, Dict[str, Optional[int]]] = {}
    for t in m.LEAVE_TYPES_V2:
        ent = entitlement[t]
        remaining = None if ent is None else ent - used[t] - pending[t]
        out[t] = {"entitlement": ent, "used": used[t], "pending": pending[t], "remaining": remaining}
    return out


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
    today = today or today_local()
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
def _link_allocated_minutes(db: Session, link: m.CompensationLink) -> int:
    """This one link's own total consumed minutes, across every surplus day
    it lists. Reads `surplus_minutes` (the {"YYYY-MM-DD": minutes} JSON dict,
    added 2026-08-25) when present; falls back to summing each surplus_dates
    day's FULL variance when surplus_minutes is empty but surplus_dates
    isn't — that's an old link created before partial allocation existed,
    and full-day consumption is the correct frozen reading for it, not a
    gap to backfill (see CompensationLink.surplus_minutes' docstring in
    app/models.py)."""
    alloc = json.loads(link.surplus_minutes or "{}")
    if alloc:
        return sum(alloc.values())
    total = 0
    for iso in json.loads(link.surplus_dates or "[]"):
        srow = db.execute(
            select(m.DayStatus).where(
                m.DayStatus.employee_id == link.employee_id,
                m.DayStatus.date == dt.date.fromisoformat(iso),
            )
        ).scalar_one_or_none()
        if srow is not None and (srow.variance_minutes or 0) > 0:
            total += srow.variance_minutes
    return total


def shortfall_allocated_minutes(db: Session, employee_id: int, shortfall_date: dt.date) -> int:
    """Total minutes linked toward `shortfall_date` across EVERY existing
    CompensationLink for this employee that targets it — a shortfall day can
    now be covered by more than one link over time (Ganesh, 2026-08-25: "we
    can made it by selecting all three but 2 hours are pending... it should
    become 8/08 -2:00 hours" — i.e. link what you have now, link the rest
    later), so this sums across all of them rather than assuming exactly
    one. Used by both evaluate_link() (to set the day's true compensated
    flag) and add_complink() (to know how much deficit is still open before
    accepting a new link)."""
    total = 0
    for link in db.execute(
        select(m.CompensationLink).where(
            m.CompensationLink.employee_id == employee_id,
            m.CompensationLink.shortfall_date == shortfall_date,
        )
    ).scalars():
        total += _link_allocated_minutes(db, link)
    return total


def allocate_surplus_minutes(
    db: Session, employee_id: int, shortfall_date: dt.date, surplus_dates: List[str]
) -> Dict[str, int]:
    """Given a shortfall day and a date-sorted list of ISO surplus dates an
    admin/employee ticked, greedily allocate minutes from each day's
    REMAINING balance (its full variance minus whatever other links have
    already used, see surplus_minutes_used_by_date) until the shortfall's
    own remaining deficit (accounting for any earlier links already on this
    day, see shortfall_allocated_minutes) is covered. Consumes oldest-ticked
    day first, stops the moment the deficit hits zero — any day ticked
    beyond that point, or with nothing left, is simply absent from the
    result rather than an error; the caller decides what an empty vs.
    partial result means (see app/routes/admin.py's add_complink() and
    app/routes/employee.py's request_compensation_match(), which both call
    this so the admin-direct and employee-self-service flows can't drift
    apart — Ganesh, 2026-08-25)."""
    short_row = db.execute(
        select(m.DayStatus).where(m.DayStatus.employee_id == employee_id, m.DayStatus.date == shortfall_date)
    ).scalar_one_or_none()
    full_deficit = -(short_row.variance_minutes or 0) if short_row is not None and (short_row.variance_minutes or 0) < 0 else 0
    deficit_remaining = full_deficit - shortfall_allocated_minutes(db, employee_id, shortfall_date)
    if deficit_remaining <= 0:
        return {}
    used_by_date = surplus_minutes_used_by_date(db, employee_id)
    allocation: Dict[str, int] = {}
    for iso in surplus_dates:
        if deficit_remaining <= 0:
            break
        d = dt.date.fromisoformat(iso)
        srow = db.execute(
            select(m.DayStatus).where(m.DayStatus.employee_id == employee_id, m.DayStatus.date == d)
        ).scalar_one_or_none()
        full_variance = srow.variance_minutes if srow is not None and (srow.variance_minutes or 0) > 0 else 0
        remaining = full_variance - used_by_date.get(d, 0)
        if remaining <= 0:
            continue
        take = min(remaining, deficit_remaining)
        allocation[iso] = take
        deficit_remaining -= take
    return allocation


def shortfall_allocated_minutes_by_date(db: Session, employee_id: int) -> Dict[dt.date, int]:
    """Same sum as shortfall_allocated_minutes(), batched across every
    shortfall date this employee has any link for in one query instead of
    one-per-date (Ganesh, 2026-08-25) — used by the Shortfall day picker on
    Person Detail and Overtime Management to show each day as fully linked
    (disabled), partially linked (remaining balance shown), or not linked
    at all (full deficit shown), replacing the old binary "already linked"
    flag that didn't distinguish partial from full."""
    out: Dict[dt.date, int] = {}
    for link in db.execute(
        select(m.CompensationLink).where(m.CompensationLink.employee_id == employee_id)
    ).scalars():
        out[link.shortfall_date] = out.get(link.shortfall_date, 0) + _link_allocated_minutes(db, link)
    return out


def compensated_dates(db: Session, employee_id: int) -> set:
    """Shortfall dates whose COMBINED linked surplus (summed across every
    link that targets that date — see shortfall_allocated_minutes(), a day
    can be partially covered by more than one link now) meets or exceeds
    that day's deficit. Rewritten 2026-08-25 alongside partial allocation:
    the old version only checked a single link's own fully_compensated flag,
    which would have missed (and silently un-flipped, since
    recompute_employee() calls this on every regular recompute pass) a day
    that's only fully covered when two or more partial links are added
    together."""
    by_date: Dict[dt.date, int] = {}
    for link in db.execute(
        select(m.CompensationLink).where(m.CompensationLink.employee_id == employee_id)
    ).scalars():
        by_date[link.shortfall_date] = by_date.get(link.shortfall_date, 0) + _link_allocated_minutes(db, link)
    out = set()
    for shortfall_date, minutes in by_date.items():
        srow = db.execute(
            select(m.DayStatus).where(
                m.DayStatus.employee_id == employee_id, m.DayStatus.date == shortfall_date,
            )
        ).scalar_one_or_none()
        deficit = -(srow.variance_minutes or 0) if srow is not None and (srow.variance_minutes or 0) < 0 else 0
        if deficit > 0 and minutes >= deficit:
            out.add(shortfall_date)
    return out


def evaluate_link(db: Session, link: m.CompensationLink) -> None:
    """Sets two related but distinct things (Ganesh, 2026-08-25, partial
    allocation): `link.fully_compensated` reflects whether THIS link alone
    covers the whole shortfall (used for this link's own row badge —
    fully/partial/pending — and the flash message right after creating it);
    `DayStatus.compensated` on the shortfall day reflects the TRUE aggregate
    across every link that targets that day, via shortfall_allocated_minutes
    (so if a second, later link finishes off a day two earlier partial
    links started, the day correctly flips to compensated without needing
    its own fully_compensated to be True). Before this rewrite there was
    only ever one link per shortfall day in practice, so these two were
    always the same number — now they can differ."""
    short_row = db.execute(
        select(m.DayStatus).where(
            m.DayStatus.employee_id == link.employee_id,
            m.DayStatus.date == link.shortfall_date,
        )
    ).scalar_one_or_none()
    deficit = 0
    if short_row is not None and (short_row.variance_minutes or 0) < 0:
        deficit = -(short_row.variance_minutes or 0)
    own_surplus = _link_allocated_minutes(db, link)
    link.fully_compensated = deficit > 0 and own_surplus >= deficit
    if short_row is not None:
        total = shortfall_allocated_minutes(db, link.employee_id, link.shortfall_date)
        short_row.compensated = deficit > 0 and total >= deficit
    db.commit()


def compensation_window_ok(shortfall_date: dt.date, surplus_date: dt.date) -> bool:
    """Requirement 9 (Overtime-for-Missed-Hours, employee-requested match,
    2026-08-21) — docs/LEAVE_MANAGEMENT_PLAN.md §3 names a "3-week/same-
    calendar-month window" as the validation check at approval time,
    without spelling out the exact boundary logic anywhere more precise
    in that doc. Implemented here as either/or, the common shape for this
    kind of policy: a surplus day is eligible to compensate a shortfall if
    it's within 21 calendar days of it OR falls in the same calendar
    month — whichever is more generous for a given pair of dates. Flag
    this interpretation to Ganesh if the source PDF actually meant
    something stricter (e.g. AND instead of OR, or calendar weeks instead
    of a flat 21-day radius)."""
    if abs((surplus_date - shortfall_date).days) <= 21:
        return True
    return surplus_date.year == shortfall_date.year and surplus_date.month == shortfall_date.month


def surplus_minutes_used_by_date(db: Session, employee_id: int) -> Dict[dt.date, int]:
    """How many minutes of each surplus day are already spoken for across
    every existing link for this employee — replaces surplus_links_by_date()
    (Ganesh, 2026-08-25: a surplus day used to be blocked entirely the
    moment it appeared in any link, even if only part of its variance was
    needed; now the remaining minutes = that day's full variance minus
    what this function reports used, and it stays pickable until that hits
    zero). Old-format links (no surplus_minutes, see
    CompensationLink.surplus_minutes' docstring) count as having used the
    FULL day for every date in their surplus_dates — same frozen-history
    fallback _link_allocated_minutes() uses elsewhere, just broken out
    per-day here instead of summed into one link total."""
    out: Dict[dt.date, int] = {}
    for link in db.execute(
        select(m.CompensationLink).where(m.CompensationLink.employee_id == employee_id)
    ).scalars():
        alloc = json.loads(link.surplus_minutes or "{}")
        if alloc:
            for iso, minutes in alloc.items():
                d = dt.date.fromisoformat(iso)
                out[d] = out.get(d, 0) + minutes
        else:
            for iso in json.loads(link.surplus_dates or "[]"):
                d = dt.date.fromisoformat(iso)
                srow = db.execute(
                    select(m.DayStatus).where(
                        m.DayStatus.employee_id == employee_id, m.DayStatus.date == d,
                    )
                ).scalar_one_or_none()
                if srow is not None and (srow.variance_minutes or 0) > 0:
                    out[d] = out.get(d, 0) + srow.variance_minutes
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
    today = today or today_local()
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
