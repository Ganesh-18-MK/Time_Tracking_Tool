"""Employee screens: Today (log + submit) and My Month (PRD §7)."""
import datetime as dt
import json
import os
from types import SimpleNamespace
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import compensation, engine, models as m
from app.auth import current_user
from app.db import get_db
from app.templating import HOLIDAY_MANAGEMENT_ENABLED, LEAVE_MANAGEMENT_V2_ENABLED, flash, render
from app.util import (
    FormError,
    audit,
    capitalize_first,
    clamp_break_end,
    fmt_time,
    normalize_title_case,
    now_local,
    overtime_minutes,
    overtime_row_flags,
    parse_date_field,
    parse_hhmm,
    parse_int_field,
    punch_out_error,
    punch_remaining_minutes,
    today_local,
)
from app.validation import (
    EntryError,
    all_gap_windows,
    earliest_allowed_date,
    earliest_gap_window,
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


class _GapLogRow:
    """Read-only placeholder row for a still-unexplained gap between two
    logged rows (Ganesh, 2026-08-22) — same `id = None` convention
    _BreakLogRow uses above, so every place that keys off entry.id (edit/
    delete controls, gap_flags' dict lookup, the overtime-row coloring
    loop) treats this as a no-op for free, without templates needing to
    know this class exists.

    Only ever added to display_entries for the live "today, not yet
    submitted" view (see _day_context's day_unlocked_today) — a past day,
    or today once locked, shows the plain ⚠ warning label same as always
    but no fillable row, since there's nowhere left to post an Add Row to.
    today.html's "Fill" button on this row is pure client-side JS
    (fillGapRow()) that pre-fills the existing Add Row form's Start/End
    with this exact window and scrolls to it — the same mechanism the
    single earliest-gap auto-prefill already used (see earliest_gap_window
    above), just triggered per-gap instead of only for the first one.
    Filling part of a gap and leaving the rest is exactly how "split a gap
    into more than one entry" works here: whatever's left over just shows
    up as a new, smaller gap row on the next page load — no separate
    multi-segment UI needed."""

    def __init__(self, window: dict):
        self.id = None
        self.is_gap = True
        self.start_minute = window["start"]
        self.end_minute = window["end"]

    @property
    def duration_minutes(self) -> int:
        return self.end_minute - self.start_minute


def _merge_entries_and_breaks(entries, breaks, gaps=None) -> list:
    """Combine real TaskEntry rows with completed BreakEntry rows (and,
    optionally, fillable gap placeholder rows — see _GapLogRow above) into
    one chronological, display-only list. Callers keep using the original
    `entries`/`breaks` lists, unchanged, for every accounting purpose (day
    total, target, gap_flags, compensation, overtime, strikes) — this
    merged list exists purely for what the employee sees in the task log
    table / My Month's per-day expand. `gaps` defaults to None (existing
    My Month call site is unaffected) — only today_page's live view passes
    it, and only when day_unlocked_today (see _day_context)."""
    rows = (
        list(entries)
        + [_BreakLogRow(b) for b in breaks if b.end_minute is not None]
        + [_GapLogRow(g) for g in (gaps or ())]
    )
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

    # Fillable gap placeholder rows (Ganesh, 2026-08-22) — every currently-
    # unexplained gap (see all_gap_windows() in app/validation.py), not
    # just the single earliest one gap_prefill_start/_end already surface
    # in today_page(). Only computed/shown for the live, still-editable
    # "today" view — a past day, or today once submitted and locked, has
    # nowhere left to post a fill to, so it keeps the plain ⚠ warning label
    # only (flags above), same as before this existed. See _GapLogRow.
    day_unlocked_today = date == today_local() and not (sub is not None and sub.locked)
    gap_windows = (
        all_gap_windows(entries, engine.cfg_int(cfg, "gap_flag_minutes"), completed_breaks)
        if day_unlocked_today else []
    )

    # Overtime-colored task log rows (Ganesh, 2026-08-21) — a row is styled
    # differently once the running total of everything logged BEFORE it
    # already reached the day's target, so hours worked past the (leave/
    # break-adjusted) 8h target read visually distinct from the regular
    # workday, without waiting for Submit Day or a separate report. The
    # actual cumulative-sum math lives in util.overtime_row_flags() (pure,
    # independently tested) — `entries` is already ordered by start_minute
    # (see the query above), and `.is_overtime` is a transient Python
    # attribute, not a mapped column, never persisted, set directly on the
    # same TaskEntry objects `entries` and `display_entries` both point at.
    # Only real TaskEntry rows count toward the running total (never break
    # time — `target` itself is already break/leave-adjusted, see above).
    for e, is_ot in zip(entries, overtime_row_flags([e.duration_minutes for e in entries], target)):
        e.is_overtime = is_ot

    # Task Planning "Today's Plan" (Ganesh, 2026-08-21) — the interactive
    # Start/Pause/Resume/Stop card is always "today" only, same live/
    # right-now convention as breaks/punch/Auto time capture above (see
    # docs/TASK_PLANNING_TIMER_PLAN.md): a plan only ever carries the date
    # it was added on, so there's nothing live to control on a past day.
    # `past_plans` (Ganesh, 2026-08-22) is the read-only counterpart for
    # browsing a past day via the date dropdown — same PlannedTask rows,
    # no forms/buttons, just what was planned/done that day for context.
    plans = []
    past_plans = []
    if date == today_local():
        plans = list(
            db.execute(
                select(m.PlannedTask)
                .where(m.PlannedTask.employee_id == emp.id, m.PlannedTask.date == date)
                .order_by(m.PlannedTask.created_at)
            ).scalars()
        )
    else:
        past_plans = list(
            db.execute(
                select(m.PlannedTask)
                .where(m.PlannedTask.employee_id == emp.id, m.PlannedTask.date == date)
                .order_by(m.PlannedTask.created_at)
            ).scalars()
        )

    return {
        "entries": entries,
        "display_entries": _merge_entries_and_breaks(entries, completed_breaks, gap_windows),
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
        "plans": plans,
        "past_plans": past_plans,
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


def _pending_edit_notices(db: Session, user: m.Employee) -> list:
    """Suggestions this employee submitted that an admin has since rewritten
    (Ganesh, 2026-08-21 — see app/routes/admin.py suggestion_edit()) and
    that haven't been shown to them yet. Each item is a plain dict — kind
    ("project"/"task"), the row's id (for the dismiss POST), its current
    name, original_name (what the employee themselves typed, captured once
    on the first edit), and who made the change. Ordered oldest-edit-first
    so if an admin has rewritten more than one of an employee's
    suggestions, they see them in the order the edits happened."""
    notices = []
    for kind, model in (("project", m.Project), ("task", m.TaskType)):
        rows = db.execute(
            select(model).where(
                model.created_by_employee_id == user.id,
                model.edited_at.isnot(None),
                model.employee_notified_at.is_(None),
            ).order_by(model.edited_at)
        ).scalars()
        for row in rows:
            notices.append(
                {
                    "kind": kind,
                    "id": row.id,
                    "name": row.name,
                    "original_name": row.original_name,
                    "edited_by": row.edited_by,
                }
            )
    return notices


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
    today = today_local()
    day = dt.date.fromisoformat(date) if date else today
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

    # Suggestion-edit notices (Ganesh, 2026-08-21) — see
    # _pending_edit_notices() above. Shown on the "today" view only (same
    # "right now" convention as the other Today banners below) rather than
    # while browsing a past day via the date dropdown.
    edit_notices = _pending_edit_notices(db, user) if day == today else []

    # Gap auto-prefill (Ganesh, 2026-08-21): previously an unexplained 15+
    # min gap between two already-logged rows only showed the ⚠ warning
    # label on the later row (see today.html's flags.get(e.id) below) —
    # the employee still had to notice it, then manually retype the right
    # start/end times into Add Row themselves. Now the Add Row form is
    # pre-scoped to the EARLIEST still-unexplained gap automatically: Start
    # becomes the end of the row right before the gap, End becomes the
    # start of the row right after it. ctx["entries"] is already ordered
    # by start_minute (see _day_context's query), so the first flagged
    # entry walking forward is the earliest gap. Only the gap strictly
    # *between* two logged rows is handled here — the common "haven't
    # logged anything since my last row yet" case is a different,
    # right-open situation already covered by suggest_start/last_end below
    # (nothing to "fill in" there yet, since there's no second row to be
    # a gap *before*).
    #
    # Deliberately skipped when a failed Add Row submission is already
    # being reopened (reopen_start present, from add_entry()'s _reopen())
    # — the employee's own just-typed, possibly-corrected values always
    # win over an auto-suggestion.
    gap_prefill_start = None
    gap_prefill_end = None
    if day == today and ctx["flags"] and not reopen_start:
        window = earliest_gap_window(ctx["entries"], ctx["flags"])
        if window is not None:
            gap_prefill_start, gap_prefill_end = window

    # Immediate overtime prompt (Ganesh, 2026-08-21): fires off total logged
    # Task Entry minutes for *today* crossing today's target (ctx["total"]/
    # ctx["target"], both already leave/break-excess-adjusted by
    # _day_context above) — not the separate Punch Clock live timer, which
    # only tracks employees actively using Punch In/Out (see punch_overtime
    # above). Only ever computed for day == today, same "right now only"
    # convention as Start Break/Punch In. Suppressed once a request already
    # covers today (requested OR approved) so saving a second row a minute
    # later doesn't re-nag — see the one-click form in today.html that
    # posts straight to the existing /overtime/request route.
    show_overtime_prompt = False
    over_allocation_minutes = 0
    if day == today:
        over_allocation_minutes = max(0, ctx["total"] - ctx["target"])
        if over_allocation_minutes > 0:
            already_requested = db.execute(
                select(m.OvertimeApproval.id).where(
                    m.OvertimeApproval.employee_id == user.id,
                    m.OvertimeApproval.start_date <= today,
                    m.OvertimeApproval.end_date >= today,
                    m.OvertimeApproval.status.in_((m.OT_REQUESTED, m.OT_APPROVED)),
                )
            ).first()
            show_overtime_prompt = already_requested is None

    # Punch-out reminder popup (Ganesh, 2026-08-21): Punch Out is already
    # blocked until Submit Day locks the day (see util.punch_out_error) —
    # this is the other half of that, a nudge the moment it becomes
    # possible. Fires only once Submit Day has actually happened AND
    # there's still an open PunchSession sitting there (nothing to remind
    # about if they never punched in at all today). No session-dismissal
    # bookkeeping needed, unlike the profile-completion popup: punching out
    # sets active_punch to None on the very next page load, which alone
    # stops this from showing again — a "Not now" close button in the
    # template is purely client-side (see today.html), safe to reappear on
    # the next reload since the condition re-evaluates fresh every time.
    show_punch_out_reminder = day == today and ctx["sub"] is not None and ctx["sub"].locked and ctx["active_punch"] is not None

    ctx.update(
        {
            "over_allocation_minutes": over_allocation_minutes,
            "show_overtime_prompt": show_overtime_prompt,
            "show_punch_out_reminder": show_punch_out_reminder,
            "edit_notices": edit_notices,
            "gap_prefill_active": gap_prefill_start is not None,
            "gap_prefill_start_min": gap_prefill_start,
            "gap_prefill_end_min": gap_prefill_end,
            # Priority order: a sticky reopen (Ganesh, 2026-08-14 — see
            # add_entry()'s _reopen()) — the employee's own just-typed
            # values — wins over an auto-detected gap, which in turn wins
            # over the plain "start right after my last row" default.
            "suggest_start": reopen_start or (
                f"{gap_prefill_start // 60:02d}:{gap_prefill_start % 60:02d}" if gap_prefill_start is not None
                else (f"{last_end // 60:02d}:{last_end % 60:02d}" if last_end else "")
            ),
            "reopen_project_id": reopen_project_id,
            "reopen_task_type_id": reopen_task_type_id,
            "reopen_details": reopen_details,
            "reopen_end": reopen_end or (
                f"{gap_prefill_end // 60:02d}:{gap_prefill_end % 60:02d}" if gap_prefill_end is not None else None
            ),
            "user": user,
            "day": day,
            "today": today,
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
                details=capitalize_first(details.strip()),
                start_minute=start_minute,
                end_minute=end_minute,
                entry_method=m.ENTRY_METHOD_MANUAL,
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
    entry.details = capitalize_first(cleaned)
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
    the employee can fix Details and try Stop again, or keep working.

    entry_method (Ganesh, 2026-08-21, usage tracking) is set from
    timer.planned_task_id: this one function is the single place both
    Auto time capture's Stop AND every Plan Pause/Stop segment finish
    through, so it's the one place that can tell them apart reliably —
    plan-linked timers stamp ENTRY_METHOD_PLAN, ad-hoc ones stamp
    ENTRY_METHOD_AUTO_TIMER."""
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
        task_type_id=timer.task_type_id, details=capitalize_first(timer.details.strip()),
        start_minute=timer.start_minute, end_minute=end_minute,
        entry_method=m.ENTRY_METHOD_PLAN if timer.planned_task_id else m.ENTRY_METHOD_AUTO_TIMER,
    ))
    db.delete(timer)
    db.commit()
    return True, None


def _stop_current_timer_if_any(db: Session, user: m.Employee, cfg: dict):
    """Shared by start_task_timer below and /plan/{id}/start (Task
    Planning, Ganesh, 2026-08-21): whatever timer is currently running —
    ad-hoc or plan-linked — gets auto-finished into a real TaskEntry before
    a new one starts, same "starting a new one auto-stops the old one"
    rule Auto time capture already had (see ActiveTaskTimer docstring).
    The only thing new here is that if the timer being auto-stopped was
    linked to a PlannedTask (planned_task_id set), that plan goes back to
    PLAN_PAUSED rather than being silently left `running` with no active
    timer behind it — it wasn't explicitly Stopped, just interrupted, so
    Resume should still be offered. Returns (ok, error) — same shape
    _finish_task_timer already returns, just with this one extra side
    effect layered on top; ad-hoc timers (planned_task_id is None) behave
    exactly as before, zero extra writes."""
    existing = db.execute(
        select(m.ActiveTaskTimer).where(m.ActiveTaskTimer.employee_id == user.id)
    ).scalar_one_or_none()
    if existing is None:
        return True, None
    interrupted_plan_id = existing.planned_task_id
    ok, error = _finish_task_timer(db, user, existing, cfg)
    if ok and interrupted_plan_id is not None:
        plan = db.get(m.PlannedTask, interrupted_plan_id)
        if plan is not None and plan.status == m.PLAN_RUNNING:
            plan.status = m.PLAN_PAUSED
            db.commit()
    return ok, error


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

    ok, error = _stop_current_timer_if_any(db, user, cfg)
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
    # Captured before _finish_task_timer deletes `active` below (Ganesh,
    # 2026-08-22) — today.html no longer offers this Stop&Log button for a
    # plan-linked timer (see the Auto time capture card, which shows
    # Pause/Stop posting straight to /plan/{id}/pause|stop instead), but
    # this route is still reachable directly (stale form, direct POST), so
    # it needs to keep PlannedTask.status in sync itself rather than
    # relying on the UI alone — otherwise a plan whose segment got stopped
    # here stayed stuck showing "running" with a dead timer behind it.
    plan_id = active.planned_task_id
    ok, error = _finish_task_timer(db, user, active, cfg)
    if not ok:
        flash(request, error, "err")
        return RedirectResponse("/today", status_code=303)
    if plan_id is not None:
        plan = db.get(m.PlannedTask, plan_id)
        if plan is not None and plan.status == m.PLAN_RUNNING:
            plan.status = m.PLAN_DONE
            db.commit()
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
        # Same reasoning as stop_task_timer above (Ganesh, 2026-08-22) — a
        # plan-linked timer that gets discarded here (rather than through
        # /plan/{id}/pause|stop) shouldn't leave that plan stuck showing
        # "running" with nothing behind it; back to `paused` is the same
        # state _stop_current_timer_if_any already puts an interrupted
        # plan into elsewhere, so Resume is still offered.
        plan_id = active.planned_task_id
        db.delete(active)
        if plan_id is not None:
            plan = db.get(m.PlannedTask, plan_id)
            if plan is not None and plan.status == m.PLAN_RUNNING:
                plan.status = m.PLAN_PAUSED
        db.commit()
        flash(request, "Timer cancelled — no entry was logged.", "ok")
    return RedirectResponse("/today", status_code=303)


# --------------------------------------------------------------------------
# Task Planning: "Plan for the Day" + Start/Pause/Resume/Stop (Ganesh,
# 2026-08-21, see docs/TASK_PLANNING_TIMER_PLAN.md for the fuller design
# this narrows down to). Always "today" — same live, right-now convention
# Auto time capture/Break/Punch already use (today.html only ever shows
# this section when day == today); a PlannedTask never carries a date
# other than the day it was actually worked. Every Start/Resume opens the
# single shared ActiveTaskTimer (see _stop_current_timer_if_any above);
# every Pause/Stop closes it into one ordinary TaskEntry via the existing
# _finish_task_timer — nothing here bypasses validate_entry's overlap/
# 4h-cap/locked-day checks, an auto-captured segment is held to exactly
# the same rules a typed row is.
# --------------------------------------------------------------------------
def _today_day_locked(db: Session, user: m.Employee) -> bool:
    today = today_local()
    sub = db.execute(
        select(m.DaySubmission).where(
            m.DaySubmission.employee_id == user.id, m.DaySubmission.date == today
        )
    ).scalar_one_or_none()
    return sub is not None and sub.locked


@router.post("/plan/add")
def add_plan(
    request: Request,
    project_id: int = Form(...),
    task_type_id: int = Form(...),
    details: str = Form(""),
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    if _today_day_locked(db, user):
        flash(request, "Day is already submitted — can't add a new plan.", "err")
        return RedirectResponse("/today", status_code=303)
    project = db.get(m.Project, project_id)
    task = db.get(m.TaskType, task_type_id)
    if project is None or not project.active or task is None or not task.active:
        flash(request, "Choose a Project and Task before adding a plan.", "err")
        return RedirectResponse("/today", status_code=303)
    cleaned = capitalize_first(details.strip())
    if not cleaned:
        flash(request, "Say what you plan to do.", "err")
        return RedirectResponse("/today", status_code=303)

    today = today_local()
    # Auto Punch In on the day's very first plan (Ganesh, 2026-08-22) —
    # "first plan of the day" is checked BEFORE the new row is added below,
    # so it's unambiguous. The "already punched in" check is the exact same
    # query punch_in() itself uses (same table, same one-open-session
    # invariant) — duplicated rather than calling punch_in() directly since
    # that route also flashes/redirects on its own, which would fight the
    # "plan added" flash below; this needs to stay a silent side effect.
    is_first_plan_today = db.execute(
        select(m.PlannedTask.id).where(
            m.PlannedTask.employee_id == user.id, m.PlannedTask.date == today
        )
    ).first() is None
    already_punched_in = db.execute(
        select(m.PunchSession.id).where(
            m.PunchSession.employee_id == user.id, m.PunchSession.date == today,
            m.PunchSession.punched_out_at.is_(None),
        )
    ).first() is not None

    db.add(m.PlannedTask(
        employee_id=user.id, date=today, project_id=project_id,
        task_type_id=task_type_id, details=cleaned, status=m.PLAN_PLANNED,
        created_by_employee_id=user.id,
    ))
    punched_in_now = False
    if is_first_plan_today and not already_punched_in:
        db.add(m.PunchSession(employee_id=user.id, date=today, punched_in_at=dt.datetime.utcnow()))
        punched_in_now = True
    db.commit()
    flash(request, "Added to today's plan." + (" Punched in for you." if punched_in_now else ""), "ok")
    return RedirectResponse("/today", status_code=303)


@router.post("/plan/{plan_id}/edit")
def edit_plan(
    plan_id: int,
    request: Request,
    details: str = Form(""),
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Scoped to just the plan text (Ganesh: "task can be editable") — not
    Project/Task, same precedent edit_entry_details() above already set
    for a logged TaskEntry's Details: changing Project/Task is a bigger
    change than what was asked for, and if the plan was picked wrong,
    Delete-and-re-add (while still `planned`) is the existing pattern for
    that, same as a mis-added row anywhere else in this app."""
    plan = db.get(m.PlannedTask, plan_id)
    if plan is None or plan.employee_id != user.id:
        return RedirectResponse("/today", status_code=303)
    if plan.status not in (m.PLAN_PLANNED, m.PLAN_PAUSED):
        flash(request, "Pause it first before editing — a running or finished plan can't be changed.", "err")
        return RedirectResponse("/today", status_code=303)
    cleaned = capitalize_first(details.strip())
    if not cleaned:
        flash(request, "Say what you plan to do.", "err")
        return RedirectResponse("/today", status_code=303)
    plan.details = cleaned
    db.commit()
    flash(request, "Plan updated.", "ok")
    return RedirectResponse("/today", status_code=303)


@router.post("/plan/{plan_id}/delete")
def delete_plan(
    plan_id: int,
    request: Request,
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    plan = db.get(m.PlannedTask, plan_id)
    if plan is None or plan.employee_id != user.id:
        return RedirectResponse("/today", status_code=303)
    if plan.status != m.PLAN_PLANNED:
        flash(request, "Only a not-yet-started plan can be removed.", "err")
        return RedirectResponse("/today", status_code=303)
    db.delete(plan)
    db.commit()
    return RedirectResponse("/today", status_code=303)


@router.post("/plan/{plan_id}/start")
def start_plan(
    plan_id: int,
    request: Request,
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Start (from `planned`) or Resume (from `paused`) — same action
    either way, a fresh segment. Whatever else is currently running (an
    ad-hoc Auto time capture timer, or a different plan) is auto-finished
    first via _stop_current_timer_if_any, same "starting a new one
    auto-stops the old one" convention Auto time capture already uses —
    the employee doesn't have to remember to Pause the other one first."""
    cfg = engine.get_config(db)
    plan = db.get(m.PlannedTask, plan_id)
    if plan is None or plan.employee_id != user.id:
        return RedirectResponse("/today", status_code=303)
    if _today_day_locked(db, user):
        flash(request, "Day is already submitted.", "err")
        return RedirectResponse("/today", status_code=303)
    if plan.status not in (m.PLAN_PLANNED, m.PLAN_PAUSED):
        return RedirectResponse("/today", status_code=303)
    project = db.get(m.Project, plan.project_id)
    task = db.get(m.TaskType, plan.task_type_id)
    if project is None or not project.active or task is None or not task.active:
        flash(request, "That plan's Project/Task is no longer active — edit it first.", "err")
        return RedirectResponse("/today", status_code=303)

    ok, error = _stop_current_timer_if_any(db, user, cfg)
    if not ok:
        flash(request, f"Couldn't save the timer already running: {error}", "err")
        return RedirectResponse("/today", status_code=303)

    now = now_local()
    db.add(m.ActiveTaskTimer(
        employee_id=user.id, date=today_local(), project_id=plan.project_id,
        task_type_id=plan.task_type_id, details=plan.details,
        start_minute=now.hour * 60 + now.minute, started_at=dt.datetime.utcnow(),
        planned_task_id=plan.id,
    ))
    plan.status = m.PLAN_RUNNING
    db.commit()
    flash(request, "Started — timer's running.", "ok")
    return RedirectResponse("/today", status_code=303)


def _finish_plan_segment(db: Session, user: m.Employee, plan: m.PlannedTask, cfg: dict):
    """Shared by pause_plan/stop_plan below: if this plan currently has the
    active timer, close that segment into a real TaskEntry via the same
    _finish_task_timer every ad-hoc Stop already uses. Returns (ok, error)
    — (True, None) with nothing to do if the plan has no running segment
    (e.g. Stop pressed on an already-paused plan, nothing to finalize)."""
    timer = db.execute(
        select(m.ActiveTaskTimer).where(
            m.ActiveTaskTimer.employee_id == user.id, m.ActiveTaskTimer.planned_task_id == plan.id
        )
    ).scalar_one_or_none()
    if timer is None:
        return True, None
    return _finish_task_timer(db, user, timer, cfg)


@router.post("/plan/{plan_id}/pause")
def pause_plan(
    plan_id: int,
    request: Request,
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    cfg = engine.get_config(db)
    plan = db.get(m.PlannedTask, plan_id)
    if plan is None or plan.employee_id != user.id:
        return RedirectResponse("/today", status_code=303)
    if plan.status != m.PLAN_RUNNING:
        return RedirectResponse("/today", status_code=303)
    ok, error = _finish_plan_segment(db, user, plan, cfg)
    if not ok:
        flash(request, error, "err")
        return RedirectResponse("/today", status_code=303)
    plan.status = m.PLAN_PAUSED
    db.commit()
    flash(request, "Paused — logged to your task log.", "ok")
    return RedirectResponse("/today", status_code=303)


@router.post("/plan/{plan_id}/stop")
def stop_plan(
    plan_id: int,
    request: Request,
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    cfg = engine.get_config(db)
    plan = db.get(m.PlannedTask, plan_id)
    if plan is None or plan.employee_id != user.id:
        return RedirectResponse("/today", status_code=303)
    if plan.status not in (m.PLAN_RUNNING, m.PLAN_PAUSED):
        return RedirectResponse("/today", status_code=303)
    ok, error = _finish_plan_segment(db, user, plan, cfg)
    if not ok:
        flash(request, error, "err")
        return RedirectResponse("/today", status_code=303)
    plan.status = m.PLAN_DONE
    db.commit()
    flash(request, "Marked done.", "ok")
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

    # Auto-carry unfinished plans to tomorrow (Ganesh, 2026-08-22) — a plan
    # still `planned` (never started) or `paused` at Submit Day time didn't
    # get finished today; rather than it just vanishing once Today's Plan
    # stops showing this date (that section only ever shows date ==
    # today_local(), see _day_context), a fresh copy shows up on tomorrow's
    # plan list automatically. This COPIES, it doesn't move — the original
    # PlannedTask row keeps its real date/status untouched as an honest
    # record of what didn't happen today, same never-rewrite-history
    # instinct as everything else in this app (DayStatus.source='imported'
    # etc.) — only PlannedTask.carried_at gets set, purely to stop a day
    # that's resubmitted after an admin unlock from copying the same plan
    # to tomorrow a second time. A plan still `running` at submit time
    # (forgotten to pause/stop) is left alone — not asked for, and Submit
    # Day shouldn't silently end a live timer out from under someone.
    carried = 0
    for plan in db.execute(
        select(m.PlannedTask).where(
            m.PlannedTask.employee_id == user.id, m.PlannedTask.date == day,
            m.PlannedTask.status.in_((m.PLAN_PLANNED, m.PLAN_PAUSED)),
            m.PlannedTask.carried_at.is_(None),
        )
    ).scalars():
        db.add(m.PlannedTask(
            employee_id=user.id, date=day + dt.timedelta(days=1),
            project_id=plan.project_id, task_type_id=plan.task_type_id,
            details=plan.details, status=m.PLAN_PLANNED,
            created_by_employee_id=user.id,
        ))
        plan.carried_at = dt.datetime.utcnow()
        carried += 1
    if carried:
        db.commit()

    flash(
        request,
        f"Day submitted and locked — total {total // 60}:{total % 60:02d}."
        + (f" {carried} unfinished plan{'s' if carried != 1 else ''} carried to tomorrow." if carried else ""),
        "ok",
    )
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
#
# Leave Management V2 (Ganesh, 2026-08-21, behind LEAVE_MANAGEMENT_V2_ENABLED
# — see docs/LEAVE_MANAGEMENT_PLAN.md): while the flag is off, my_leave()/
# request_leave() behave exactly as before (m.LEAVE_TYPES, engine.
# leave_balance()) — nothing below changes for anyone until it's flipped on
# after a real pytest + legacy.verify_strikes run confirms the accrual math.
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
    if LEAVE_MANAGEMENT_V2_ENABLED:
        cfg = engine.get_config(db)
        # Overtime-for-Missed-Hours match request UI moved to /overtime
        # (Ganesh, 2026-08-22 — see my_overtime() below): it's an overtime
        # decision, not a leave one, so it no longer lives on this page.
        return render(
            request, "leave.html",
            {
                "user": user, "records": records, "leave_types": m.LEAVE_TYPES_V2, "today": today,
                "balance_v2": engine.leave_balance_v2(db, user, today, cfg),
                "is_probation_active": engine.is_probation_active(user, today, cfg),
                "half_day_minutes": user.daily_target_minutes // 2,
                "full_day_minutes": user.daily_target_minutes,
            },
        )
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
    duration: str = Form(""),  # V2 only: "half" / "full" / "custom"
    hours: str = Form(""),
    note: str = Form(""),
    relation: str = Form(""),  # V2 only: Bereavement Time
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

    if not LEAVE_MANAGEMENT_V2_ENABLED:
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

    # ---- Leave Management V2 path -------------------------------------------
    if type not in m.LEAVE_TYPES_V2:
        flash(request, "Choose a valid leave type.", "err")
        return RedirectResponse("/leave", status_code=303)
    if type == m.LEAVE_SPECIAL_PAID:
        # Special Paid Time is granted by management, not requested (see
        # SpecialPaidGrant / docs/LEAVE_MANAGEMENT_PLAN.md §3) — not offered
        # as an option in the template's dropdown either, but block it here
        # too in case someone crafts the POST directly.
        flash(request, "Special Paid Time is granted by management, not requested.", "err")
        return RedirectResponse("/leave", status_code=303)

    duration = duration or m.LEAVE_DURATION_FULL
    if duration not in m.LEAVE_DURATIONS:
        flash(request, "Choose Half Day, Full Day, or Custom.", "err")
        return RedirectResponse("/leave", status_code=303)
    if duration == m.LEAVE_DURATION_FULL:
        minutes = None  # None => full day = the employee's own daily target that day
    elif duration == m.LEAVE_DURATION_HALF:
        minutes = user.daily_target_minutes // 2
    else:
        if not hours.strip():
            flash(request, "Enter the number of custom hours (or pick Half/Full Day).", "err")
            return RedirectResponse("/leave", status_code=303)
        try:
            minutes = int(round(float(hours) * 60))
        except ValueError:
            flash(request, "Hours must be a number.", "err")
            return RedirectResponse("/leave", status_code=303)
        if minutes <= 0:
            flash(request, "Custom hours must be greater than zero.", "err")
            return RedirectResponse("/leave", status_code=303)

    relation = relation.strip()
    if type == m.LEAVE_BEREAVEMENT:
        if relation not in m.BEREAVEMENT_RELATIONS:
            flash(request, "Choose who Bereavement Time is for.", "err")
            return RedirectResponse("/leave", status_code=303)
    else:
        relation = ""

    cfg = engine.get_config(db)
    today = today_local()

    # Requirement: block Planned Time during the waiting period — every
    # other type stays available (m.LEAVE_TYPES_NO_PROBATION_BLOCK).
    if type == m.LEAVE_PLANNED and engine.is_probation_active(user, today, cfg):
        flash(request, "Planned Time isn't available yet — you're still in your waiting period.", "err")
        return RedirectResponse("/leave", status_code=303)

    # Notice period, Planned Time only (docs/LEAVE_MANAGEMENT_PLAN.md,
    # decided 2026-08-20).
    if type == m.LEAVE_PLANNED:
        days_requested = (end - start).days + 1
        holidays = engine.holidays_set(db)
        if not engine.notice_period_satisfied(today, start, days_requested, user, holidays):
            required = engine.required_notice_working_days(days_requested)
            flash(
                request,
                f"Planned Time for {days_requested} day(s) needs at least {required} working day(s)' notice.",
                "err",
            )
            return RedirectResponse("/leave", status_code=303)

    # Requirement 10: no paid leave while on a PIP — every type becomes
    # Unpaid Time, decided at request time.
    effective_type = engine.effective_leave_type(user, type)
    pip_converted = effective_type != type

    lv = m.LeaveRecord(
        employee_id=user.id, start_date=start, end_date=end, type=effective_type,
        minutes_per_day=minutes, note=note.strip(), entered_by=user.name,
        relation=relation or None,
        status=m.LEAVE_REQUESTED,
    )
    db.add(lv)
    db.commit()
    audit(db, user.name, "leave_requested", "LeaveRecord", lv.id,
          {"range": f"{start}..{end}", "type": effective_type, "minutes": minutes,
           "pip_converted_from": type if pip_converted else None})
    if pip_converted:
        flash(request, "Recorded as Unpaid Time — no paid leave is available while on a Performance Improvement Plan.", "ok")
    else:
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
# Overtime-for-Missed-Hours match request (requirement 9, Leave Management
# V2, 2026-08-21) — extends the existing Compensation Links feature
# (app/routes/admin.py's add_complink, Person Detail page) rather than
# building a second one, per docs/LEAVE_MANAGEMENT_PLAN.md §3. Previously
# only an admin could create a link; this lets an employee propose one
# themselves for a SuperAdmin to approve/reject — same submit -> queue ->
# act shape as Leave/Overtime requests just above.
# --------------------------------------------------------------------------
@router.post("/leave/match-request")
def request_compensation_match(
    request: Request,
    shortfall_date: str = Form(...),
    surplus_dates: list = Form([]),
    note: str = Form(""),
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    if not LEAVE_MANAGEMENT_V2_ENABLED:
        flash(request, "Not available.", "err")
        return RedirectResponse("/overtime", status_code=303)
    try:
        shortfall = parse_date_field(shortfall_date, "Missed Hours day")
    except FormError as e:
        flash(request, e.message, "err")
        return RedirectResponse("/overtime", status_code=303)
    try:
        surplus = sorted({dt.date.fromisoformat(x.strip()).isoformat() for x in surplus_dates if x.strip()})
    except ValueError:
        flash(request, "Surplus/overtime dates must be valid dates.", "err")
        return RedirectResponse("/overtime", status_code=303)
    if not surplus:
        flash(request, "Pick at least one overtime day to match against.", "err")
        return RedirectResponse("/overtime", status_code=303)
    # a surplus day backs at most one shortfall — same invariant
    # add_complink() already enforces for admin-direct links.
    taken = engine.surplus_links_by_date(db, user.id)
    clash = [s for s in surplus if dt.date.fromisoformat(s) in taken]
    if clash:
        flash(request, f"Already matched to another day: {', '.join(clash)}", "err")
        return RedirectResponse("/overtime", status_code=303)
    link = m.CompensationLink(
        employee_id=user.id, shortfall_date=shortfall, surplus_dates=json.dumps(surplus),
        note=note.strip(), linked_by=user.name,
        status=m.LEAVE_REQUESTED, requested_by_employee=True,
    )
    db.add(link)
    db.commit()
    audit(db, user.name, "compensation_match_requested", "CompensationLink", link.id,
          {"shortfall": shortfall_date, "surplus": surplus})
    flash(request, "Match request submitted — an admin will review it.", "ok")
    return RedirectResponse("/overtime", status_code=303)


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
    ctx = {"user": user, "records": records}
    if LEAVE_MANAGEMENT_V2_ENABLED:
        # Overtime-for-Missed-Hours match picker (requirement 9) — moved
        # here from /leave (Ganesh, 2026-08-22: "this should be not in
        # leave management... it should be in overtime management", the
        # same call he made for the admin-side decision card). The
        # employee's own recent shortfall (Missed Hours) and surplus
        # (Overtime) days, same window CompensationLink already reasons
        # about, so they're picking from real days rather than typing
        # dates blind. 60 days back is a plain, generous window — no
        # requirement pinned an exact number, and it's cheap to widen
        # later if needed.
        today = today_local()
        window_start = today - dt.timedelta(days=60)
        recent_statuses = list(
            db.execute(
                select(m.DayStatus).where(
                    m.DayStatus.employee_id == user.id,
                    m.DayStatus.date.between(window_start, today),
                )
            ).scalars()
        )
        cfg = engine.get_config(db)
        comp_erases = cfg.get("comp_erases_strike") == "1"
        taken_surplus = engine.surplus_links_by_date(db, user.id)
        match_shortfalls = [
            r for r in recent_statuses
            if (r.variance_minutes or 0) < 0 and r.effective_status(comp_erases) in m.STRIKE_STATUSES
        ]
        match_surpluses = [
            r for r in recent_statuses
            if (r.variance_minutes or 0) > 0 and r.date not in taken_surplus
        ]
        match_links = list(
            db.execute(
                select(m.CompensationLink)
                .where(m.CompensationLink.employee_id == user.id, m.CompensationLink.requested_by_employee.is_(True))
                .order_by(m.CompensationLink.created_at.desc())
            ).scalars()
        )
        ctx["match_shortfalls"] = sorted(match_shortfalls, key=lambda r: r.date, reverse=True)
        ctx["match_surpluses"] = sorted(match_surpluses, key=lambda r: r.date, reverse=True)
        ctx["match_links"] = [
            (lk, [dt.date.fromisoformat(x) for x in json.loads(lk.surplus_dates or "[]")])
            for lk in match_links
        ]
    return render(request, "overtime.html", ctx)


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
    # Backdating block (Ganesh, 2026-08-21): this is a *pre-approval*
    # request per the module docstring above ("awaits lead/admin review —
    # doesn't block logging time or Punch In/Out either way"), so a
    # request for a date that's already in the past isn't really
    # "pre" anything anymore — it's asking for permission after the fact.
    # today_local() (not dt.date.today()), same as every other
    # what-day-is-it check in this app — see CLAUDE.md's BUSINESS_TZ hard
    # rule. Only blocks the employee's own self-service request; an admin
    # recording overtime on someone's behalf goes through
    # app/routes/admin.py, which isn't touched by this check.
    if start < today_local():
        flash(request, "Overtime requests can't be backdated — submit one for today or a future date.", "err")
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
    suggestions ending up on real logged time before review.

    Ganesh, 2026-08-21: the name is auto-normalized via
    normalize_title_case() before either the dedupe check or the save --
    'leads console' becomes 'Leads Console'. The dedupe check itself is
    case-insensitive (func.lower on both sides) so 'Leads Console' and
    someone later typing 'leads console' collide into the same pending
    row instead of creating two near-duplicate suggestions, even though
    Project.name/TaskType.name's own unique constraint is case-sensitive
    at the database level."""
    name = normalize_title_case(name)
    if not name:
        flash(request, "Enter a name before suggesting it.", "err")
        return RedirectResponse("/today", status_code=303)
    model = {"project": m.Project, "task": m.TaskType}.get(kind)
    if model is None:
        flash(request, "Unknown suggestion type.", "err")
        return RedirectResponse("/today", status_code=303)
    existing = db.execute(
        select(model).where(func.lower(model.name) == name.lower())
    ).scalar_one_or_none()
    if existing is not None:
        flash(request, f"'{existing.name}' already exists — pick it from the list instead of suggesting it again.", "err")
        return RedirectResponse("/today", status_code=303)
    db.add(model(name=name, active=True, status=m.LIST_PENDING, created_by_employee_id=user.id))
    db.commit()
    label = "Project" if kind == "project" else "Task"
    flash(request, f"{label} '{name}' suggested — a team lead will review it before it's usable.", "ok")
    return RedirectResponse("/today", status_code=303)


@router.post("/suggestions/edit-notice/{kind}/{item_id}/dismiss")
def dismiss_edit_notice(
    kind: str,
    item_id: int,
    request: Request,
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    """'Got it' on the "an admin rewrote your suggestion" banner (Ganesh,
    2026-08-21 — see _pending_edit_notices() and today.html). Ownership-
    checked (created_by_employee_id must be this employee) same as
    cancel_leave_request/cancel_overtime_request's own pattern, so one
    employee can't silently dismiss a notice meant for someone else via a
    guessed id. Silently no-ops (no error flash) if the row's already been
    dismissed or doesn't belong to them — this is a low-stakes UI
    preference, not something worth interrupting them over."""
    model = {"project": m.Project, "task": m.TaskType}.get(kind)
    item = db.get(model, item_id) if model else None
    if item is not None and item.created_by_employee_id == user.id:
        item.employee_notified_at = dt.datetime.utcnow()
        db.commit()
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


@router.post("/profile/reminder/dismiss")
def dismiss_profile_reminder(
    request: Request,
    return_to: str = Form("/today"),
    user: m.Employee = Depends(current_user),
):
    """'Remind me later' on the mandatory profile-completion popup (see
    app/templating.py's _needs_profile_reminder/render — Ganesh,
    2026-08-21). Only silences it for the rest of this login; logging out
    clears the session (app/routes/auth.py logout()), so it's back the
    next time this employee signs in, same as the 'once per login'
    requirement. No db write on purpose — this is a per-session UI
    preference, not data worth persisting or auditing."""
    request.session["profile_reminder_dismissed"] = True
    # return_to keeps them on whatever employee-zone page they were on
    # instead of always bouncing to Today — only ever a same-app relative
    # path posted from base.html's own modal, never user-typed input.
    if not return_to.startswith("/"):
        return_to = "/today"
    return RedirectResponse(return_to, status_code=303)


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
