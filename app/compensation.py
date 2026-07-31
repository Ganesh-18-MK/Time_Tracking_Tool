"""Automatic Punch Clock compensation balance (Ganesh, 2026-07-31).

If an employee's completed Punch In/Out time on a day falls short of that
day's target, the shortfall accumulates into a balance they're expected to
work off by the end of the calendar month; completed overtime from other
days in the same month automatically pays it back down — no admin approval
step, per Ganesh's explicit choice ("Punch Clock hours, fully automatic
deduction") when asked to confirm this against the existing, deliberately
manual CompensationLink feature (app/models.py — an admin explicitly links
a shortfall day to surplus days so there's a record for the disciplinary
conversation). That manual feature is untouched; this is a second, purely
additive concept that happens to share the word "compensation".

Overtime beyond what's owed banks as a credit rather than being capped at
0 (Ganesh, confirmed 2026-07-31: short 1h Monday + 3h overtime Tuesday ->
balance is +2h, not 0h) — see "balance"'s sign convention below.

Deliberately independent of app/engine.py, DayStatus, and strikes — same
boundary PunchSession itself was built to respect (see its docstring):
actual_minutes/status/variance/strikes still come only from logged
TaskEntry rows. This module only reads DayStatus.target_minutes (already
leave/holiday/break-adjusted) as the day's expected minutes and compares
it against completed Punch In/Out minutes — nothing here writes back to
DayStatus, so no verify_strikes re-check is triggered by this feature.

Resets every calendar month (Ganesh: "compensate by end of the month") —
each month_summary() call is scoped to one year/month and starts fresh.
"""
import datetime as dt
from typing import Dict, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import engine, models as m
from app.util import overtime_minutes


def _completed_punch_minutes_by_day(
    db: Session, employee_id: int, start: dt.date, end: dt.date
) -> Dict[dt.date, int]:
    """date -> total completed Punch In/Out minutes that day. Only closed
    sessions count, same convention as app/reports.py's
    _punch_minutes_by_day (a still-running punch isn't a finished fact
    yet)."""
    out: Dict[dt.date, int] = {}
    for row in db.execute(
        select(m.PunchSession).where(
            m.PunchSession.employee_id == employee_id,
            m.PunchSession.date.between(start, end),
            m.PunchSession.punched_out_at.isnot(None),
        )
    ).scalars():
        out[row.date] = out.get(row.date, 0) + (row.duration_minutes or 0)
    return out


def monthly_summary(
    db: Session, employee: m.Employee, year: int, month: int, today: Optional[dt.date] = None
) -> dict:
    """{"balance": int, "shortfall_total": int, "overtime_total": int,
    "days": [{"date","target","punched","shortfall","overtime"}, ...]}
    for one calendar month, through today (or the month's last day if it's
    already over).

    "balance" = overtime_total - shortfall_total — signed, same convention
    as the existing task-row-based running balance on My Month (positive =
    ahead/credit, negative = still owed), so the two KPI cards read
    consistently side by side. Overtime beyond what's owed banks as a
    credit rather than being capped at 0 — confirmed explicitly by Ganesh
    (short 1h + over 3h in the same month -> +2h, not 0h). Resets each
    calendar month (see module docstring) — a credit does NOT carry into
    the next month's calculation, only within-month shortfall/overtime
    nets against each other. Only days with a positive
    DayStatus.target_minutes count as a working day; leave/holiday/weekend
    days have target 0 and are never a shortfall."""
    today = today or dt.date.today()
    first, last = engine.month_range(year, month)
    end = min(last, today)
    if end < first:
        return {"balance": 0, "shortfall_total": 0, "overtime_total": 0, "days": []}

    statuses = list(
        db.execute(
            select(m.DayStatus).where(
                m.DayStatus.employee_id == employee.id,
                m.DayStatus.date.between(first, end),
            )
        ).scalars()
    )
    punch_by_day = _completed_punch_minutes_by_day(db, employee.id, first, end)

    shortfall_total = overtime_total = 0
    days = []
    for row in statuses:
        target = row.target_minutes
        if not target:  # None or 0 -> leave/holiday/weekend/no data yet
            continue
        punched = punch_by_day.get(row.date, 0)
        day_shortfall = max(0, target - punched)
        day_overtime = overtime_minutes(punched, target)
        shortfall_total += day_shortfall
        overtime_total += day_overtime
        if day_shortfall or day_overtime:
            days.append({
                "date": row.date, "target": target, "punched": punched,
                "shortfall": day_shortfall, "overtime": day_overtime,
            })
    days.sort(key=lambda d: d["date"])
    return {
        "balance": overtime_total - shortfall_total,
        "shortfall_total": shortfall_total,
        "overtime_total": overtime_total,
        "days": days,
    }
