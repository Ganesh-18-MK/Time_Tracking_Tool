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

Break time never counts as punched/productive minutes (Ganesh,
2026-08-01: "whenever we are taking a break the punch in timer should
stop... then again punch timer should continue") — a PunchSession's raw
duration includes any breaks taken while punched in, so completed
BreakEntry minutes for that day are netted out before comparing punched
minutes to target. The other half of that request — break time beyond
the configured allowance adding to the hours owed — is already handled
by engine.compute_day's break_excess_min extending DayStatus.target_minutes
itself, so it doesn't need repeating here.

Resets every calendar month (Ganesh: "compensate by end of the month") —
each month_summary() call is scoped to one year/month and starts fresh.
"""
import datetime as dt
from typing import Dict, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import engine, models as m
from app.util import overtime_minutes, today_local


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


def _completed_break_minutes_by_day(
    db: Session, employee_id: int, start: dt.date, end: dt.date
) -> Dict[dt.date, int]:
    """date -> total completed break minutes that day. Netted out of
    Punch In/Out duration below — a break isn't productive time, so it
    must not count toward the automatic compensation balance (Ganesh,
    2026-08-01). Only closed breaks count, same convention as completed
    punch sessions above — a still-running break's not-yet-known duration
    is excluded until it ends."""
    out: Dict[dt.date, int] = {}
    for row in db.execute(
        select(m.BreakEntry).where(
            m.BreakEntry.employee_id == employee_id,
            m.BreakEntry.date.between(start, end),
            m.BreakEntry.end_minute.isnot(None),
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
    today = today or today_local()
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
    break_by_day = _completed_break_minutes_by_day(db, employee.id, first, end)

    shortfall_total = overtime_total = 0
    days = []
    for row in statuses:
        target = row.target_minutes
        if not target:  # None or 0 -> leave/holiday/weekend/no data yet
            continue
        # break time is netted out here, not added to target — the >30 min
        # portion already extended target_minutes itself, see module
        # docstring, so this is purely "breaks aren't work", not a second
        # penalty on top of the first.
        punched = max(0, punch_by_day.get(row.date, 0) - break_by_day.get(row.date, 0))
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
