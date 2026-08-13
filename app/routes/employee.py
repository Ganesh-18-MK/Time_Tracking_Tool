"""Employee screens: Today (log + submit) and My Month (PRD §7)."""
import datetime as dt
import os
from types import SimpleNamespace
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import compensation, engine, models as m
from app.auth import current_user
from app.db import get_db
from app.templating import HOLIDAY_MANAGEMENT_ENABLED, flash, render
from app.util import (
    FormError,
    audit,
    clamp_break_end,
    fmt_time,
    now_local,
    overtime_minutes,
    parse_date_field,
    parse_hhmm,
    parse_int_field,
    punch_out_error,
    punch_remaining_minutes,
    today_local,
)
from app.validation import (
    EntryError,
    earliest_allowed_date,
    entry_details_edit_error,
    gap_flags,
    suggest_non_overlapping_start,
    validate_entry,
)

router = APIRouter()

# ---- profile photo storage --------------------------------------------------
# Local disk, served via a dedicated StaticFiles mount at the same
# /static/uploads/avatars URL prefix the app already used (see app/main.py)
# — no template changes needed regardless of where the directory itself
# lives. AVATAR_UPLOAD_DIR is env-overridable so the directory can point at
# a host's persistent storage instead of the deployed code directory:
# most PaaS hosts wipe everything outside an explicit persistent path on
# every deploy (Railway needs a mounted Volume; Azure App Service persists
# /home automatically — pointing this at e.g. /home/data/avatars survives
# redeploys without depending on the deployed wwwroot tree itself staying
# untouched, since a fresh deploy replaces wwwroot's contents). Falls back
# to the original in-repo path when the env var isn't set — unchanged
# behavior for local dev and anywhere this is already handled another way.
# Object storage (S3-compatible / Azure Blob) is the longer-term fix if
# this ever needs to survive across multiple regions/instances.
_STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
AVATAR_DIR = os.environ.get("AVATAR_UPLOAD_DIR") or os.path.join(_STATIC_DIR, "uploads", "avatars")
ALLOWED_PHOTO_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
MAX_PHOTO_BYTES = 2 * 1024 * 1024  # 2 MB


def _allowed_dates(db: Session, emp: m.Employee, cfg) -> list:
    today = today_local()
    earliest = earliest_allowed_date(
        emp, today, engine.cfg_int(cfg, "backdate_working_days"), engine.holidays_set(db)
    )
    days = []
    d = earliest
    while d <= today:
        days.append(d)
        d += dt.timedelta(days=1)
    return days


class _BreakLogRow:
    """Read-only stand-in for a TaskEntry, built from a completed BreakEntry
    so a break shows up in the task log (Today) / entries view (My Month)
    without actually becoming a TaskEntry (Ganesh, 2026-08-14 — employees had
    been manually adding a 'Break' row themselves using whatever real client
    Project they'd last picked, which reads oddly on a report and, worse,
    quietly counted break time toward the day's logged total even though
    BreakEntry/target math already treats break time as separate from work
    — see BreakEntry's docstring and _day_context's break_excess comment).

    `id` is deliberately None — every place that would edit/delete/flag a
    real logged row (today.html's ✎ edit and ✕ controls, gap_flags' dict
    lookup) keys off entry.id, so `None` makes those a no-op/hidden for a
    break row for free, without templates needing to know this class
    exists. Project/Task are fixed labels, not real Project/TaskType rows —
    nothing pickable in the Add row/timer dropdowns changes, and no report
    ever attributes break time to an actual client/employer.

    `details` (Ganesh, 2026-08-14 — employees asked for the same in-place
    ✎ edit TaskEntry rows get) is the break_type plus the employee's own
    optional note, e.g. "Personal — stepped out for a call"; `break_id`
    (BreakEntry.id, separate from the always-None `id` above) and
    `break_notes` (the raw note only, no "Personal — " prefix) exist so
    today.html can point its edit control at POST /breaks/{break_id}/edit
    with just the note pre-filled, not the combined display string."""

    def __init__(self, b: m.BreakEntry):
        self.id = None
        self.break_id = b.id
        self.start_minute = b.start_minute
        self.end_minute = b.end_minute
        self.break_notes = b.details or ""
        self.details = b.break_type + (f" — {self.break_notes}" if self.break_notes else "")
        self.project = SimpleNamespace(name="General")
        self.task_type = SimpleNamespace(name="Break")

    @property
    def duration_minutes(self) -> int:
        return self.end_minute - self.start_minute


def _merge_entries_and_breaks(entries, breaks) -> list:
    """Combine real TaskEntry rows with completed BreakEntry rows into one
    chronological, display-only list (see _BreakLogRow above for why).
    Callers keep using the original `entries`/`breaks` lists, unchanged, for
    every accounting purpose (day total, target, gap_flags, compensation,
    overtime, strikes) — this merged list exists purely for what the
    employee sees in the task log table / My Month's per-day expand."""
    rows = list(entries) + [_BreakLogRow(b) for b in breaks if b.end_minute is not None]
    rows.sort(key=lambda r: r.start_minute)
    return rows


