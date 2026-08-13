"""Entry validation — the strict guidelines of PRD §4.

All rules enforced server-side; the Today screen mirrors them client-side
for immediate feedback. Raises EntryError with a user-readable message list.
"""
import datetime as dt
from typing import List, Optional, Union

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models as m
from app.engine import cfg_int, holidays_set, is_working_day
from app.util import today_local


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


def entry_details_edit_error(
    entry: Union[m.TaskEntry, m.BreakEntry],
    user: m.Employee,
    today: dt.date,
    day_submission: Optional[m.DaySubmission],
) -> Optional[str]:
    """Guard for editing an existing TaskEntry's (or, since 2026-08-14, a
    BreakEntry's) Details text in place (Ganesh, 2026-08-10 — rows were
    previously delete-and-re-add only, no in-place edit). Pulled out of
    routes/employee.py's edit_entry_details so the rule is unit-testable
    without a live route/DB round trip, same pattern as the rest of this
    module. Returns None when editing is allowed, or a user-facing error
    message when it isn't.

    Ownership (entry.employee_id != user.id) is checked by the route
    before this is even called, same as delete_entry's existing pattern —
    this only covers the two rules that gate the edit itself: today-only
    for a self-service employee (yesterday's log is closed to quiet edits
    the same way it's closed to deletes once locked, so history stays
    trustworthy), and the existing day-lock rule. Admins bypass both,
    same precedent as delete_entry. Both models have a plain `.date`
    column, so this works unchanged for either — no BreakEntry-specific
    branch needed."""
    if not user.is_admin and entry.date != today:
        return "You can only edit today's entries — ask an admin to fix a past day."
    if day_submission is not None and day_submission.locked and not user.is_admin:
        return "Day is locked — ask an admin to unlock it."
    return None


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
    today = today_local()

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
    # A suggestion (Ganesh, 2026-08-01: employee/lead-suggested projects/
    # tasks) is unusable by anyone — including whoever suggested it — until
    # a team lead/admin approves it (Ganesh, 2026-08-11: previously the
    # submitter could use their own pending suggestion right away; an admin
    # reported unreviewed projects/tasks ending up on real logged time
    # before anyone had signed off on them, so that carve-out is gone).
    # Enforced here, not just hidden client-side, since the dropdown filter
    # alone would only be a UI nicety, not a real rule.
    project = db.get(m.Project, project_id) if project_id else None
    if project is not None and project.active and project.status == m.LIST_PENDING:
        errors.append("That Project/Employer is still awaiting admin approval.")
    elif project is None or not project.active or project.status != m.LIST_APPROVED:
        errors.append("Choose a Project/Employer from the list.")

    task = db.get(m.TaskType, task_type_id) if task_type_id else None
    if task is not None and task.active and task.status == m.LIST_PENDING:
        errors.append("That Task is still awaiting admin approval.")
    elif task is None or not task.active or task.status != m.LIST_APPROVED:
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

        # --- no logging over a break --------------------------------------------
        # An employee can't be doing task work and on a break at the same time.
        # Blocked here (not just left to gap_flags's netting, below) so the
        # employee gets pointed at the actual break window and a valid start
        # time, instead of silently saving a row that overlaps it (Ganesh,
        # 2026-08-11 — employee logged 1:15 PM when their break ran 12:57–1:18
        # PM). An open/still-running break (end_minute is None) blocks
        # everything from its start to end of day, same as "you're on a break
        # right now."
        breaks = db.execute(
            select(m.BreakEntry).where(
                m.BreakEntry.employee_id == emp.id, m.BreakEntry.date == date
            )
        ).scalars()
        for b in breaks:
            b_end = b.end_minute if b.end_minute is not None else 1440
            if start_minute < b_end and b.start_minute < end_minute:
                b_end_label = fmt_minute(b.end_minute) if b.end_minute is not None else "now"
                errors.append(
                    f"That time is during your {b.break_type} break "
                    f"({fmt_minute(b.start_minute)}–{b_end_label}). "
                    f"Choose a start time after the break ends."
                )
                break

    if errors:
        raise EntryError(errors)


def suggest_non_overlapping_start(
    db: Session,
    emp: m.Employee,
    date: dt.date,
    start_minute: int,
    end_minute: int,
    entry_id: Optional[int] = None,
) -> Optional[int]:
    """When start_minute..end_minute overlaps an existing TaskEntry row or a
    BreakEntry (Ganesh, 2026-08-14 — a failed Add Row used to reset the
    whole form and leave the employee to guess a new time by trial and
    error), return the earliest minute that clears every conflict — the
    latest end_minute among everything it overlapped, since touching a row
    exactly at its boundary is fine (the overlap check below is strictly
    `<`, matching validate_entry's own rule, not `<=`).

    Returns None when nothing actually overlaps — including when the
    failure was for an unrelated reason (missing details, unapproved
    project, etc.) — so callers can safely call this unconditionally on any
    validate_entry() failure and only get a suggestion back when a real
    time conflict exists.

    Deliberately duplicates validate_entry's own overlap conditions rather
    than calling it (this only wants "does X overlap and by how much", not
    a exception/error-message path) — the two overlap checks must be kept
    in sync; a change to one almost certainly needs the same change here."""
    if end_minute <= start_minute:
        return None
    conflict_end = None

    others = db.execute(
        select(m.TaskEntry).where(m.TaskEntry.employee_id == emp.id, m.TaskEntry.date == date)
    ).scalars()
    for other in others:
        if entry_id is not None and other.id == entry_id:
            continue
        if start_minute < other.end_minute and other.start_minute < end_minute:
            conflict_end = max(conflict_end or 0, other.end_minute)

    breaks = db.execute(
        select(m.BreakEntry).where(m.BreakEntry.employee_id == emp.id, m.BreakEntry.date == date)
    ).scalars()
    for b in breaks:
        b_end = b.end_minute if b.end_minute is not None else 1440
        if start_minute < b_end and b.start_minute < end_minute:
            conflict_end = max(conflict_end or 0, b_end)

    return conflict_end


def gap_flags(
    entries: List[m.TaskEntry], gap_minutes: int, breaks: Optional[List[m.BreakEntry]] = None
) -> dict:
    """entry.id -> unexplained gap in minutes since previous row, when over
    threshold. Visual flag only, never a block (breaks are legitimate —
    logging *over* one is blocked separately, in validate_entry).

    `breaks` (an employee's BreakEntry rows for the same day, normally just
    the completed ones) are netted out of each raw gap before it's compared
    to the threshold, and the flag — when still over threshold — shows the
    *remaining* unexplained minutes, not the raw gap (Ganesh, 2026-08-11:
    previously a break had to line up exactly with both neighboring rows to
    the minute to suppress the flag at all — see employee.py's old
    _day_context post-processing — so a break that started/ended a couple
    minutes off from the adjacent task rows still flagged the *entire* gap).
    Passing breaks=None (or omitting it) flags the full raw gap, unchanged
    from before this existed."""
    flags = {}
    ordered = sorted(entries, key=lambda e: e.start_minute)
    for prev, cur in zip(ordered, ordered[1:]):
        gap = cur.start_minute - prev.end_minute
        if gap <= 0:
            continue
        covered = 0
        for b in breaks or ():
            b_end = b.end_minute if b.end_minute is not None else cur.start_minute
            overlap = min(cur.start_minute, b_end) - max(prev.end_minute, b.start_minute)
            if overlap > 0:
                covered += overlap
        remaining = gap - min(covered, gap)
        if remaining > gap_minutes:
            flags[cur.id] = remaining
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
