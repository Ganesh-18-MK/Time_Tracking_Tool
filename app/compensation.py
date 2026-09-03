"""Automatic compensation balance (Ganesh, 2026-07-31; source switched
2026-09-03).

If an employee's completed hours on a day fall short of that day's target,
the shortfall accumulates into a balance they're expected to work off by
the end of the calendar month; completed overtime from other days in the
same month automatically pays it back down — no admin approval step, per
Ganesh's explicit choice ("Punch Clock hours, fully automatic deduction")
when asked to confirm this against the existing, deliberately manual
CompensationLink feature (app/models.py — an admin explicitly links a
shortfall day to surplus days so there's a record for the disciplinary
conversation). That manual feature is untouched; this is a second, purely
additive concept that happens to share the word "compensation".

Overtime beyond what's owed banks as a credit rather than being capped at
0 (Ganesh, confirmed 2026-07-31: short 1h Monday + 3h overtime Tuesday ->
balance is +2h, not 0h) — see "balance"'s sign convention below.

**Source switched from Punch In/Out to logged Task Log (TaskEntry) time
(Ganesh, 2026-09-03)** — originally this module deliberately read
completed Punch In/Out minutes (netting out BreakEntry time), independent
of app/engine.py/DayStatus/TaskEntry, on the reasoning that Punch In/Out
was a simpler, separate "were you at your desk" signal. Ganesh later
pointed out the My Month KPI tile this feeds ("Compensation owed this
month") read as inconsistent sitting right next to a Task-Log-based
number, and asked why it wasn't Task Log time too — by 2026-09-03 the
rest of the app (DayStatus.actual_minutes, strikes, compliance — see the
2026-08-28 "Auto-count logged hours" change) already treats Task Log time
as the one authoritative measure of hours worked, so Punch In/Out was the
odd one out here, not Task Log. `_logged_minutes_by_day()` now sums
TaskEntry duration per day instead of netting PunchSession against
BreakEntry — there's no break-netting step to repeat here anymore, since
TaskEntry rows never overlap with a break by construction (validate_entry
already rejects that).

Known, accepted consequence of the switch: for any day with a positive
target, `overtime - shortfall` for that day now equals exactly
`actual_minutes - target_minutes` — the same per-day figure
engine.running_ledger() sums into the "Running hours balance" KPI shown
directly above this one on My Month. Since this function's own
long-standing rule (unchanged by this switch) only ever looks at days
with target_minutes > 0 — see the loop below — while running_ledger()
sums every DayStatus row in range including target=0 (leave/holiday/
weekend) days, where variance_minutes = actual_minutes (any hours
logged on an off day count straight in there), the two totals will now
usually match but can genuinely diverge whenever hours were logged on an
off day to compensate: running_ledger's balance picks that up,
this one's balance does not. Flagged here rather than silently
changed, since narrowing/widening that day-inclusion rule wasn't part of
what was asked — only the data source was.

This module still writes nothing back to DayStatus, so no verify_strikes
re-check is triggered by this feature either way.

Resets every calendar month (Ganesh: "compensate by end of the month") —
each month_summary() call is scoped to one year/month and starts fresh.
"""
import datetime as dt
from typing import Dict, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import engine, models as m
from app.util import overtime_minutes, today_local


def _logged_minutes_by_day(
    db: Session, employee_id: int, start: dt.date, end: dt.date
) -> Dict[dt.date, int]:
    """date -> total logged TaskEntry minutes that day (Ganesh, 2026-09-03
    — replaces the original Punch In/Out + BreakEntry netting; see module
    docstring for why). Same grouped-sum shape as engine.recompute_employee's
    own `task_totals` query — the same live-TaskEntry source that already
    drives DayStatus.actual_minutes everywhere else in the app, so this
    module's "logged" figure and DayStatus's "actual" figure for the same
    day are the same number by construction, not just similar."""
    out: Dict[dt.date, int] = {}
    for d, total in db.execute(
        select(m.TaskEntry.date, func.sum(m.TaskEntry.end_minute - m.TaskEntry.start_minute))
        .where(
            m.TaskEntry.employee_id == employee_id,
            m.TaskEntry.date.between(start, end),
        )
        .group_by(m.TaskEntry.date)
    ).all():
        out[d] = total or 0
    return out


def monthly_summary(
    db: Session, employee: m.Employee, year: int, month: int, today: Optional[dt.date] = None
) -> dict:
    """{"balance": int, "shortfall_total": int, "overtime_total": int,
    "days": [{"date","target","logged","shortfall","overtime"}, ...]}
    for one calendar month, through today (or the month's last day if it's
    already over).

    "balance" = overtime_total - shortfall_total — signed, same convention
    as the existing task-row-based running balance on My Month (positive =
    ahead/credit, negative = still owed), so the two KPI cards read
    consistently side by side (as of the 2026-09-03 source switch, this is
    now more than a shared sign convention — see the module docstring's
    note on how close these two numbers now are). Overtime beyond what's
    owed banks as a credit rather than being capped at 0 — confirmed
    explicitly by Ganesh (short 1h + over 3h in the same month -> +2h, not
    0h). Resets each calendar month (see module docstring) — a credit does
    NOT carry into the next month's calculation, only within-month
    shortfall/overtime nets against each other. Only days with a positive
    DayStatus.target_minutes count as a working day; leave/holiday/weekend
    days have target 0 and are never a shortfall — unchanged by the
    2026-09-03 source switch, see the module docstring's note on why this
    means an off-day worked to compensate shows in running_ledger's
    balance but not in this one."""
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
    logged_by_day = _logged_minutes_by_day(db, employee.id, first, end)

    shortfall_total = overtime_total = 0
    days = []
    for row in statuses:
        target = row.target_minutes
        if not target:  # None or 0 -> leave/holiday/weekend/no data yet
            continue
        logged = logged_by_day.get(row.date, 0)
        day_shortfall = max(0, target - logged)
        day_overtime = overtime_minutes(logged, target)
        shortfall_total += day_shortfall
        overtime_total += day_overtime
        if day_shortfall or day_overtime:
            days.append({
                "date": row.date, "target": target, "logged": logged,
                "shortfall": day_shortfall, "overtime": day_overtime,
            })
    days.sort(key=lambda d: d["date"])
    return {
        "balance": overtime_total - shortfall_total,
        "shortfall_total": shortfall_total,
        "overtime_total": overtime_total,
        "days": days,
    }