def _day_context(db: Session, emp: m.Employee, date: dt.date, cfg):
    entries = list(
        db.execute(
            select(m.TaskEntry)
            .where(m.TaskEntry.employee_id == emp.id, m.TaskEntry.date == date)
            .order_by(m.TaskEntry.start_minute)
        ).scalars()
    )
    total = sum(e.duration_minutes for e in entries)
    sub = db.execute(
        select(m.DaySubmission).where(
            m.DaySubmission.employee_id == emp.id, m.DaySubmission.date == date
        )
    ).scalar_one_or_none()
    leaves = list(
        db.execute(
            select(m.LeaveRecord).where(
                m.LeaveRecord.employee_id == emp.id,
                m.LeaveRecord.start_date <= date,
                m.LeaveRecord.end_date >= date,
            )
        ).scalars()
    )
    leave_min = engine.leave_minutes_on(leaves, emp, date)
    base_target = max(0, emp.daily_target_minutes - leave_min)

    breaks_today = list(
        db.execute(
            select(m.BreakEntry)
            .where(m.BreakEntry.employee_id == emp.id, m.BreakEntry.date == date)
            .order_by(m.BreakEntry.start_minute)
        ).scalars()
    )
    active_break = next((b for b in breaks_today if b.end_minute is None), None)
    completed_breaks = [b for b in breaks_today if b.end_minute is not None]
    total_break_minutes = sum(b.duration_minutes for b in completed_breaks)

    # break time beyond the configured allowance extends today's target —
    # shown live here, before submission; engine.compute_day applies the
    # identical rule once the day is actually submitted/recomputed. Full-day
    # leave (base_target == 0) is exempt, same as compute_day: no work
    # expected, so break policy doesn't apply on a day off.
    max_break = engine.cfg_int(cfg, "max_break_minutes")
    on_full_day_leave = leave_min > 0 and base_target == 0
    break_excess = 0 if on_full_day_leave else max(0, total_break_minutes - max_break)
    target = base_target + break_excess

    # Punch In/Out: a personal countdown-to-target widget, always keyed off
    # "today" in practice (see /punch/in, /punch/out below — same
    # right-now-only convention as breaks). Deliberately reuses `target`
    # (already break-excess/leave adjusted, above) rather than computing
    # its own — see PunchSession's docstring for why.
    punches_today = list(
        db.execute(
            select(m.PunchSession)
            .where(m.PunchSession.employee_id == emp.id, m.PunchSession.date == date)
            .order_by(m.PunchSession.punched_in_at)
        ).scalars()
    )
    active_punch = next((p for p in punches_today if p.punched_out_at is None), None)
    completed_punches = [p for p in punches_today if p.punched_out_at is not None]
    completed_punch_minutes = sum(p.duration_minutes for p in completed_punches)
    punch_remaining = punch_remaining_minutes(target, completed_punch_minutes)
    # only meaningful once punched out for the day — while a session is
    # still open, `punch_remaining` going negative already communicates
    # overtime live (see today.html); this is the "day's done" summary.
    punch_overtime = overtime_minutes(completed_punch_minutes, target) if active_punch is None else 0

    # Auto time-capture timer (Ganesh, 2026-08-01) — single active timer per
    # employee, not per-date (see ActiveTaskTimer docstring), so this is
    # fetched by employee only; today.html only shows the widget when
    # `date == today`, same as how Start Break/Punch In are hardcoded to
    # "today" regardless of which day is currently being viewed.
    active_timer = db.execute(
        select(m.ActiveTaskTimer).where(m.ActiveTaskTimer.employee_id == emp.id)
    ).scalar_one_or_none()

    # completed_breaks are netted out of each gap inside gap_flags itself now
    # (Ganesh, 2026-08-11) — a break that doesn't line up to the exact minute
    # with the rows on either side of it no longer flags the whole gap.
    flags = gap_flags(entries, engine.cfg_int(cfg, "gap_flag_minutes"), completed_breaks)

    return {
        "entries": entries,
        "display_entries": _merge_entries_and_breaks(entries, completed_breaks),
        "total": total,
        "sub": sub,
        "target": target,
        "leave_min": leave_min,
        "flags": flags,
        "active_break": active_break,
        "completed_breaks": completed_breaks,
        "total_break_minutes": total_break_minutes,
        "break_excess": break_excess,
        "max_break_minutes": max_break,
        "active_punch": active_punch,
        "completed_punches": completed_punches,
        "completed_punch_minutes": completed_punch_minutes,
        "punch_remaining": punch_remaining,
        "punch_overtime": punch_overtime,
        "active_timer": active_timer,
    }


def _visible_projects_and_tasks(db: Session, user: m.Employee):
    """What a given employee is allowed to pick from Today's Project/Task
    dropdowns: every approved, active one — nothing else. A suggestion
    (Ganesh, 2026-08-01) is invisible to everyone, including whoever
    suggested it, until a team lead/admin approves it (Ganesh, 2026-08-11:
    previously the submitter could use their own pending suggestion right
    away — an admin reported this let unreviewed projects/tasks end up on
    real logged time before anyone had signed off on them). validate_entry()
    enforces the same rule server-side, so this filter is a UI convenience,
    not the only thing standing between an employee and a not-yet-approved
    suggestion."""
    projects = list(
        db.execute(
            select(m.Project)
            .where(m.Project.active.is_(True), m.Project.status == m.LIST_APPROVED)
            .order_by(m.Project.name)
        ).scalars()
    )
    tasks = list(
        db.execute(
            select(m.TaskType)
            .where(m.TaskType.active.is_(True), m.TaskType.status == m.LIST_APPROVED)
            .order_by(m.TaskType.name)
        ).scalars()
    )
    return projects, tasks


def _combo_items(objs, assigned_ids: set) -> list:
    """ORM rows -> plain {id, name} dicts for the searchable-combo widget
    (see today.html/combo.js), with whatever this employee is assigned to
    (Ganesh, 2026-08-01: team-lead project/task assignment) sorted first
    and starred — advisory only, everything else stays just as pickable,
    only the ordering/label changes."""
    starred = [{"id": o.id, "name": f"★ {o.name}"} for o in objs if o.id in assigned_ids]
    rest = [{"id": o.id, "name": o.name} for o in objs if o.id not in assigned_ids]
    return starred + rest


