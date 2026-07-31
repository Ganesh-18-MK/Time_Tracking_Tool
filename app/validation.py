"""Entry validation — the strict guidelines of PRD §4.

All rules enforced server-side; the Today screen mirrors them client-side
for immediate feedback. Raises EntryError with a user-readable message list.
"""
import datetime as dt
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models as m
from app.engine import cfg_int, holidays_set, is_working_day


class EntryError(Exception):
    def __init__(self, errors: List[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def earliest_allowed_date(
    emp: m.Employee, today: dt.date, backdate_working_days: int, holidays: set
) -> dt.date:
    """Employees may log at most N *working* days into the past (PRD §4/§10.7)."""
    d = today
    remaining = backdate_working_days
    while remaining > 0:
        d -= dt.timedelta(days=1)
        if is_working_day(emp, d, holidays):
            remaining -= 1
        if (today - d).days > 21:  # safety stop for odd schedules
            break
    return d


def validate_entry(
    db: Session,
    emp: m.Employee,
    date: dt.date,
    project_id: int,
    task_type_id: int,
    details: str,
    start_minute: int,
    end_minute: int,
    cfg: dict,
    entry_id: Optional[int] = None,
    acting_admin: bool = False,
) -> None:
    errors: List[str] = []
    today = dt.date.today()

    # --- date window ---------------------------------------------------------
    if not acting_admin:
        if date > today:
            errors.append("Cannot log time in the future.")
        else:
            earliest = earliest_allowed_date(
                emp, today, cfg_int(cfg, "backdate_working_days"), holidays_set(db)
            )
            if date < earliest:
                errors.append(
                    f"Too far back: employees may log at most "
                    f"{cfg_int(cfg, 'backdate_working_days')} working day(s) in the past "
                    f"(earliest allowed: {earliest.isoformat()})."
                )

    # --- locked day ------------------------------------------------------------
    sub = db.execute(
        select(m.DaySubmission).where(
            m.DaySubmission.employee_id == emp.id, m.DaySubmission.date == date
        )
    ).scalar_one_or_none()
    if sub is not None and sub.locked:
        errors.append("This day is submitted and locked. Ask an admin to unlock it.")

    # --- dropdowns only (no free text) ----------------------------------------
    # A pending suggestion (Ganesh, 2026-08-01: employee/lead-suggested
    # projects/tasks) is usable ONLY by whoever suggested it, until a team
    # lead approves it — enforced here, not just hidden client-side, since
    # the dropdown filter alone would only be a UI nicety, not a real rule.
    project = db.get(m.Project, project_id) if project_id else None
    if project is None or not project.active or not (
        project.status == m.LIST_APPROVED
        or (project.status == m.LIST_PENDING and project.created_by_employee_id == emp.id)
    ):
        errors.append("Choose a Project/Employer from the list.")
    task = db.get(m.TaskType, task_type_id) if task_type_id else None
    if task is None or not task.active or not (
        task.status == m.LIST_APPROVED
        or (task.status == m.LIST_PENDING and task.created_by_employee_id == emp.id)
    ):
        errors.append("Choose a Task from the list.")

    # --- details ----------------------------------------------------------------
    if len((details or "").strip()) < cfg_int(cfg, "min_details_chars"):
        errors.append(f"Details must be at least {cfg_int(cfg, 'min_details_chars')} characters.")

    # --- times -------------------------------------------------------------------
    if not (0 <= start_minute < 1440) or not (0 < end_minute <= 1440):
        errors.append("Times must be within one day (rows may not span midnight — split across days).")
    elif end_minute <= start_minute:
        errors.append("End Time must be after Start Time (rows may not span midnight — split across days).")
    else:
        max_row = cfg_int(cfg, "max_row_minutes")
        if end_minute - start_minute > max_row:
            errors.append(
                f"Single row longer than {max_row // 60}h {max_row % 60}m — break the work down."
            )
        # --- no overlapping rows within the day -------------------------------
        others = db.execute(
            select(m.TaskEntry).where(
                m.TaskEntry.employee_id == emp.id, m.TaskEntry.date == date
            )
        ).scalars()
        for other in others:
            if entry_id is not None and other.id == entry_id:
                continue
            if start_minute < other.end_minute and other.start_minute < end_minute:
                errors.append(
                    f"Overlaps existing row {fmt_minute(other.start_minute)}–{fmt_minute(other.end_minute)}."
                )
                break

    if errors:
        raise EntryError(errors)


def gap_flags(entries: List[m.TaskEntry], gap_minutes: int) -> dict:
    """entry.id -> gap in minutes since previous row, when over threshold.
    Visual flag only, never a block (breaks are legitimate)."""
    flags = {}
    ordered = sorted(entries, key=lambda e: e.start_minute)
    for prev, cur in zip(ordered, ordered[1:]):
        gap = cur.start_minute - prev.end_minute
        if gap > gap_minutes:
            flags[cur.id] = gap
    return flags


def fmt_minute(minute: int) -> str:
    h, mi = divmod(minute, 60)
    suffix = "AM"
    display_h = h
    if h == 0:
        display_h = 12
    elif h == 12:
        suffix = "PM"
    elif h > 12:
        display_h = h - 12
        suffix = "PM"
    return f"{display_h}:{mi:02d} {suffix}"
