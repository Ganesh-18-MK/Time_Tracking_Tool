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

from app import compensation, engine, llm_summary, models as m, reports
from app.auth import current_user
from app.db import get_db
from app.templating import HOLIDAY_MANAGEMENT_ENABLED, LEAVE_MANAGEMENT_V2_ENABLED, flash, render
from app.util import (
    BUSINESS_TZ,
    FormError,
    audit,
    capitalize_first,
    clamp_break_end,
    fmt_date,
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
    project_allowed_for_department,
    suggest_non_overlapping_start,
    task_allowed_for_project,
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


# Plan-ahead window (Ganesh, 2026-08-31 — "the employees should be able to
# plan their day the previous day itself"; first cut let any day up to a
# flat 30 days out be planned, then narrowed the same day to a week-scoped
# rule instead: "if today is monday so i can plan my days till friday...
# if today is friday then i can only able to add a plan for next monday
# only for monday, once that monday came then i can plan for that whole
# week days"). Not a Config row: this bounds a date-picker dropdown, not a
# business threshold admins tune per CLAUDE.md's "never hardcode
# thresholds" rule (that rule is about compliance math — strike counts,
# caps, accrual — this is neither). Only ever used to bound
# _allowed_dates()'s dropdown and add_plan()'s own "not too far out" check
# below; nothing engine.py/validation.py reads plans by date range, so
# this can't silently affect strikes/targets.
def _plan_ahead_max_date(today: dt.date, work_days: set) -> dt.date:
    """The furthest date `today` is allowed to plan for, per the rule
    above: through the last configured work day of THIS calendar week
    (Monday-Sunday) — but once `today` itself IS that last work day (or
    later, e.g. a weekend for a Mon-Fri employee), jump straight to the
    FIRST configured work day of NEXT week instead of the whole week, so
    there's always exactly one new day to plan for on your way out the
    door on a Friday, not a wide-open week you'd have to guess at. The
    moment that next Monday actually arrives (becomes `today`), this
    function is called fresh and returns THAT week's own last work day —
    the "whole week" opens up again automatically, no day-rollover
    bookkeeping needed anywhere. Falls back to Mon-Fri ({0,1,2,3,4}) if
    `work_days` is empty, same default Roster's own Add-person form uses."""
    days = work_days or {0, 1, 2, 3, 4}
    this_monday = today - dt.timedelta(days=today.weekday())
    last_day_this_week = this_monday + dt.timedelta(days=max(days))
    if today < last_day_this_week:
        return last_day_this_week
    next_monday = this_monday + dt.timedelta(days=7)
    return next_monday + dt.timedelta(days=min(days))


def _allowed_dates(db: Session, emp: m.Employee, cfg) -> list:
    today = today_local()
    earliest = earliest_allowed_date(
        emp, today, engine.cfg_int(cfg, "backdate_working_days"), engine.holidays_set(db)
    )
    latest = _plan_ahead_max_date(today, emp.work_day_set)
    days = []
    d = earliest
    while d <= latest:
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
        self.client = ""  # Case Type / Client (Ganesh, 2026-08-28) — a break is never case work

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
    # Auto time-capture timer (Ganesh, 2026-08-01) — single active timer per
    # employee, not per-date (see ActiveTaskTimer docstring), so this is
    # fetched by employee only; today.html only shows the widget when
    # `date == today`, same as how Start Break/Punch In are hardcoded to
    # "today" regardless of which day is currently being viewed.
    #
    # Max-row auto-split (Ganesh, 2026-08-28) — see
    # _auto_split_timer_if_over_cap's own docstring — runs right here,
    # BEFORE `entries` is queried below, specifically so a chunk it just
    # logged shows up in *this same* page load's task log/total/gap flags
    # instead of only appearing after a second refresh. This is the read
    # path (every GET /today, i.e. every page load/reload), so it's what
    # heals a timer that's already run past Config.max_row_minutes even if
    # the employee never clicks Stop — logs the completed cap-length
    # chunk(s) and advances the same timer's start_minute forward, so the
    # Auto time capture widget renders a freshly-reset elapsed time on this
    # very page load. Only for `date == today` — the widget itself is only
    # ever shown then (see today.html), and the helper is employee-scoped,
    # not date-scoped, so running it while browsing a past day would have
    # nothing to do with what's on screen.
    active_timer = db.execute(
        select(m.ActiveTaskTimer).where(m.ActiveTaskTimer.employee_id == emp.id)
    ).scalar_one_or_none()
    if active_timer is not None and date == today_local():
        active_timer = _auto_split_timer_if_over_cap(db, emp, active_timer, cfg)

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
    # Unlock requests (Ganesh, 2026-08-27) — only relevant to the lock
    # banner on a locked day, but cheap enough to just always fetch here
    # alongside `sub` rather than adding a second conditional query at
    # each of today_page()'s call sites.
    pending_unlock_request = db.execute(
        select(m.UnlockRequest).where(
            m.UnlockRequest.employee_id == emp.id, m.UnlockRequest.date == date,
            m.UnlockRequest.status == m.LEAVE_REQUESTED,
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
    #
    # Plan-ahead (Ganesh, 2026-08-31 — see PLAN_AHEAD_DAYS above) widened
    # this from `date == today` to `date >= today`: a future day's plans
    # now come back through this same `plans` list too, so the Today
    # template's add/edit/delete forms work on it exactly like today's own
    # plans do — Start/Pause/Resume/Stop stay gated to `day == today` in
    # the template itself (today.html), since a future day's row can only
    # ever be `planned` (nothing can start a live timer for a day that
    # hasn't arrived yet — see start_plan()'s own guard). Once that future
    # date actually becomes today, this same query naturally picks its rows
    # up as ordinary, fully-interactive today's-plan items — no day-
    # rollover bookkeeping needed anywhere.
    plans = []
    past_plans = []
    if date >= today_local():
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
        "pending_unlock_request": pending_unlock_request,
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


def _week_summary(db: Session, emp: m.Employee, cfg, today: dt.date) -> dict:
    """"This Week" card (Ganesh, 2026-08-29, from a pasted mockup) — fills
    the blank space below Plan for the Day / Today's Plan on the live
    "today" view. Monday of the current week through today (never a
    future day), computed LIVE from real TaskEntry/LeaveRecord/Holiday
    data rather than DayStatus — an unlocked "today" (and any
    not-yet-submitted earlier day this week) has no guaranteed DayStatus
    row, since recompute_employee() only runs on Submit Day / My Month
    view / admin actions, not on every /today load (see the 2026-08-28
    "auto-count logged hours" note above) — reading DayStatus here could
    show a stale or missing figure instead of what's actually logged.

    `days` is one badge per WORKING day strictly before today this week
    (Monday..yesterday, skipping weekends/holidays), marked done if that
    day has any logged minutes at all — mirrors the mockup's "Mon-Thu"
    row when today is a Friday. Today's own (still in-progress) day isn't
    included here since the Time log/progress bar above it already shows
    today's live total.

    Overtime this week sums each day's own positive variance (logged
    minus target) — a shortfall day never offsets a surplus day, same
    "positive variance only, per day" definition
    reports.task_log_overtime_report() already established for Overtime
    Management's "Who worked overtime" table."""
    week_start = today - dt.timedelta(days=today.weekday())  # Monday
    holidays = engine.holidays_set(db)
    leaves = list(
        db.execute(
            select(m.LeaveRecord).where(
                m.LeaveRecord.employee_id == emp.id,
                m.LeaveRecord.start_date <= today,
                m.LeaveRecord.end_date >= week_start,
            )
        ).scalars()
    )
    logged_by_date = dict(
        db.execute(
            select(m.TaskEntry.date, func.sum(m.TaskEntry.end_minute - m.TaskEntry.start_minute))
            .where(
                m.TaskEntry.employee_id == emp.id,
                m.TaskEntry.date >= week_start,
                m.TaskEntry.date <= today,
            )
            .group_by(m.TaskEntry.date)
        ).all()
    )
    total_logged = total_target = total_overtime = 0
    day_badges = []
    d = week_start
    while d <= today:
        logged = logged_by_date.get(d, 0) or 0
        working = engine.is_working_day(emp, d, holidays)
        target = max(0, emp.daily_target_minutes - engine.leave_minutes_on(leaves, emp, d)) if working else 0
        total_logged += logged
        total_target += target
        total_overtime += max(0, logged - target)
        if d < today and working:
            day_badges.append({"abbr": d.strftime("%a")[0], "done": logged > 0})
        d += dt.timedelta(days=1)
    return {
        "week_start": week_start,
        "logged": total_logged,
        "target": total_target,
        "overtime": total_overtime,
        "days": day_badges,
    }


def _week_day_circles(today: dt.date) -> list:
    """Mon-Sun circle row next to the Working date picker on Today (Ganesh,
    2026-08-30: "week days in circle... today should be other color
    highlighting and completed day should be greyed out"). Purely a
    date-position read against the REAL today (never the `day` being
    browsed via the date picker, per the ask's own wording — "for suppose
    today is Tue") — no DayStatus/TaskEntry lookup at all, so this never
    needs a DB call and always reflects the actual current week regardless
    of which day the employee happens to be viewing. Three states only:
    'past' (before today, this week), 'today', 'upcoming' (after today,
    still this week) — deliberately not tied to whether hours were logged
    that day (unlike _week_summary()'s day_badges), since the ask was
    about calendar position, not compliance."""
    monday = today - dt.timedelta(days=today.weekday())
    circles = []
    for i, label in enumerate(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]):
        d = monday + dt.timedelta(days=i)
        state = "past" if d < today else ("today" if d == today else "upcoming")
        circles.append({"label": label, "date": d, "state": state})
    return circles


def _month_summary(db: Session, emp: m.Employee, cfg, today: dt.date) -> dict:
    """"My Month" card (Ganesh, 2026-08-29, from a pasted mockup) — replaces
    the earlier "This Week" card in the same Today-page slot with the
    same month-level compliance snapshot My Month's own page already
    shows (Logged/Effective target/Running balance/Missing days/Partial
    days/Strikes/Compliance), not a new parallel figure.

    Unlike _week_summary() above (a pure live read that deliberately
    avoids DayStatus, since a week's not-yet-submitted days have no
    guaranteed fresh row), this one calls engine.recompute_employee()
    first — the exact same call my_month() itself makes before reading
    DayStatus — because these ARE the DayStatus/ledger/strike numbers My
    Month shows, not a different measurement; recomputing here is what
    keeps them accurate for "today" even though recompute_employee()
    doesn't otherwise run on every /today load (see the 2026-08-28
    "auto-count logged hours" note above). Bounded to the current
    calendar month through today (never future days), same
    min(last, today) convention my_month() uses.

    missing_days/partial_days both exclude strike_exempt rows, same
    filter engine.strikes_in() itself applies — so missing_days +
    partial_days always equals strikes, exactly like the mockup's
    "Strikes 3 (missing + partial)" implies."""
    year, month = today.year, today.month
    first, last = engine.month_range(year, month)
    effective_end = min(last, today)
    engine.recompute_employee(db, emp, first, effective_end, cfg)
    rows = list(
        db.execute(
            select(m.DayStatus).where(
                m.DayStatus.employee_id == emp.id, m.DayStatus.date.between(first, last)
            )
        ).scalars()
    )
    comp_erases = cfg.get("comp_erases_strike") == "1"
    logged = sum(r.actual_minutes for r in rows if r.actual_minutes is not None)
    effective_target = sum(r.target_minutes for r in rows if r.target_minutes is not None)
    ledger = engine.running_ledger(db, emp, first, effective_end)
    balance = ledger[-1]["balance"] if ledger else 0
    missing_days = sum(1 for r in rows if r.effective_status(comp_erases) == m.MISSING and not r.strike_exempt)
    partial_days = sum(1 for r in rows if r.effective_status(comp_erases) == m.PARTIAL and not r.strike_exempt)
    strikes = engine.strikes_in(rows, comp_erases)
    threshold = engine.cfg_int(cfg, "strike_threshold")

    # Mini working-days calendar (Ganesh, 2026-08-29: "bring that calendar
    # into my month section in today page, no need to display weekend
    # days... just display how many hours worked... +1hr / -2hrs") — a
    # compact variant of My Month's own full calendar, embedded in this
    # card instead. "Working days" reuses engine.is_working_day()'s own
    # definition (this employee's work_day_set minus the shared holiday
    # calendar) rather than a hardcoded Mon-Fri check, so an employee with
    # a non-Mon-Fri schedule (work_days is per-employee, see
    # Employee.work_day_set) still gets the right columns. The column set
    # itself (which weekdays appear) is fixed per employee and rendered
    # for every week — a holiday that lands on a normal work weekday still
    # gets its OWN cell (state "holiday", no number) rather than being
    # dropped, specifically so every week stays the same width and later
    # cells in that row never shift into the wrong weekday column.
    # Saturday/Sunday are always included too (Ganesh, 2026-08-29 follow-up:
    # "I want Saturday and sunday also") even for an employee whose
    # work_day_set doesn't include them — a weekend day still gets a
    # DayStatus row (status='weekend', target usually 0) via the same
    # recompute_employee() call above, so any voluntary weekend hours still
    # show up here as a normal variance the same way a scheduled day does.
    _WEEKDAY_ABBR = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    calendar_weekdays = sorted(set(emp.work_day_set) | {5, 6})
    holidays = engine.holidays_set(db)
    rows_by_date = {r.date: r for r in rows}
    calendar_weeks, week = [], []
    d = first
    while d <= last:
        if d.weekday() in calendar_weekdays:
            if d > effective_end:
                cell = {"day": d.day, "state": "future"}
            elif d in holidays:
                cell = {"day": d.day, "state": "holiday"}
            else:
                r = rows_by_date.get(d)
                if r is not None and r.status == m.LEAVE:
                    cell = {"day": d.day, "state": "leave"}
                elif r is not None and r.variance_minutes is not None:
                    cell = {"day": d.day, "state": "logged", "variance": r.variance_minutes}
                else:
                    cell = {"day": d.day, "state": "future"}
            week.append(cell)
        if d.weekday() == 6:
            if week:
                calendar_weeks.append(week)
            week = []
        d += dt.timedelta(days=1)
    if week:
        calendar_weeks.append(week)
    calendar_headers = [_WEEKDAY_ABBR[wd] for wd in calendar_weekdays]

    return {
        "logged": logged,
        "effective_target": effective_target,
        "balance": balance,
        "missing_days": missing_days,
        "partial_days": partial_days,
        "strikes": strikes,
        "threshold": threshold,
        "calendar_headers": calendar_headers,
        "calendar_weeks": calendar_weeks,
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
    # Department-scoped projects (Ganesh, 2026-08-28) — see
    # ProjectDepartment's docstring in app/models.py. A project absent
    # from project_depts has no links at all (unrestricted, visible to
    # every department); one WITH links is filtered down to just this
    # employee's own department. validate_entry() enforces the identical
    # rule server-side (validation.project_allowed_for_department()), so
    # this filter can't disagree with it — same "UI convenience, not the
    # only gate" relationship the suggestion-approval filter above has.
    project_depts = _project_department_links(db)
    emp_dept = user.department or "—"
    projects = [p for p in projects if p.id not in project_depts or emp_dept in project_depts[p.id]]
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


def _pending_plan_assignment_notices(db: Session, user: m.Employee) -> list:
    """TK-04 (Ganesh, 2026-08-28) — "Employee is notified when an entry is
    assigned to them." Plans an admin/team lead added to this employee's
    log (created_by_employee_id set to someone other than the employee —
    see PlannedTask's own docstring) that haven't been shown to them yet.
    Same one-card-per-still-unseen-item, dismiss-marks-just-that-one
    pattern _pending_edit_notices() above already established for the "an
    admin rewrote your suggestion" banner — reuses the identical shape
    (a plain list of ORM rows here rather than dicts, since today.html
    only needs a handful of fields straight off PlannedTask/its relations,
    unlike the suggestion notices which pull from two different models).
    Ordered oldest-assignment-first."""
    return list(
        db.execute(
            select(m.PlannedTask).where(
                m.PlannedTask.employee_id == user.id,
                m.PlannedTask.created_by_employee_id.isnot(None),
                m.PlannedTask.created_by_employee_id != user.id,
                m.PlannedTask.assigned_notified_at.is_(None),
            ).order_by(m.PlannedTask.created_at)
        ).scalars()
    )


def _project_department_links(db: Session) -> dict:
    """project_id -> sorted [department, ...] for every project that has
    at least one ProjectDepartment row (Ganesh, 2026-08-28 — see that
    model's docstring). A project_id absent from this dict has no links,
    which means unrestricted — visible to every department — same
    convention _task_project_links() below uses for tasks."""
    out: dict = {}
    for pid, dept in db.execute(select(m.ProjectDepartment.project_id, m.ProjectDepartment.department)).all():
        out.setdefault(pid, []).append(dept)
    return out


def _task_project_links(db: Session) -> dict:
    """task_type_id -> sorted [project_id, ...] for every task that has at
    least one ProjectTask row (Ganesh, 2026-08-27 — see that model's
    docstring). A task_type_id absent from this dict has NO links, which
    combo.js/validation.task_allowed_for_project() both treat the same
    way: unrestricted, pickable under every project."""
    out: dict = {}
    for pid, tid in db.execute(select(m.ProjectTask.project_id, m.ProjectTask.task_type_id)).all():
        out.setdefault(tid, []).append(pid)
    return out


def _combo_items(objs, assigned_ids: set, project_links: Optional[dict] = None) -> list:
    """ORM rows -> plain {id, name} dicts for the searchable-combo widget
    (see today.html/combo.js), with whatever this employee is assigned to
    (Ganesh, 2026-08-01: team-lead project/task assignment) sorted first
    and starred — advisory only, everything else stays just as pickable,
    only the ordering/label changes.

    project_links (Ganesh, 2026-08-27, task items only) adds a
    "project_ids" key combo.js reads to filter the Task combo down to
    whatever's valid for the currently-selected Project — see
    _task_project_links() above and ProjectTask's docstring in
    app/models.py. None/omitted means "every project" (no restriction),
    same convention task_allowed_for_project() uses server-side so the
    UI filter and the real enforcement can't disagree.

    Case Type / Client (Ganesh, 2026-08-28) — project items (only; a
    TaskType has no such column) also carry "is_case_type" straight off
    Project.is_case_type, so combo.js's initProjectTaskCombo can reveal
    the paired Client field the moment a case-type project is picked. See
    Project.is_case_type's docstring in app/models.py."""
    def _item(o):
        d = {"id": o.id, "name": o.name}
        if project_links is not None:
            d["project_ids"] = project_links.get(o.id)  # None if unrestricted
        if hasattr(o, "is_case_type"):
            d["is_case_type"] = bool(o.is_case_type)
        return d

    starred = [_item(o) for o in objs if o.id in assigned_ids]
    rest = [_item(o) for o in objs if o.id not in assigned_ids]
    # starring still prefixes the display name, applied after _item() so
    # project_ids travels with it either way
    for d in starred:
        d["name"] = f"★ {d['name']}"
    return starred + rest


@router.get("/today")
def today_page(
    request: Request,
    date: Optional[str] = None,
    reopen_project_id: Optional[str] = None,
    reopen_task_type_id: Optional[str] = None,
    reopen_details: Optional[str] = None,
    reopen_client: Optional[str] = None,
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
    task_project_links = _task_project_links(db)
    ctx = _day_context(db, user, day, cfg)
    last_end = max((e.end_minute for e in ctx["entries"]), default=None)

    # Suggestion-edit notices (Ganesh, 2026-08-21) — see
    # _pending_edit_notices() above. Shown on the "today" view only (same
    # "right now" convention as the other Today banners below) rather than
    # while browsing a past day via the date dropdown.
    edit_notices = _pending_edit_notices(db, user) if day == today else []

    # Plan-assignment notices (Ganesh, 2026-08-28, TK-04) — see
    # _pending_plan_assignment_notices() above. Same "today view only"
    # scoping as edit_notices right above, even though an assigned plan's
    # own date could be a different day — this is a notification about a
    # NEW assignment, surfaced on the main landing page, not tied to
    # whichever date happens to be selected in the dropdown.
    plan_assignment_notices = _pending_plan_assignment_notices(db, user) if day == today else []

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

    # "This Week" / "Reminders" cards (Ganesh, 2026-08-29, from a pasted
    # mockup — see _week_summary()'s own docstring) fill the blank space
    # below Plan for the Day / Today's Plan on the live "today" view only,
    # same "right now" convention as every other live widget on this page
    # — a past day's own read-only past_plans card already occupies that
    # spot instead. planned_remaining_days is None (hidden in the
    # template) when Leave V2 is off, since Planned Time is a V2-only
    # concept (see LEAVE_MANAGEMENT_V2_ENABLED). pending_ot_days counts
    # every day spanned by this employee's still-`requested` overtime
    # pre-approvals — 0 hides the Reminders card entirely rather than
    # showing an empty "nothing pending" line.
    week_summary = None
    month_summary = None
    planned_remaining_days = None
    pending_ot_days = 0
    if day == today:
        week_summary = _week_summary(db, user, cfg, today)
        month_summary = _month_summary(db, user, cfg, today)
        if LEAVE_MANAGEMENT_V2_ENABLED:
            planned_bal = engine.leave_balance_v2(db, user, today, cfg)[m.LEAVE_PLANNED]
            if planned_bal["remaining"] is not None and user.daily_target_minutes:
                planned_remaining_days = planned_bal["remaining"] // user.daily_target_minutes
        pending_ot_rows = list(
            db.execute(
                select(m.OvertimeApproval.start_date, m.OvertimeApproval.end_date).where(
                    m.OvertimeApproval.employee_id == user.id,
                    m.OvertimeApproval.status == m.OT_REQUESTED,
                )
            ).all()
        )
        pending_ot_days = sum((end - start).days + 1 for start, end in pending_ot_rows)

    ctx.update(
        {
            "week_summary": week_summary,
            "month_summary": month_summary,
            "planned_remaining_days": planned_remaining_days,
            "pending_ot_days": pending_ot_days,
            "over_allocation_minutes": over_allocation_minutes,
            "show_overtime_prompt": show_overtime_prompt,
            "show_punch_out_reminder": show_punch_out_reminder,
            "edit_notices": edit_notices,
            "plan_assignment_notices": plan_assignment_notices,
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
            "reopen_client": reopen_client,
            "reopen_end": reopen_end or (
                f"{gap_prefill_end // 60:02d}:{gap_prefill_end % 60:02d}" if gap_prefill_end is not None else None
            ),
            "user": user,
            "day": day,
            "today": today,
            "allowed_dates": _allowed_dates(db, user, cfg),
            "plan_ahead_max_date": _plan_ahead_max_date(today, user.work_day_set),
            "week_circles": _week_day_circles(today),
            # plain dicts, not ORM objects — the template feeds these straight
            # into the searchable-combo widget via |tojson. Assigned ones
            # (Ganesh, 2026-08-01) sort first and get a ★ — advisory only,
            # everything else stays just as pickable.
            "projects": _combo_items(projects, assigned_project_ids),
            "tasks": _combo_items(tasks, assigned_task_ids, task_project_links),
            "max_row_minutes": engine.cfg_int(cfg, "max_row_minutes"),
            "gap_minutes": engine.cfg_int(cfg, "gap_flag_minutes"),
        }
    )
    return render(request, "today.html", ctx)


def _client_required_error(project: Optional[m.Project], client: str) -> Optional[str]:
    """Case Type / Client (Ganesh, 2026-08-28) — the one server-side rule
    this feature needs: if the selected project is flagged is_case_type
    (see that column's docstring in app/models.py), Client can't be
    blank. Deliberately a plain route-level helper, not part of
    app/validation.py's validate_entry() — this is a simple presence
    check tied to which project was picked, not a PRD §4 entry rule like
    overlap/gap/cap/backdate, so keeping it here means this feature never
    touches validation.py and isn't gated by the pytest+verify_strikes
    hard rule. Called from every place a Client value can first be set:
    add_entry, start_task_timer, stop_task_timer (top-up, in case Start
    left it blank), and add_plan — NOT from _finish_task_timer, since by
    the time a segment finishes, Client was already required at whichever
    of those entry points started it."""
    if project is not None and project.is_case_type and not (client or "").strip():
        return f"'{project.name}' is a Case Type project — enter the Client."
    return None


@router.post("/entries")
def add_entry(
    request: Request,
    date: str = Form(...),
    project_id: int = Form(0),
    task_type_id: int = Form(0),
    details: str = Form(""),
    client: str = Form(""),
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
                "reopen_client": client,
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
    project = db.get(m.Project, project_id) if project_id else None
    client_err = _client_required_error(project, client)
    if client_err:
        flash(request, client_err, "err")
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
                client=client.strip(),
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


def _log_timer_as_entry(db: Session, user: m.Employee, timer: m.ActiveTaskTimer, end_minute: int, cfg: dict):
    """The actual "turn this timer's project/task/details/client into a
    validated TaskEntry row from timer.start_minute to end_minute" step,
    factored out of _finish_task_timer below (Ganesh, 2026-08-28, max-row
    auto-split — see _auto_split_timer_if_over_cap's docstring) so both
    that function's final segment AND the auto-split's earlier full-cap
    chunks go through validate_entry() identically. Never touches the
    timer row itself or commits — callers own both, since a caller may
    need to add several of these in a loop before doing either. Flushes
    (not commits) after add() so a same-request second call's own overlap
    check inside validate_entry sees this row (this session is
    autoflush=False — see app/db.py — so without an explicit flush a
    second chunk logged in the same request wouldn't see the first one and
    could wrongly validate as non-overlapping, or vice versa). Returns
    (True, None) or (False, message), same shape _finish_task_timer always
    returned.

    Passes closing_existing=True (bug fix, Ganesh, 2026-09-03) — an admin
    narrowing a project's departments (or unlinking a task from a project)
    while an employee already had a timer running against it used to
    permanently strand them: Stop/Pause, and the auto-close that happens
    before starting anything else, all funnel through this one function,
    so the newly-applied restriction blocked every way out. closing_existing
    tells validate_entry() this project/task pairing isn't a fresh pick —
    it was already running under whatever rule was in effect when it
    started — so it skips project_allowed_for_department()/
    task_allowed_for_project() for this one call only; every other check
    (locked day, overlap, 4h cap, backdate window) still fully applies."""
    client_err = _client_required_error(db.get(m.Project, timer.project_id), timer.client)
    if client_err:
        return False, client_err
    try:
        validate_entry(
            db, user, timer.date, timer.project_id, timer.task_type_id,
            timer.details, timer.start_minute, end_minute, cfg,
            closing_existing=True,
        )
    except EntryError as e:
        return False, "; ".join(e.errors)
    db.add(m.TaskEntry(
        employee_id=user.id, date=timer.date, project_id=timer.project_id,
        task_type_id=timer.task_type_id, details=capitalize_first((timer.details or "").strip()),
        client=(timer.client or "").strip(),
        start_minute=timer.start_minute, end_minute=end_minute,
        entry_method=m.ENTRY_METHOD_PLAN if timer.planned_task_id else m.ENTRY_METHOD_AUTO_TIMER,
    ))
    db.flush()
    return True, None


def _auto_split_timer_if_over_cap(db: Session, user: m.Employee, timer: Optional[m.ActiveTaskTimer], cfg: dict) -> Optional[m.ActiveTaskTimer]:
    """Max single-row duration auto-split (Ganesh, 2026-08-28) — before
    this, a timer left running past Config.max_row_minutes (the same §4
    4h cap validate_entry enforces on every manually-typed row, see
    validate_entry's own "times" section) just got stuck: Stop & Log
    failed outright with "Single row longer than 4h 0m — break the work
    down" (validate_entry's own message), and Pause/Stop on a plan-linked
    timer failed the exact same way since both go through
    _finish_task_timer below. The employee had no way to resolve it short
    of editing the day by hand — reported live via screenshot, a timer
    that had been left running 6h+.

    This app has no background scheduler (see the missing-legacy-data /
    no-pytest sandbox notes — everything here is plain request/response),
    so "automatically" means lazily, on next touch — same convention
    stop_task_timer/cancel_task_timer already use to keep PlannedTask.status
    in sync as "defense-in-depth" (Ganesh, 2026-08-22 bugfix). Called from
    two places: _day_context (every GET /today, so simply reloading the
    page — including the client-side auto-reload today.html's timer script
    now does once its live clock crosses this same cap — heals a stuck
    timer with no click needed) and the top of _finish_task_timer (so Stop
    & Log / Pause / Stop-plan / starting-a-new-timer's own auto-finish via
    _stop_current_timer_if_any never hit the "longer than 4h" error in the
    first place — the split runs first, so by the time _finish_task_timer
    computes its own final segment against "now", only the within-cap
    remainder is left).

    For each full cap-length chunk the timer has been running past its own
    start_minute, logs it as a real TaskEntry (start, start+cap) via
    _log_timer_as_entry — same validated path, same Details/Client copied
    as-is a normal Stop & Log already uses — then advances this SAME
    ActiveTaskTimer's start_minute/started_at forward by one cap-length
    instead of deleting it, so the timer keeps running uninterrupted under
    its existing id: the live JS clock re-reads started_at fresh on next
    page load and naturally reads back near 0:00 — that's the "timer
    restarts from 00:00 for the same task" the employee asked for, not a
    separate stop-then-start action. Loops (bounded by how many
    cap-lengths could possibly fit in a day, so a misconfigured tiny cap
    can't spin) in case a page was left open across more than one full
    cap-length. Stops early, leaving the remainder running rather than
    logging a row past midnight, if a chunk's boundary would reach 1440 —
    the existing "no rows span midnight" rule already covers what happens
    next, same as any other overnight-left-running timer today. Also stops
    early (silently, leaving the timer exactly as it was) if a chunk fails
    validate_entry for an unrelated reason (day got locked mid-run, project
    deactivated) — same "don't lose time, leave it for a human to sort
    out" instinct _finish_task_timer's own failure path already has.

    Deliberately does NOT run from cancel_task_timer — Cancel means "this
    timer was a mistake, discard the whole thing," and retroactively
    logging cap-length chunks the employee is actively trying to throw
    away would contradict that.

    Cross-midnight rollover (Ganesh, 2026-08-28, same-day follow-up —
    explicitly asked for over the alternative of just discarding a stuck
    timer via Cancel) — a timer whose `date` no longer matches today
    crossed midnight while still running (reported live: Today stuck
    "loading" forever, because the reload-on-cap-crossing script in
    today.html had no way to know the server couldn't fix a timer like
    this, and kept retrying every time the reloaded page still showed it
    over cap — see that bugfix's own CLAUDE.md entry). Before touching
    "now," this closes out the remainder of the ORIGINAL day first — same
    cap-sized chunks as below, just capped at day-end (1440) instead of
    stopping there — then rolls the SAME timer forward to start fresh at
    00:00 BUSINESS_TZ the next calendar day (`started_at` recomputed via
    BUSINESS_TZ midnight -> UTC, not naive timedelta arithmetic, so a
    DST boundary can't shift it) and repeats, bounded to 7 calendar days
    so a timer stuck for a very long time doesn't loop indefinitely (a
    timer that old is a sign something else needs a human, not something
    to keep auto-processing). Once `timer.date == today`, falls through
    to the ordinary same-day loop below unchanged. Same "leave it alone
    and stop" fallback as everywhere else here if a chunk fails
    validate_entry (e.g. that old day is now locked)."""
    if timer is None:
        return timer
    cap = engine.cfg_int(cfg, "max_row_minutes")
    if cap <= 0:
        return timer
    today = today_local()
    changed = False

    guard_days = 0
    while timer.date != today and guard_days < 7:
        while timer.start_minute < 1440:
            boundary = min(timer.start_minute + cap, 1440)
            ok, _error = _log_timer_as_entry(db, user, timer, boundary, cfg)
            if not ok:
                if changed:
                    db.commit()
                return timer
            timer.start_minute = boundary
            changed = True
        next_day = timer.date + dt.timedelta(days=1)
        local_midnight = dt.datetime.combine(next_day, dt.time(0, 0), tzinfo=BUSINESS_TZ)
        timer.date = next_day
        timer.start_minute = 0
        timer.started_at = local_midnight.astimezone(dt.timezone.utc).replace(tzinfo=None)
        guard_days += 1

    now_minute = now_local().hour * 60 + now_local().minute
    for _ in range((1440 // cap) + 2):
        if now_minute - timer.start_minute <= cap:
            break
        boundary = timer.start_minute + cap
        if boundary >= 1440:
            break
        ok, _error = _log_timer_as_entry(db, user, timer, boundary, cfg)
        if not ok:
            break
        timer.start_minute = boundary
        timer.started_at = timer.started_at + dt.timedelta(minutes=cap)
        changed = True
    if changed:
        db.commit()
    return timer


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
    ENTRY_METHOD_AUTO_TIMER.

    Max-row auto-split (Ganesh, 2026-08-28) runs first here — see
    _auto_split_timer_if_over_cap's docstring — so a timer that's been
    running past Config.max_row_minutes gets its earlier full-cap chunks
    logged off before this function computes its own final segment against
    "now"; without this, Stop (and Pause/Stop-plan, which both funnel
    through this same function) on a stuck long-running timer failed
    outright with validate_entry's "longer than 4h — break the work down"
    error instead of logging anything."""
    timer = _auto_split_timer_if_over_cap(db, user, timer, cfg)
    now = now_local()
    end_minute = clamp_break_end(timer.start_minute, now.hour * 60 + now.minute)
    ok, error = _log_timer_as_entry(db, user, timer, end_minute, cfg)
    if not ok:
        return False, error
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
    client: str = Form(""),
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
    # Case Type / Client (Ganesh, 2026-08-28) — required immediately here,
    # unlike Details on this same form (which stays "optional now, needed
    # by Stop"): which client the work is for is normally known before
    # you start the clock, same reasoning add_entry/add_plan already
    # apply to their own Client field, so all 3 entry points are
    # consistent about when this is asked for.
    client_err = _client_required_error(project, client)
    if client_err:
        flash(request, client_err, "err")
        return RedirectResponse("/today", status_code=303)

    now = now_local()
    db.add(m.ActiveTaskTimer(
        employee_id=user.id, date=today, project_id=project_id, task_type_id=task_type_id,
        details=details.strip(), client=client.strip(), start_minute=now.hour * 60 + now.minute,
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


def _parse_estimated_minutes(raw: str) -> "tuple[Optional[int], Optional[str]]":
    """Plan-ahead / estimated time (Ganesh, 2026-08-31). Returns
    (minutes_or_None, error_or_None). Blank input is valid and means "no
    estimate" (stored as NULL, see PlannedTask.estimated_minutes's own
    docstring) — this field was never made required. Bounded to a single
    calendar day (1440 minutes) for the same reason `max_row_minutes`
    bounds a single logged row: an estimate bigger than a whole day is
    almost certainly a typo (e.g. hours typed where minutes were
    expected), not a real plan."""
    raw = (raw or "").strip()
    if not raw:
        return None, None
    try:
        minutes = int(raw)
    except ValueError:
        return None, "Estimated time must be a whole number of minutes."
    if minutes < 0:
        return None, "Estimated time can't be negative."
    if minutes > 1440:
        return None, "Estimated time can't be more than a day (1440 minutes)."
    return minutes, None


@router.post("/plan/add")
def add_plan(
    request: Request,
    project_id: int = Form(...),
    task_type_id: int = Form(...),
    details: str = Form(""),
    client: str = Form(""),
    date: str = Form(""),
    estimated_minutes: str = Form(""),
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    today = today_local()
    # Plan-ahead (Ganesh, 2026-08-31): a plan can now be added for today OR
    # any day through _plan_ahead_max_date()'s week-scoped boundary (see
    # that function's own docstring above _allowed_dates) — never a past
    # day, planning something that already happened doesn't mean anything.
    # Blank/unparseable `date` falls back to today, same as today_page()'s
    # own `date` query param handling, so this stays backward-compatible
    # with anything that still posts here without a date field.
    try:
        plan_date = dt.date.fromisoformat(date) if date else today
    except ValueError:
        plan_date = today
    max_plan_date = _plan_ahead_max_date(today, user.work_day_set)
    if plan_date < today:
        flash(request, "Can't plan a day that's already passed.", "err")
        return RedirectResponse("/today", status_code=303)
    if plan_date > max_plan_date:
        flash(request, f"Can't plan past {fmt_date(max_plan_date)} yet — more days open up once your next work week starts.", "err")
        return RedirectResponse("/today", status_code=303)
    redirect_url = "/today" if plan_date == today else f"/today?date={plan_date.isoformat()}"

    # A locked day can only ever be *today* (a future day has no
    # DaySubmission row yet — nothing to lock), so this check stays scoped
    # to that case rather than generalizing _today_day_locked() for a date
    # that can never actually be locked.
    if plan_date == today and _today_day_locked(db, user):
        flash(request, "Day is already submitted — can't add a new plan.", "err")
        return RedirectResponse(redirect_url, status_code=303)
    project = db.get(m.Project, project_id)
    task = db.get(m.TaskType, task_type_id)
    if project is None or not project.active or task is None or not task.active:
        flash(request, "Choose a Project and Task before adding a plan.", "err")
        return RedirectResponse(redirect_url, status_code=303)
    # Project-scoped tasks (Ganesh, 2026-08-27) — same
    # validation.task_allowed_for_project() validate_entry() uses, checked
    # here too for immediate feedback rather than only discovering the
    # mismatch once this plan is Started/finished into a real TaskEntry.
    if not task_allowed_for_project(db, project_id, task_type_id):
        flash(request, f"'{task.name}' isn't set up for '{project.name}' — choose a different task.", "err")
        return RedirectResponse(redirect_url, status_code=303)
    # Department-scoped projects (Ganesh, 2026-08-28) — same
    # validation.project_allowed_for_department() validate_entry() uses,
    # checked here too for immediate feedback (mirrors the
    # task_allowed_for_project check right above).
    if not project_allowed_for_department(db, project_id, user.department):
        flash(request, f"'{project.name}' isn't available to your department.", "err")
        return RedirectResponse(redirect_url, status_code=303)
    client_err = _client_required_error(project, client)
    if client_err:
        flash(request, client_err, "err")
        return RedirectResponse(redirect_url, status_code=303)
    cleaned = capitalize_first(details.strip())
    if not cleaned:
        flash(request, "Say what you plan to do.", "err")
        return RedirectResponse(redirect_url, status_code=303)
    est_minutes, est_err = _parse_estimated_minutes(estimated_minutes)
    if est_err:
        flash(request, est_err, "err")
        return RedirectResponse(redirect_url, status_code=303)

    # Auto Punch In on the day's very first plan (Ganesh, 2026-08-22) —
    # "first plan of the day" is checked BEFORE the new row is added below,
    # so it's unambiguous. The "already punched in" check is the exact same
    # query punch_in() itself uses (same table, same one-open-session
    # invariant) — duplicated rather than calling punch_in() directly since
    # that route also flashes/redirects on its own, which would fight the
    # "plan added" flash below; this needs to stay a silent side effect.
    # Scoped to plan_date == today (Ganesh, 2026-08-31, plan-ahead) —
    # punching in for a day that hasn't happened yet makes no sense, and
    # planning tomorrow shouldn't silently punch you in for today either.
    punched_in_now = False
    if plan_date == today:
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
        if is_first_plan_today and not already_punched_in:
            db.add(m.PunchSession(employee_id=user.id, date=today, punched_in_at=dt.datetime.utcnow()))
            punched_in_now = True

    db.add(m.PlannedTask(
        employee_id=user.id, date=plan_date, project_id=project_id,
        task_type_id=task_type_id, details=cleaned, client=client.strip(), status=m.PLAN_PLANNED,
        created_by_employee_id=user.id, estimated_minutes=est_minutes,
    ))
    db.commit()
    msg = "Added to today's plan." if plan_date == today else f"Added to the plan for {fmt_date(plan_date)}."
    flash(request, msg + (" Punched in for you." if punched_in_now else ""), "ok")
    return RedirectResponse(redirect_url, status_code=303)


@router.post("/plan/{plan_id}/edit")
def edit_plan(
    plan_id: int,
    request: Request,
    details: str = Form(""),
    estimated_minutes: str = Form(""),
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Scoped to just the plan text (Ganesh: "task can be editable") — not
    Project/Task, same precedent edit_entry_details() above already set
    for a logged TaskEntry's Details: changing Project/Task is a bigger
    change than what was asked for, and if the plan was picked wrong,
    Delete-and-re-add (while still `planned`) is the existing pattern for
    that, same as a mis-added row anywhere else in this app. Estimated
    time (Ganesh, 2026-08-31) was added to this same form rather than
    treated as another "delete and re-add" field — unlike Project/Task, a
    wrong estimate has no downstream effect on anything (it isn't read by
    engine.py/validation.py at all), so there's no reason to withhold
    editing it here."""
    plan = db.get(m.PlannedTask, plan_id)
    if plan is None or plan.employee_id != user.id:
        return RedirectResponse("/today", status_code=303)
    redirect_url = "/today" if plan.date == today_local() else f"/today?date={plan.date.isoformat()}"
    if plan.status not in (m.PLAN_PLANNED, m.PLAN_PAUSED):
        flash(request, "Pause it first before editing — a running or finished plan can't be changed.", "err")
        return RedirectResponse(redirect_url, status_code=303)
    cleaned = capitalize_first(details.strip())
    if not cleaned:
        flash(request, "Say what you plan to do.", "err")
        return RedirectResponse(redirect_url, status_code=303)
    est_minutes, est_err = _parse_estimated_minutes(estimated_minutes)
    if est_err:
        flash(request, est_err, "err")
        return RedirectResponse(redirect_url, status_code=303)
    plan.details = cleaned
    plan.estimated_minutes = est_minutes
    db.commit()
    flash(request, "Plan updated.", "ok")
    return RedirectResponse(redirect_url, status_code=303)


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
    redirect_url = "/today" if plan.date == today_local() else f"/today?date={plan.date.isoformat()}"
    # TK-04 (Ganesh, 2026-08-28) — "Employee cannot delete an assigned
    # entry." created_by_employee_id != user.id (and not None, though that
    # can't happen for a row that passed the ownership check above) means
    # an admin/team lead created this one, not the employee themself — see
    # PlannedTask's own docstring. Only the admin-side
    # admin_delete_plan() (app/routes/admin.py) can remove it while
    # still Planned; edit_plan() (the plan-text-only edit) is intentionally
    # NOT restricted the same way — TK-04 only calls out delete.
    if plan.created_by_employee_id is not None and plan.created_by_employee_id != user.id:
        flash(request, "This was assigned by an admin — ask them to remove it.", "err")
        return RedirectResponse(redirect_url, status_code=303)
    if plan.status != m.PLAN_PLANNED:
        flash(request, "Only a not-yet-started plan can be removed.", "err")
        return RedirectResponse(redirect_url, status_code=303)
    db.delete(plan)
    db.commit()
    return RedirectResponse(redirect_url, status_code=303)


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
    # Plan-ahead (Ganesh, 2026-08-31): a plan dated after today can exist
    # now (see add_plan()), but there's nothing to Start yet — the live
    # timer this opens always runs against `now_local()`, which can't be
    # inside a day that hasn't started. today.html already hides the
    # Start/Resume button for a future-day item; this is the server-side
    # backstop, same "hidden in the UI, still blocked in the route"
    # precedent every other button-guard in this file already follows.
    if plan.date != today_local():
        flash(request, "Can't start a plan before its day arrives.", "err")
        return RedirectResponse(f"/today?date={plan.date.isoformat()}", status_code=303)
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
        task_type_id=plan.task_type_id, details=plan.details, client=plan.client,
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


def _generate_day_summary(db: Session, sub: m.DaySubmission, user: m.Employee, day: dt.date) -> None:
    """AI day summary (Ganesh, 2026-08-31) — called once from submit_day()
    below, right after that day's DaySubmission/DayStatus are already
    committed. Deliberately synchronous and in-request: this app has no
    background job queue (see the "no background scheduler" note
    elsewhere in this codebase), so there's no other point at which this
    could run, and llm_summary.summarize_day() has its own short timeout
    specifically so this can't turn a Submit Day click into a long hang.

    Never lets an LLM-backend failure affect the employee's own submission —
    everything past the `total == 0`/`already locked` guards in submit_day()
    has already happened by the time this runs, so the worst case here is
    a missing/errored summary on the admin side, never a failed Submit Day.
    `sub` is re-used (not re-queried) since submit_day() already holds the
    exact row that needs updating.

    Only the fields the prompt actually needs (project/task names, minutes,
    details) are pulled out of each TaskEntry into a plain dict before
    calling summarize_day() — keeps llm_summary.py free of any ORM/DB
    coupling, so it can be tested (or swapped for a different provider)
    without a database at all."""
    rows = list(
        db.execute(
            select(m.TaskEntry)
            .where(m.TaskEntry.employee_id == user.id, m.TaskEntry.date == day)
            .order_by(m.TaskEntry.start_minute)
        ).scalars()
    )
    entries = [
        {
            "project": r.project.name if r.project else "—",
            "task": r.task_type.name if r.task_type else "—",
            "duration_minutes": r.duration_minutes,
            "details": r.details,
        }
        for r in rows
    ]
    text, error = llm_summary.summarize_day(entries)
    sub.summary_text = text
    sub.summary_error = error
    sub.summary_generated_at = dt.datetime.utcnow()
    db.commit()


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
    _generate_day_summary(db, sub, user, day)

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


@router.post("/unlock-request")
def request_unlock(
    request: Request,
    date: str = Form(...),
    note: str = Form(""),
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    """"Ask an admin to unlock" button on a locked day's lock banner
    (Ganesh, 2026-08-27 — before this, the only way to ask was outside
    the app entirely). Note is optional (AskUserQuestion, 2026-08-27:
    lowest-friction, same call as the existing Overtime<->Missed-Hours
    match request's own optional note) — a blank note still creates a
    perfectly actionable request, since the admin reviewing it can already
    see exactly what's in the task log for that day."""
    try:
        day = parse_date_field(date)
    except FormError as e:
        flash(request, e.message, "err")
        return RedirectResponse("/today", status_code=303)
    sub = db.execute(
        select(m.DaySubmission).where(
            m.DaySubmission.employee_id == user.id, m.DaySubmission.date == day
        )
    ).scalar_one_or_none()
    if sub is None or not sub.locked:
        flash(request, "That day isn't locked — nothing to request.", "err")
        return RedirectResponse(f"/today?date={day.isoformat()}", status_code=303)
    already = db.execute(
        select(m.UnlockRequest).where(
            m.UnlockRequest.employee_id == user.id, m.UnlockRequest.date == day,
            m.UnlockRequest.status == m.LEAVE_REQUESTED,
        )
    ).scalar_one_or_none()
    if already is not None:
        flash(request, "You already have a pending unlock request for that day.", "err")
        return RedirectResponse(f"/today?date={day.isoformat()}", status_code=303)
    db.add(m.UnlockRequest(employee_id=user.id, date=day, note=note.strip()))
    db.commit()
    audit(db, user.name, "unlock_requested", "UnlockRequest", f"{user.id}:{day.isoformat()}", {"note": note.strip()})
    flash(request, "Unlock request sent — an admin will review it.", "ok")
    return RedirectResponse(f"/today?date={day.isoformat()}", status_code=303)


def _ledger_display_status(r: m.DayStatus, comp_erases: bool) -> str:
    """Display-only label for My Month's "Hours ledger" table (Ganesh,
    2026-08-29) — layers "Overtime" on top of a genuinely Complete day when
    that day's variance_minutes is positive (logged more than target).
    Missing/Partial/Leave/Holiday/Weekend are returned exactly as
    effective_status() already gives them — this never changes
    DayStatus.status/effective_status(), strikes, or anything compliance-
    facing; it's purely which word this one table shows next to a Complete
    day. A Leave day keeps reading "Leave" even if variance happens to be
    positive (e.g. a reduced/zeroed target from a partial-day leave) —
    Leave takes precedence since that's the more meaningful fact about the
    day."""
    eff = r.effective_status(comp_erases)
    if eff == m.COMPLETE and r.variance_minutes is not None and r.variance_minutes > 0:
        return "overtime"
    return eff


def _weekly_ledger(ledger: List[dict], comp_erases: bool) -> List[dict]:
    """Groups the chronological (ascending) per-day rows from
    engine.running_ledger() into Monday-Sunday weeks for My Month's
    collapsible Hours ledger table (Ganesh, 2026-08-29: "make it week
    wise... click on that week then that particular week days will
    expand"). Purely a display grouping over already-computed rows: a
    week's Logged/Target/Variance are plain sums of its days, but a week's
    "Balance" is the LAST day's already-cumulative running balance within
    that week, not a sum — summing running balances would double-count
    every prior day's balance into every later week. Returns weeks and,
    within each week, days in DESCENDING (most-recent-first) order, so the
    template can render top-to-bottom with no further reversal — matching
    the old `ledger|reverse` convention this replaces."""
    weeks: List[dict] = []
    current = None
    for item in ledger:
        r = item["row"]
        week_start = r.date - dt.timedelta(days=r.date.weekday())
        week_end = week_start + dt.timedelta(days=6)
        if current is None or current["start"] != week_start:
            current = {
                "start": week_start,
                "end": week_end,
                "logged": 0,
                "target": 0,
                "variance": 0,
                "balance": item["balance"],
                "days": [],
            }
            weeks.append(current)
        current["logged"] += r.actual_minutes or 0
        current["target"] += r.target_minutes or 0
        current["variance"] += r.variance_minutes or 0
        current["balance"] = item["balance"]
        current["days"].append(
            {
                "row": r,
                "balance": item["balance"],
                "display_status": _ledger_display_status(r, comp_erases),
            }
        )
    for week in weeks:
        week["days"].reverse()
    weeks.reverse()
    return weeks


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
    weekly_ledger = _weekly_ledger(ledger, comp_erases)
    comp = compensation.monthly_summary(db, user, year, month)
    # Simple "where did my hours go this month" bar chart (Ganesh,
    # 2026-09-03) — sits between the KPI tiles and the Hours ledger, see
    # reports.my_month_project_totals()'s own docstring for why this is a
    # flat per-project total rather than a day-by-day breakdown.
    project_totals = reports.my_month_project_totals(db, user.id, first, last)
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
            "strikes": strikes,
            "threshold": threshold,
            "comp_erases": comp_erases,
            "leave_totals": leave_totals,
            "ledger": ledger,
            "weekly_ledger": weekly_ledger,
            "balance": balance,
            "entries_by_date": entries_by_date,
            "comp": comp,
            "project_totals": project_totals,
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
    # Partial allocation (Ganesh, 2026-08-25) — same shared
    # engine.allocate_surplus_minutes() add_complink() uses, computed and
    # stored right now (at request time) rather than deferred to approval,
    # so a ticked surplus day is claimed the moment it's requested — same
    # "immediate claim" timing the old whole-day version already had (it
    # never filtered by link status either), just minute-accurate now
    # instead of blocking the whole day.
    allocation = engine.allocate_surplus_minutes(db, user.id, shortfall, surplus)
    if not allocation:
        flash(request, "None of the selected day(s) have any overtime hours left to match — pick a different day.", "err")
        return RedirectResponse("/overtime", status_code=303)
    link = m.CompensationLink(
        employee_id=user.id, shortfall_date=shortfall,
        surplus_dates=json.dumps(sorted(allocation.keys())),
        surplus_minutes=json.dumps(allocation),
        note=note.strip(), linked_by=user.name,
        status=m.LEAVE_REQUESTED, requested_by_employee=True,
    )
    db.add(link)
    db.commit()
    audit(db, user.name, "compensation_match_requested", "CompensationLink", link.id,
          {"shortfall": shortfall_date, "allocation": allocation})
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
    ym: Optional[str] = None,
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
        # employee's own shortfall (Missed Hours) and surplus (Overtime)
        # days, same idea CompensationLink already reasons about, so
        # they're picking from real days rather than typing dates blind.
        # Switched from a fixed 60-day rolling window to one-calendar-
        # month-at-a-time with prev/next navigation (Ganesh, 2026-08-25 —
        # "why its showing previous month" against the old rolling window;
        # this now matches the admin-side pickers on Person Detail and
        # Overtime Management, which were always month-scoped) — see
        # app/util.py's parse_ym()/prev_next_month() for the same pattern
        # used elsewhere (my_month(), overtime_page()).
        from app.util import parse_ym, prev_next_month

        today = today_local()
        year, month = parse_ym(ym, default=today)
        first, last = engine.month_range(year, month)
        recent_statuses = list(
            db.execute(
                select(m.DayStatus).where(
                    m.DayStatus.employee_id == user.id,
                    m.DayStatus.date.between(first, min(last, today)),
                )
            ).scalars()
        )
        cfg = engine.get_config(db)
        comp_erases = cfg.get("comp_erases_strike") == "1"
        # Partial allocation (Ganesh, 2026-08-25) — a shortfall/surplus day
        # is no longer hidden the instant it appears in any link; the
        # template shows its remaining balance instead, same treatment the
        # admin-side pickers got (app/templates/admin/person.html,
        # admin/overtime.html). See engine.shortfall_allocated_minutes_by_
        # date()/surplus_minutes_used_by_date().
        match_shortfall_allocated_by_date = engine.shortfall_allocated_minutes_by_date(db, user.id)
        match_surplus_used_by_date = engine.surplus_minutes_used_by_date(db, user.id)
        match_shortfalls = [
            r for r in recent_statuses
            if (r.variance_minutes or 0) < 0 and r.effective_status(comp_erases) in m.STRIKE_STATUSES
        ]
        match_surpluses = [r for r in recent_statuses if (r.variance_minutes or 0) > 0]
        match_links = list(
            db.execute(
                select(m.CompensationLink)
                .where(m.CompensationLink.employee_id == user.id, m.CompensationLink.requested_by_employee.is_(True))
                .order_by(m.CompensationLink.created_at.desc())
            ).scalars()
        )
        ctx["match_shortfalls"] = sorted(match_shortfalls, key=lambda r: r.date, reverse=True)
        ctx["match_surpluses"] = sorted(match_surpluses, key=lambda r: r.date, reverse=True)
        ctx["match_shortfall_allocated_by_date"] = match_shortfall_allocated_by_date
        ctx["match_surplus_used_by_date"] = match_surplus_used_by_date
        ctx["match_links"] = [
            (lk, [dt.date.fromisoformat(x) for x in json.loads(lk.surplus_dates or "[]")])
            for lk in match_links
        ]
        (py, pm), (ny, nm) = prev_next_month(year, month)
        ctx["year"] = year
        ctx["month"] = month
        ctx["prev_ym"] = f"{py}-{pm:02d}"
        ctx["next_ym"] = f"{ny}-{nm:02d}"
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
    project_id: int = Form(0),
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
    at the database level.

    Ganesh, 2026-08-27: a suggested Task now also requires an existing,
    approved, active Project — same project-scoping every other task
    creation path (Lists page, bulk upload) now goes through (see
    ProjectTask's docstring in app/models.py). The ProjectTask link is
    created right away, even while the TaskType itself is still pending —
    there's no separate approval step for the link; approving the
    suggestion (suggestion_approve() in app/routes/admin.py) only flips
    TaskType.status, it doesn't touch ProjectTask. A suggested Project
    doesn't need this — project_id is ignored/blank for kind=project."""
    name = normalize_title_case(name)
    if not name:
        flash(request, "Enter a name before suggesting it.", "err")
        return RedirectResponse("/today", status_code=303)
    model = {"project": m.Project, "task": m.TaskType}.get(kind)
    if model is None:
        flash(request, "Unknown suggestion type.", "err")
        return RedirectResponse("/today", status_code=303)
    project = None
    if kind == "task":
        project = db.get(m.Project, project_id) if project_id else None
        if project is None or not project.active or project.status != m.LIST_APPROVED:
            flash(request, "Choose which Project this task is for before suggesting it.", "err")
            return RedirectResponse("/today", status_code=303)
    existing = db.execute(
        select(model).where(func.lower(model.name) == name.lower())
    ).scalar_one_or_none()
    if existing is not None:
        flash(request, f"'{existing.name}' already exists — pick it from the list instead of suggesting it again.", "err")
        return RedirectResponse("/today", status_code=303)
    item = model(name=name, active=True, status=m.LIST_PENDING, created_by_employee_id=user.id)
    db.add(item)
    if kind == "task":
        db.flush()  # need item.id for the ProjectTask link below
        db.add(m.ProjectTask(project_id=project.id, task_type_id=item.id, created_by=user.name))
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


@router.post("/plan/{plan_id}/assignment-notice/dismiss")
def dismiss_plan_assignment_notice(
    plan_id: int,
    request: Request,
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    """'Got it' on the "an admin assigned you a task" banner (Ganesh,
    2026-08-28, TK-04) — see _pending_plan_assignment_notices() and
    today.html. Ownership-checked (plan.employee_id must be this employee)
    same as dismiss_edit_notice's own pattern above; silently no-ops if the
    plan's already been dismissed, doesn't belong to them, or was self-
    planned (nothing to dismiss)."""
    plan = db.get(m.PlannedTask, plan_id)
    if plan is not None and plan.employee_id == user.id:
        plan.assigned_notified_at = dt.datetime.utcnow()
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