@router.get("/today")
def today_page(
    request: Request,
    date: Optional[str] = None,
    reopen_project_id: Optional[str] = None,
    reopen_task_type_id: Optional[str] = None,
    reopen_details: Optional[str] = None,
    reopen_start: Optional[str] = None,
    reopen_end: Optional[str] = None,
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    cfg = engine.get_config(db)
    day = dt.date.fromisoformat(date) if date else today_local()
    projects, tasks = _visible_projects_and_tasks(db, user)
    assigned_project_ids = {
        row[0] for row in db.execute(
            select(m.ProjectAssignment.project_id).where(m.ProjectAssignment.employee_id == user.id)
        ).all()
    }
    assigned_task_ids = {
        row[0] for row in db.execute(
            select(m.TaskAssignment.task_type_id).where(m.TaskAssignment.employee_id == user.id)
        ).all()
    }
    ctx = _day_context(db, user, day, cfg)
    last_end = max((e.end_minute for e in ctx["entries"]), default=None)
    ctx.update(
        {
            # A sticky reopen (Ganesh, 2026-08-14 — see add_entry()'s
            # _reopen()) wins over the plain "start right after my last
            # row" default: it's what the employee already typed, possibly
            # nudged past whatever it conflicted with.
            "suggest_start": reopen_start or (
                f"{last_end // 60:02d}:{last_end % 60:02d}" if last_end else ""
            ),
            "reopen_project_id": reopen_project_id,
            "reopen_task_type_id": reopen_task_type_id,
            "reopen_details": reopen_details,
            "reopen_end": reopen_end,
            "user": user,
            "day": day,
            "today": today_local(),
            "allowed_dates": _allowed_dates(db, user, cfg),
            # plain dicts, not ORM objects — the template feeds these straight
            # into the searchable-combo widget via |tojson. Assigned ones
            # (Ganesh, 2026-08-01) sort first and get a ★ — advisory only,
            # everything else stays just as pickable.
            "projects": _combo_items(projects, assigned_project_ids),
            "tasks": _combo_items(tasks, assigned_task_ids),
            "max_row_minutes": engine.cfg_int(cfg, "max_row_minutes"),
            "gap_minutes": engine.cfg_int(cfg, "gap_flag_minutes"),
        }
    )
    return render(request, "today.html", ctx)


@router.post("/entries")
def add_entry(
    request: Request,
    date: str = Form(...),
    project_id: int = Form(0),
    task_type_id: int = Form(0),
    details: str = Form(""),
    start_time: str = Form(...),
    end_time: str = Form(...),
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    cfg = engine.get_config(db)
    day = dt.date.fromisoformat(date)

    # Sticky row on failure (Ganesh, 2026-08-14) — a failed Add Row used to
    # reset the whole form (project/task/details/times all cleared),
    # forcing the employee to retype everything just to fix one field.
    # `_reopen()` carries whatever was submitted back through the redirect
    # as query params; today_page() reads them as the Add Row form's
    # values instead of the normal blank/suggest_start-only defaults.
    def _reopen(start_value: str) -> RedirectResponse:
        params = urlencode(
            {
                "date": day.isoformat(),
                "reopen_project_id": project_id or "",
                "reopen_task_type_id": task_type_id or "",
                "reopen_details": details,
                "reopen_start": start_value,
                "reopen_end": end_time,
            }
        )
        return RedirectResponse(f"/today?{params}", status_code=303)

    try:
        start_minute = parse_hhmm(start_time)
        end_minute = parse_hhmm(end_time)
    except (ValueError, IndexError):
        flash(request, "Enter valid start and end times.", "err")
        return _reopen(start_time)
    try:
        validate_entry(
            db, user, day, project_id, task_type_id, details, start_minute, end_minute, cfg
        )
        db.add(
            m.TaskEntry(
                employee_id=user.id,
                date=day,
                project_id=project_id,
                task_type_id=task_type_id,
                details=details.strip(),
                start_minute=start_minute,
                end_minute=end_minute,
            )
        )
        db.commit()
    except EntryError as e:
        for err in e.errors:
            flash(request, err, "err")
        # When the failure was (also) a time conflict, nudge Start to the
        # earliest minute that clears it — touching a conflicting row's own
        # end is allowed, so no "+1 minute" fudge is needed. Any other
        # failure (details too short, project unapproved, etc.) leaves
        # Start exactly as typed; suggest_non_overlapping_start() returns
        # None when nothing actually overlapped.
        suggested = suggest_non_overlapping_start(db, user, day, start_minute, end_minute)
        corrected_start = (
            f"{suggested // 60:02d}:{suggested % 60:02d}" if suggested is not None else start_time
        )
        return _reopen(corrected_start)
    return RedirectResponse(f"/today?date={day.isoformat()}", status_code=303)


@router.post("/entries/{entry_id}/delete")
def delete_entry(
    entry_id: int,
    request: Request,
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    entry = db.get(m.TaskEntry, entry_id)
    if entry is None or (entry.employee_id != user.id and not user.is_admin):
        return RedirectResponse("/today", status_code=303)
    day = entry.date
    sub = db.execute(
        select(m.DaySubmission).where(
            m.DaySubmission.employee_id == entry.employee_id, m.DaySubmission.date == day
        )
    ).scalar_one_or_none()
    if sub is not None and sub.locked and not user.is_admin:
        flash(request, "Day is locked — ask an admin to unlock it.", "err")
    else:
        db.delete(entry)
        db.commit()
    return RedirectResponse(f"/today?date={day.isoformat()}", status_code=303)


@router.post("/entries/{entry_id}/edit")
def edit_entry_details(
    entry_id: int,
    request: Request,
    details: str = Form(...),
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Ganesh, 2026-08-10: rows were previously delete-and-re-add only, no
    in-place edit. Deliberately scoped to just the Details text (not
    times/project/task — changing those re-opens overlap/cap validation,
    a bigger change than what was asked for) and, for a self-service
    employee, to TODAY's own rows only — yesterday's log is closed to quiet
    edits the same way it's closed to deletes once locked, so history stays
    trustworthy. Admins bypass both the ownership and today-only checks,
    same precedent as delete_entry above; the day-lock check still applies
    to them only if they're not an admin, also matching delete_entry."""
    entry = db.get(m.TaskEntry, entry_id)
    if entry is None or (entry.employee_id != user.id and not user.is_admin):
        return RedirectResponse("/today", status_code=303)
    day = entry.date
    sub = db.execute(
        select(m.DaySubmission).where(
            m.DaySubmission.employee_id == entry.employee_id, m.DaySubmission.date == day
        )
    ).scalar_one_or_none()
    err = entry_details_edit_error(entry, user, today_local(), sub)
    if err:
        flash(request, err, "err")
        return RedirectResponse(f"/today?date={day.isoformat()}", status_code=303)
    cfg = engine.get_config(db)
    cleaned = (details or "").strip()
    min_chars = engine.cfg_int(cfg, "min_details_chars")
    if len(cleaned) < min_chars:
        flash(request, f"Details must be at least {min_chars} characters.", "err")
        return RedirectResponse(f"/today?date={day.isoformat()}", status_code=303)
    entry.details = cleaned
    db.commit()
    audit(db, user.name, "entry_details_edited", "TaskEntry", str(entry.id), {"date": day.isoformat()})
    flash(request, "Details updated.", "ok")
    return RedirectResponse(f"/today?date={day.isoformat()}", status_code=303)


@router.post("/breaks/{break_id}/edit")
def edit_break_details(
    break_id: int,
    request: Request,
    details: str = Form(""),
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Mirrors edit_entry_details() above, for the optional note on the
    auto-added 'General / Break' row (Ganesh, 2026-08-14 — employees asked
    for the same in-place edit a real task row's Details gets). Same
    ownership/today-only/day-lock rules via the shared
    entry_details_edit_error() guard. Unlike a TaskEntry's details, this
    has no minimum length: a break's note is optional context, not the
    only record of what happened, so clearing it back to blank is a valid
    edit, not an error."""
    brk = db.get(m.BreakEntry, break_id)
    if brk is None or (brk.employee_id != user.id and not user.is_admin):
        return RedirectResponse("/today", status_code=303)
    if brk.end_minute is None:
        # Still an open/running break — today.html only ever renders this
        # edit control for a completed break's display row, but a direct
        # POST could still reach here, so guard server-side too.
        return RedirectResponse("/today", status_code=303)
    day = brk.date
    sub = db.execute(
        select(m.DaySubmission).where(
            m.DaySubmission.employee_id == brk.employee_id, m.DaySubmission.date == day
        )
    ).scalar_one_or_none()
    err = entry_details_edit_error(brk, user, today_local(), sub)
    if err:
        flash(request, err, "err")
        return RedirectResponse(f"/today?date={day.isoformat()}", status_code=303)
    brk.details = (details or "").strip()
    db.commit()
    audit(db, user.name, "break_details_edited", "BreakEntry", str(brk.id), {"date": day.isoformat()})
    flash(request, "Break note updated.", "ok")
    return RedirectResponse(f"/today?date={day.isoformat()}", status_code=303)


@router.post("/break/start")
def start_break(
    request: Request,
    break_type: str = Form(m.BREAK_PERSONAL),
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Deliberately always 'today', regardless of what date the Today page
    happens to be viewing — a break is a live, right-now thing, not
    something you log after the fact."""
    today = today_local()
    break_type = break_type if break_type in m.BREAK_TYPES else m.BREAK_PERSONAL

    todays_breaks = list(
        db.execute(
            select(m.BreakEntry).where(
                m.BreakEntry.employee_id == user.id, m.BreakEntry.date == today
            )
        ).scalars()
    )
    if any(b.end_minute is None for b in todays_breaks):
        flash(request, "You're already on a break — end it before starting another.", "err")
        return RedirectResponse("/today", status_code=303)
    if break_type == m.BREAK_LUNCH_DINNER and any(
        b.break_type == m.BREAK_LUNCH_DINNER for b in todays_breaks
    ):
        flash(request, "Lunch/Dinner break is allowed once per day — you've already taken it today.", "err")
        return RedirectResponse("/today", status_code=303)

    now = now_local()
    db.add(m.BreakEntry(
        employee_id=user.id, date=today, break_type=break_type,
        start_minute=now.hour * 60 + now.minute,
        started_at=dt.datetime.utcnow(),  # explicit, full-precision — the
        # live timer needs real seconds, not just the truncated minute
    ))
    db.commit()
    return RedirectResponse("/today", status_code=303)


@router.post("/break/end")
def end_break(
    request: Request,
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    today = today_local()
    active = db.execute(
        select(m.BreakEntry).where(
            m.BreakEntry.employee_id == user.id, m.BreakEntry.date == today,
            m.BreakEntry.end_minute.is_(None),
        )
    ).scalar_one_or_none()
    if active is not None:
        now = now_local()
        active.end_minute = clamp_break_end(active.start_minute, now.hour * 60 + now.minute)
        active.ended_at = dt.datetime.utcnow()
        db.commit()
        flash(
            request,
            f"Break ended — {fmt_time(active.start_minute)}–{fmt_time(active.end_minute)} "
            f"({active.duration_minutes} min).",
            "ok",
        )
    return RedirectResponse("/today", status_code=303)


def _finish_task_timer(db: Session, user: m.Employee, timer: m.ActiveTaskTimer, cfg: dict):
    """Converts a running ActiveTaskTimer into a real TaskEntry, through
    the exact same validate_entry() every manually-typed row goes through
    (overlap / 4h-cap / details-length / locked-day checks) — an
    auto-captured entry is never held to looser rules than a typed one.
    Returns (True, None) on success; on failure returns (False, message)
    and leaves the timer running/untouched so nothing is silently lost —
    the employee can fix Details and try Stop again, or keep working."""
    now = now_local()
    end_minute = clamp_break_end(timer.start_minute, now.hour * 60 + now.minute)
    try:
        validate_entry(
            db, user, timer.date, timer.project_id, timer.task_type_id,
            timer.details, timer.start_minute, end_minute, cfg,
        )
    except EntryError as e:
        return False, "; ".join(e.errors)
    db.add(m.TaskEntry(
        employee_id=user.id, date=timer.date, project_id=timer.project_id,
        task_type_id=timer.task_type_id, details=timer.details.strip(),
        start_minute=timer.start_minute, end_minute=end_minute,
    ))
    db.delete(timer)
    db.commit()
    return True, None


@router.post("/task-timer/start")
def start_task_timer(
    request: Request,
    project_id: int = Form(...),
    task_type_id: int = Form(...),
    details: str = Form(""),
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Auto-captures the start time from the system clock — same
    right-now convention as Punch In and Break Start. Single active timer
    per employee (Ganesh, 2026-08-01): starting a new one auto-stops and
    saves whatever was already running as a real TaskEntry first, rather
    than allowing several to run at once (see ActiveTaskTimer docstring)."""
    cfg = engine.get_config(db)
    today = today_local()

    existing = db.execute(
        select(m.ActiveTaskTimer).where(m.ActiveTaskTimer.employee_id == user.id)
    ).scalar_one_or_none()
    if existing is not None:
        ok, error = _finish_task_timer(db, user, existing, cfg)
        if not ok:
            # can't silently drop the running timer's time — make the
            # employee resolve it (e.g. add Details) before starting a new one
            flash(request, f"Couldn't save the timer already running: {error}", "err")
            return RedirectResponse("/today", status_code=303)

    project = db.get(m.Project, project_id)
    task = db.get(m.TaskType, task_type_id)
    if project is None or not project.active or task is None or not task.active:
        flash(request, "Choose a Project and Task before starting the timer.", "err")
        return RedirectResponse("/today", status_code=303)

    now = now_local()
    db.add(m.ActiveTaskTimer(
        employee_id=user.id, date=today, project_id=project_id, task_type_id=task_type_id,
        details=details.strip(), start_minute=now.hour * 60 + now.minute,
        started_at=dt.datetime.utcnow(),
    ))
    db.commit()
    flash(request, "Timer started.", "ok")
    return RedirectResponse("/today", status_code=303)


@router.post("/task-timer/stop")
def stop_task_timer(
    request: Request,
    details: str = Form(""),
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    cfg = engine.get_config(db)
    active = db.execute(
        select(m.ActiveTaskTimer).where(m.ActiveTaskTimer.employee_id == user.id)
    ).scalar_one_or_none()
    if active is None:
        flash(request, "No timer is running.", "err")
        return RedirectResponse("/today", status_code=303)
    # top up Details at Stop time — the Start form may have been left blank
    # if the employee wasn't sure what to type until the work was done
    if details.strip():
        active.details = details.strip()
    ok, error = _finish_task_timer(db, user, active, cfg)
    if not ok:
        flash(request, error, "err")
        return RedirectResponse("/today", status_code=303)
    flash(request, "Timer stopped — entry logged.", "ok")
    return RedirectResponse("/today", status_code=303)


@router.post("/task-timer/cancel")
def cancel_task_timer(
    request: Request,
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Discards a running timer with no TaskEntry created — for a
    mis-started timer (wrong project/task picked), not a normal Stop."""
    active = db.execute(
        select(m.ActiveTaskTimer).where(m.ActiveTaskTimer.employee_id == user.id)
    ).scalar_one_or_none()
    if active is not None:
        db.delete(active)
        db.commit()
        flash(request, "Timer cancelled — no entry was logged.", "ok")
    return RedirectResponse("/today", status_code=303)


@router.post("/punch/in")
def punch_in(
    request: Request,
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Personal countdown timer only — see PunchSession's docstring.
    Deliberately always 'today', same reasoning as start_break: this is a
    live, right-now action, not something logged after the fact."""
    today = today_local()
    already_open = db.execute(
        select(m.PunchSession).where(
            m.PunchSession.employee_id == user.id, m.PunchSession.date == today,
            m.PunchSession.punched_out_at.is_(None),
        )
    ).scalar_one_or_none()
    if already_open is not None:
        flash(request, "You're already punched in.", "err")
        return RedirectResponse("/today", status_code=303)

    db.add(m.PunchSession(employee_id=user.id, date=today, punched_in_at=dt.datetime.utcnow()))
    db.commit()
    flash(request, "Punched in — timer's running.", "ok")
    return RedirectResponse("/today", status_code=303)


@router.post("/punch/out")
def punch_out(
    request: Request,
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    today = today_local()
    active = db.execute(
        select(m.PunchSession).where(
            m.PunchSession.employee_id == user.id, m.PunchSession.date == today,
            m.PunchSession.punched_out_at.is_(None),
        )
    ).scalar_one_or_none()
    if active is None:
        return RedirectResponse("/today", status_code=303)
    sub = db.execute(
        select(m.DaySubmission).where(
            m.DaySubmission.employee_id == user.id, m.DaySubmission.date == today
        )
    ).scalar_one_or_none()
    err = punch_out_error(sub)
    if err:
        flash(request, err, "err")
        return RedirectResponse("/today", status_code=303)
    active.punched_out_at = dt.datetime.utcnow()
    db.commit()
    flash(request, f"Punched out — {active.duration_minutes} min this session.", "ok")
    return RedirectResponse("/today", status_code=303)


@router.post("/submit-day")
def submit_day(
    request: Request,
    date: str = Form(...),
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    day = dt.date.fromisoformat(date)
    total = engine.day_total_minutes(db, user.id, day)
    if total == 0:
        flash(request, "Nothing to submit — log at least one task row.", "err")
        return RedirectResponse(f"/today?date={day.isoformat()}", status_code=303)
    sub = db.execute(
        select(m.DaySubmission).where(
            m.DaySubmission.employee_id == user.id, m.DaySubmission.date == day
        )
    ).scalar_one_or_none()
    if sub is not None and sub.locked:
        flash(request, "Day already submitted.", "err")
        return RedirectResponse(f"/today?date={day.isoformat()}", status_code=303)
    resubmit = sub is not None
    if sub is None:
        sub = m.DaySubmission(employee_id=user.id, date=day)
        db.add(sub)
    sub.total_minutes = total  # computed, never typed (PRD §4)
    sub.submitted_at = dt.datetime.utcnow()
    sub.locked = True
    db.commit()
    audit(
        db, user.name, "resubmit_day" if resubmit else "submit_day", "DaySubmission",
        f"{user.id}:{day.isoformat()}", {"total_minutes": total},
    )
    engine.recompute_employee(db, user, day, day)
    flash(request, f"Day submitted and locked — total {total // 60}:{total % 60:02d}.", "ok")
    return RedirectResponse(f"/today?date={day.isoformat()}", status_code=303)


@router.get("/my-month")
def my_month(
    request: Request,
    ym: Optional[str] = None,
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    from app.util import parse_ym, prev_next_month

    cfg = engine.get_config(db)
    year, month = parse_ym(ym)
    first, last = engine.month_range(year, month)
    # keep the visible month fresh
    engine.recompute_employee(db, user, first, min(last, today_local()), cfg)

    rows = {
        r.date: r
        for r in db.execute(
            select(m.DayStatus).where(
                m.DayStatus.employee_id == user.id, m.DayStatus.date.between(first, last)
            )
        ).scalars()
    }
    comp_erases = cfg.get("comp_erases_strike") == "1"
    strikes = engine.strikes_in(rows.values(), comp_erases)
    threshold = engine.cfg_int(cfg, "strike_threshold")

    # calendar weeks (Mon-first)
    weeks, week = [], [None] * 7
    d = first
    while d <= last:
        week[d.weekday()] = {"date": d, "row": rows.get(d)}
        if d.weekday() == 6:
            weeks.append(week)
            week = [None] * 7
        d += dt.timedelta(days=1)
    if any(week):
        weeks.append(week)

    # leave by category (PRD §5: computed, not typed) — approved only; a
    # pending or rejected request was never actually taken.
    leave_totals = {}
    for lv in db.execute(
        select(m.LeaveRecord).where(
            m.LeaveRecord.employee_id == user.id,
            m.LeaveRecord.start_date <= last,
            m.LeaveRecord.end_date >= first,
            m.LeaveRecord.status == m.LEAVE_APPROVED,
        )
    ).scalars():
        d = max(lv.start_date, first)
        while d <= min(lv.end_date, last):
            per_day = lv.minutes_per_day if lv.minutes_per_day is not None else user.daily_target_minutes
            frac = per_day / user.daily_target_minutes if user.daily_target_minutes else 1
            leave_totals[lv.type] = leave_totals.get(lv.type, 0) + min(frac, 1.0)
            d += dt.timedelta(days=1)

    ledger = engine.running_ledger(db, user, first, min(last, today_local()))
    balance = ledger[-1]["balance"] if ledger else 0
    comp = compensation.monthly_summary(db, user, year, month)
    (py, pm), (ny, nm) = prev_next_month(year, month)

    # read-only lookback (Ganesh, 2026-08-13): employees can't edit a past
    # day's rows outside today, but they should still be able to see what
    # they logged. One query for the whole month, grouped by date, so the
    # ledger table below can expand each row in place with no extra route.
    task_entries_by_date = {}
    for e in db.execute(
        select(m.TaskEntry)
        .where(m.TaskEntry.employee_id == user.id, m.TaskEntry.date.between(first, last))
        .order_by(m.TaskEntry.date, m.TaskEntry.start_minute)
    ).scalars():
        task_entries_by_date.setdefault(e.date, []).append(e)

    # completed breaks merge into the same per-day view as General/Break
    # rows (Ganesh, 2026-08-14) — see _BreakLogRow's docstring; same
    # display-only merge Today's task log uses, so a day's "View" here
    # matches what was actually on Today for that date.
    breaks_by_date = {}
    for b in db.execute(
        select(m.BreakEntry)
        .where(
            m.BreakEntry.employee_id == user.id,
            m.BreakEntry.date.between(first, last),
            m.BreakEntry.end_minute.isnot(None),
        )
        .order_by(m.BreakEntry.date, m.BreakEntry.start_minute)
    ).scalars():
        breaks_by_date.setdefault(b.date, []).append(b)

    entries_by_date = {
        d: _merge_entries_and_breaks(task_entries_by_date.get(d, []), breaks_by_date.get(d, []))
        for d in set(task_entries_by_date) | set(breaks_by_date)
    }

    return render(
        request,
        "my_month.html",
        {
            "user": user,
            "year": year,
            "month": month,
            "weeks": weeks,
            "strikes": strikes,
            "threshold": threshold,
            "comp_erases": comp_erases,
            "leave_totals": leave_totals,
            "ledger": ledger,
            "balance": balance,
            "entries_by_date": entries_by_date,
            "comp": comp,
            "prev_ym": f"{py}-{pm:02d}",
            "next_ym": f"{ny}-{nm:02d}",
            "today": today_local(),
        },
    )


# --------------------------------------------------------------------------
# Leave: self-service request (PRD open question 5 — employees request,
# admin approves; see app/routes/admin.py for the approval queue).
# --------------------------------------------------------------------------
@router.get("/leave")
def my_leave(
    request: Request,
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    records = list(
        db.execute(
            select(m.LeaveRecord)
            .where(m.LeaveRecord.employee_id == user.id)
            .order_by(m.LeaveRecord.start_date.desc())
        ).scalars()
    )
    today = today_local()
    return render(
        request, "leave.html",
        {
            "user": user, "records": records, "leave_types": m.LEAVE_TYPES, "today": today,
            "balance": engine.leave_balance(db, user, today.year), "balance_year": today.year,
        },
    )


@router.post("/leave/request")
def request_leave(
    request: Request,
    start_date: str = Form(...),
    end_date: str = Form(""),
    type: str = Form("Other"),
    hours: str = Form(""),
    note: str = Form(""),
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    try:
        start = parse_date_field(start_date, "Start date")
        end = parse_date_field(end_date, "End date") if end_date else start
    except FormError as e:
        flash(request, e.message, "err")
        return RedirectResponse("/leave", status_code=303)
    if end < start:
        flash(request, "End date is before start date.", "err")
        return RedirectResponse("/leave", status_code=303)
    minutes = None  # full day = your daily target (PRD §5)
    if hours.strip():
        try:
            minutes = int(round(float(hours) * 60))
        except ValueError:
            flash(request, "Hours must be a number (leave blank for a full day).", "err")
            return RedirectResponse("/leave", status_code=303)
    if type == "Other" and not note.strip():
        flash(request, "'Other' leave needs a note.", "err")
        return RedirectResponse("/leave", status_code=303)
    lv = m.LeaveRecord(
        employee_id=user.id, start_date=start, end_date=end, type=type,
        minutes_per_day=minutes, note=note.strip(), entered_by=user.name,
        status=m.LEAVE_REQUESTED,  # awaits admin approval — doesn't affect
        # compliance math until then (see engine.leave_minutes_on)
    )
    db.add(lv)
    db.commit()
    audit(db, user.name, "leave_requested", "LeaveRecord", lv.id,
          {"range": f"{start}..{end}", "type": type, "minutes": minutes})
    flash(request, "Leave request submitted — an admin will review it.", "ok")
    return RedirectResponse("/leave", status_code=303)


@router.post("/leave/{leave_id}/cancel")
def cancel_leave_request(
    leave_id: int,
    request: Request,
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Employees may withdraw their own still-pending request. Anything
    already approved/rejected needs an admin (it's already been acted on)."""
    lv = db.get(m.LeaveRecord, leave_id)
    if lv is None or lv.employee_id != user.id or lv.status != m.LEAVE_REQUESTED:
        flash(request, "That request can no longer be withdrawn.", "err")
        return RedirectResponse("/leave", status_code=303)
    db.delete(lv)
    db.commit()
    audit(db, user.name, "leave_request_withdrawn", "LeaveRecord", leave_id, {})
    flash(request, "Request withdrawn.", "ok")
    return RedirectResponse("/leave", status_code=303)


# --------------------------------------------------------------------------
# Overtime: self-service pre-approval request (Ganesh's manager, 2026-08-03
# — exact same submit -> lead/admin queue -> lead/admin acts shape as Leave
# above; see app/routes/admin.py for the approval queue and app/auth.py's
# led_by() for who it's routed to).
# --------------------------------------------------------------------------
@router.get("/overtime")
def my_overtime(
    request: Request,
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    records = list(
        db.execute(
            select(m.OvertimeApproval)
            .where(m.OvertimeApproval.employee_id == user.id)
            .order_by(m.OvertimeApproval.start_date.desc())
        ).scalars()
    )
    return render(request, "overtime.html", {"user": user, "records": records})


@router.post("/overtime/request")
def request_overtime(
    request: Request,
    start_date: str = Form(...),
    end_date: str = Form(""),
    note: str = Form(""),
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    try:
        start = parse_date_field(start_date, "Start date")
        end = parse_date_field(end_date, "End date") if end_date else start
    except FormError as e:
        flash(request, e.message, "err")
        return RedirectResponse("/overtime", status_code=303)
    if end < start:
        flash(request, "End date is before start date.", "err")
        return RedirectResponse("/overtime", status_code=303)
    ot = m.OvertimeApproval(
        employee_id=user.id, start_date=start, end_date=end,
        note=note.strip(), requested_by=user.name,
        status=m.OT_REQUESTED,  # awaits lead/admin review — doesn't block
        # logging time or Punch In/Out either way, see model docstring
    )
    db.add(ot)
    db.commit()
    audit(db, user.name, "overtime_requested", "OvertimeApproval", ot.id,
          {"range": f"{start}..{end}"})
    flash(request, "Overtime request submitted — your lead will review it.", "ok")
    return RedirectResponse("/overtime", status_code=303)


@router.post("/overtime/{ot_id}/cancel")
def cancel_overtime_request(
    ot_id: int,
    request: Request,
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Employees may withdraw their own still-pending request. Anything
    already approved/rejected needs a lead/admin (it's already been acted
    on) — same rule as Leave's cancel_leave_request above."""
    ot = db.get(m.OvertimeApproval, ot_id)
    if ot is None or ot.employee_id != user.id or ot.status != m.OT_REQUESTED:
        flash(request, "That request can no longer be withdrawn.", "err")
        return RedirectResponse("/overtime", status_code=303)
    db.delete(ot)
    db.commit()
    audit(db, user.name, "overtime_request_withdrawn", "OvertimeApproval", ot_id, {})
    flash(request, "Request withdrawn.", "ok")
    return RedirectResponse("/overtime", status_code=303)


# --------------------------------------------------------------------------
# Support: employee submits a question, admin sees it in /admin/support
# (same submit -> admin-queue -> admin-acts shape as leave requests above).
# --------------------------------------------------------------------------
MIN_SUPPORT_MESSAGE_CHARS = 5


@router.get("/support")
def support_page(
    request: Request,
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    records = list(
        db.execute(
            select(m.SupportQuery)
            .where(m.SupportQuery.employee_id == user.id)
            .order_by(m.SupportQuery.created_at.desc())
        ).scalars()
    )
    return render(request, "support.html", {"user": user, "records": records})


@router.get("/holidays")
def holidays_page(
    request: Request,
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    """One shared company-wide holiday calendar (Ganesh, 2026-08-14 — see
    Holiday's docstring in app/models.py; briefly split per-country on
    2026-08-12, reverted the same week)."""
    if not HOLIDAY_MANAGEMENT_ENABLED:
        raise HTTPException(status_code=404)
    holidays = list(db.execute(select(m.Holiday).order_by(m.Holiday.date)).scalars())
    return render(request, "holidays.html", {"user": user, "holidays": holidays})


@router.post("/suggestions")
def suggest_list_item(
    request: Request,
    kind: str = Form(...),
    name: str = Form(...),
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Employee/lead-suggested Project or Task (Ganesh, 2026-08-01) —
    invisible and unusable by anyone, including whoever suggested it (see
    _visible_projects_and_tasks and validate_entry above), until a team
    lead/admin approves it (see app/routes/admin.py suggestions_page /
    suggestion_approve). Ganesh, 2026-08-11: this used to be usable by the
    submitter right away; removed after an admin reported unreviewed
    suggestions ending up on real logged time before review."""
    name = name.strip()
    if not name:
        flash(request, "Enter a name before suggesting it.", "err")
        return RedirectResponse("/today", status_code=303)
    model = {"project": m.Project, "task": m.TaskType}.get(kind)
    if model is None:
        flash(request, "Unknown suggestion type.", "err")
        return RedirectResponse("/today", status_code=303)
    existing = db.execute(select(model).where(model.name == name)).scalar_one_or_none()
    if existing is not None:
        flash(request, f"'{name}' already exists — pick it from the list instead of suggesting it again.", "err")
        return RedirectResponse("/today", status_code=303)
    db.add(model(name=name, active=True, status=m.LIST_PENDING, created_by_employee_id=user.id))
    db.commit()
    label = "Project" if kind == "project" else "Task"
    flash(request, f"{label} '{name}' suggested — a team lead will review it before it's usable.", "ok")
    return RedirectResponse("/today", status_code=303)


@router.post("/support/submit")
def support_submit(
    request: Request,
    message: str = Form(...),
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    message = message.strip()
    if len(message) < MIN_SUPPORT_MESSAGE_CHARS:
        flash(request, f"Please describe your question (at least {MIN_SUPPORT_MESSAGE_CHARS} characters).", "err")
        return RedirectResponse("/support", status_code=303)
    q = m.SupportQuery(employee_id=user.id, message=message)
    db.add(q)
    db.commit()
    audit(db, user.name, "support_query_submitted", "SupportQuery", q.id, {"message": message[:200]})
    flash(request, "Sent to an admin — you'll see their reply here.", "ok")
    return RedirectResponse("/support", status_code=303)


# --------------------------------------------------------------------------
# Profile photo
# --------------------------------------------------------------------------
@router.get("/profile")
def profile_page(
    request: Request,
    user: m.Employee = Depends(current_user),
):
    return render(
        request, "profile.html",
        {"user": user, "pd": user.personal_details, "bd": user.bank_details, "locations": m.LOCATIONS},
    )


@router.post("/profile/photo")
def upload_photo(
    request: Request,
    photo: UploadFile = File(...),
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    ext = ALLOWED_PHOTO_TYPES.get(photo.content_type)
    if ext is None:
        flash(request, "Please upload a JPEG, PNG, or WebP image.", "err")
        return RedirectResponse("/profile", status_code=303)
    data = photo.file.read(MAX_PHOTO_BYTES + 1)
    if len(data) > MAX_PHOTO_BYTES:
        flash(request, "Photo must be under 2 MB.", "err")
        return RedirectResponse("/profile", status_code=303)
    if not data:
        flash(request, "That file looked empty — try again.", "err")
        return RedirectResponse("/profile", status_code=303)

    os.makedirs(AVATAR_DIR, exist_ok=True)
    # one file per employee — replace whatever extension was there before
    # so old uploads don't pile up on disk
    if user.photo_path:
        old_path = os.path.join(AVATAR_DIR, user.photo_path)
        if os.path.exists(old_path):
            os.remove(old_path)
    filename = f"{user.id}{ext}"
    with open(os.path.join(AVATAR_DIR, filename), "wb") as f:
        f.write(data)
    user.photo_path = filename
    db.commit()
    flash(request, "Profile photo updated.", "ok")
    return RedirectResponse("/profile", status_code=303)


@router.post("/profile/location")
def update_location(
    request: Request,
    location: str = Form(...),
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Self-service country (Ganesh, 2026-08-12). No longer drives Holiday
    scoping as of 2026-08-14 — holidays are one shared company-wide list
    now (see Holiday's docstring in app/models.py) — this is just the
    employee's own profile metadata at this point. Left in place (kept
    gated behind HOLIDAY_MANAGEMENT_ENABLED, unchanged) since it was built
    together with Holiday Management and nothing currently needs it
    removed."""
    if not HOLIDAY_MANAGEMENT_ENABLED:
        raise HTTPException(status_code=404)
    if location not in m.LOCATIONS:
        flash(request, "Choose a valid country.", "err")
        return RedirectResponse("/profile", status_code=303)
    if location != user.location:
        user.location = location
        db.commit()
        audit(db, user.name, "location_change", "Employee", str(user.id), {"location": location})
        flash(request, f"Country set to {location}.", "ok")
    return RedirectResponse("/profile", status_code=303)


# --------------------------------------------------------------------------
# Profile: Personal Details — personal info + contact info in one card, per
# Ganesh's instruction. Employee.date_of_birth/country_code/phone already
# exist and are edited here too rather than duplicated into the new table;
# Employee.email ("Company Email") is deliberately NOT editable from this
# form — it's the /signup match key (see app/routes/auth.py), so changing
# it here could silently break the employee's own login or collide with
# someone else's roster row. It's shown read-only with a pointer to admin.
# --------------------------------------------------------------------------
@router.get("/profile/personal-details")
def personal_details_page(
    request: Request,
    user: m.Employee = Depends(current_user),
):
    return render(request, "profile_personal_details.html", {"user": user, "pd": user.personal_details})


@router.post("/profile/personal-details")
def personal_details_save(
    request: Request,
    date_of_birth: str = Form(""),
    country_code: str = Form(""),
    phone: str = Form(""),
    blood_type: str = Form(""),
    gender: str = Form(""),
    marital_status: str = Form(""),
    family_members: str = Form(""),
    nationality: str = Form(""),
    hobbies: str = Form(""),
    professional_skills: str = Form(""),
    special_skills: str = Form(""),
    known_languages: str = Form(""),
    company_contact: str = Form(""),
    alternate_phone: str = Form(""),
    emergency_phone: str = Form(""),
    whatsapp_number: str = Form(""),
    personal_email: str = Form(""),
    current_address: str = Form(""),
    permanent_address: str = Form(""),
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    if date_of_birth.strip():
        try:
            user.date_of_birth = parse_date_field(date_of_birth, "Date of birth")
        except FormError as e:
            flash(request, e.message, "err")
            return RedirectResponse("/profile/personal-details", status_code=303)
    else:
        user.date_of_birth = None
    user.country_code = country_code.strip() or None
    user.phone = phone.strip() or None

    family_members_val = None
    if family_members.strip():
        try:
            family_members_val = parse_int_field(family_members, "Number of family members")
        except FormError as e:
            flash(request, e.message, "err")
            return RedirectResponse("/profile/personal-details", status_code=303)

    pd = user.personal_details
    if pd is None:
        pd = m.EmployeePersonalDetails(employee_id=user.id)
        db.add(pd)
    pd.blood_type = blood_type.strip() or None
    pd.gender = gender.strip() or None
    pd.marital_status = marital_status.strip() or None
    pd.family_members = family_members_val
    pd.nationality = nationality.strip() or None
    pd.hobbies = hobbies.strip() or None
    pd.professional_skills = professional_skills.strip() or None
    pd.special_skills = special_skills.strip() or None
    pd.known_languages = known_languages.strip() or None
    pd.company_contact = company_contact.strip() or None
    pd.alternate_phone = alternate_phone.strip() or None
    pd.emergency_phone = emergency_phone.strip() or None
    pd.whatsapp_number = whatsapp_number.strip() or None
    pd.personal_email = personal_email.strip() or None
    pd.current_address = current_address.strip() or None
    pd.permanent_address = permanent_address.strip() or None
    pd.updated_at = dt.datetime.utcnow()
    pd.updated_by = user.name

    db.commit()
    audit(db, user.name, "profile_personal_details_updated", "Employee", user.id, {})
    flash(request, "Personal details saved.", "ok")
    return RedirectResponse("/profile", status_code=303)


# --------------------------------------------------------------------------
# Profile: Employment Details — bank account + statutory IDs (PAN/Aadhaar/
# UAN/ESI). Sensitive fields are never sent back to the browser once saved
# (see profile_employment_details.html) — the form field for each starts
# blank with a masked "currently set: ...1234" hint next to it, so a blank
# submission means "leave this one alone", not "clear it". Non-sensitive
# fields (holder name, bank/branch name, account type, IFSC) are prefilled
# and behave like every other edit form in the app: blank clears them.
# --------------------------------------------------------------------------
@router.get("/profile/employment-details")
def employment_details_page(
    request: Request,
    user: m.Employee = Depends(current_user),
):
    return render(request, "profile_employment_details.html", {"user": user, "bd": user.bank_details})


@router.post("/profile/employment-details")
def employment_details_save(
    request: Request,
    account_holder_name: str = Form(""),
    account_number: str = Form(""),
    ifsc_code: str = Form(""),
    bank_name: str = Form(""),
    branch_name: str = Form(""),
    account_type: str = Form(""),
    pan_number: str = Form(""),
    aadhaar_number: str = Form(""),
    uan_number: str = Form(""),
    esi_number: str = Form(""),
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    bd = user.bank_details
    if bd is None:
        bd = m.EmployeeBankDetails(employee_id=user.id)
        db.add(bd)

    bd.account_holder_name = account_holder_name.strip() or None
    bd.ifsc_code = ifsc_code.strip().upper() or None
    bd.bank_name = bank_name.strip() or None
    bd.branch_name = branch_name.strip() or None
    bd.account_type = account_type.strip() or None

    # blank = "leave unchanged" for every sensitive field — see docstring above
    if account_number.strip():
        bd.account_number = account_number.strip()
    if pan_number.strip():
        bd.pan_number = pan_number.strip().upper()
    if aadhaar_number.strip():
        bd.aadhaar_number = aadhaar_number.strip()
    if uan_number.strip():
        bd.uan_number = uan_number.strip()
    if esi_number.strip():
        bd.esi_number = esi_number.strip()

    bd.updated_at = dt.datetime.utcnow()
    bd.updated_by = user.name
    db.commit()
    # detail dict deliberately empty — never write sensitive values into the
    # audit log, even a masked one; "something changed, by whom, when" is
    # all the trail needs.
    audit(db, user.name, "profile_employment_details_updated", "Employee", user.id, {})
    flash(request, "Employment details saved.", "ok")
    return RedirectResponse("/profile", status_code=303)
