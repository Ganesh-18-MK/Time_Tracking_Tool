"""Employee screens: Today (log + submit) and My Month (PRD §7)."""
import datetime as dt
import os
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app import compensation, engine, models as m
from app.auth import current_user
from app.db import get_db
from app.templating import flash, render
from app.util import (
    FormError,
    audit,
    clamp_break_end,
    overtime_minutes,
    parse_date_field,
    parse_hhmm,
    parse_int_field,
    punch_remaining_minutes,
)
from app.validation import EntryError, earliest_allowed_date, gap_flags, validate_entry

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
    today = dt.date.today()
    earliest = earliest_allowed_date(
        emp, today, engine.cfg_int(cfg, "backdate_working_days"), engine.holidays_set(db)
    )
    days = []
    d = earliest
    while d <= today:
        days.append(d)
        d += dt.timedelta(days=1)
    return days


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

    flags = gap_flags(entries, engine.cfg_int(cfg, "gap_flag_minutes"))
    if flags:
        ordered = sorted(entries, key=lambda e: e.start_minute)
        for prev, cur in zip(ordered, ordered[1:]):
            if cur.id not in flags:
                continue
            gap_start, gap_end = prev.end_minute, cur.start_minute
            # a gap the employee logged as a break isn't an unexplained gap
            if any(
                b.start_minute <= gap_start and (b.end_minute or gap_end) >= gap_end
                for b in completed_breaks
            ):
                del flags[cur.id]

    return {
        "entries": entries,
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
    dropdowns: every approved, active one, PLUS any pending suggestion
    THEY made themselves (Ganesh, 2026-08-01 — usable by the submitter
    right away, invisible to everyone else until a team lead approves it).
    validate_entry() enforces the same rule server-side, so this filter is
    a UI convenience, not the only thing standing between an employee and
    someone else's not-yet-approved suggestion."""
    visible = or_(
        m.Project.status == m.LIST_APPROVED,
        and_(m.Project.status == m.LIST_PENDING, m.Project.created_by_employee_id == user.id),
    )
    projects = list(
        db.execute(
            select(m.Project).where(m.Project.active.is_(True), visible).order_by(m.Project.name)
        ).scalars()
    )
    visible_t = or_(
        m.TaskType.status == m.LIST_APPROVED,
        and_(m.TaskType.status == m.LIST_PENDING, m.TaskType.created_by_employee_id == user.id),
    )
    tasks = list(
        db.execute(
            select(m.TaskType).where(m.TaskType.active.is_(True), visible_t).order_by(m.TaskType.name)
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
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    cfg = engine.get_config(db)
    day = dt.date.fromisoformat(date) if date else dt.date.today()
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
            "suggest_start": f"{last_end // 60:02d}:{last_end % 60:02d}" if last_end else "",
            "user": user,
            "day": day,
            "today": dt.date.today(),
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
    try:
        start_minute = parse_hhmm(start_time)
        end_minute = parse_hhmm(end_time)
    except (ValueError, IndexError):
        flash(request, "Enter valid start and end times.", "err")
        return RedirectResponse(f"/today?date={day.isoformat()}", status_code=303)
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
    today = dt.date.today()
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

    now = dt.datetime.now()
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
    today = dt.date.today()
    active = db.execute(
        select(m.BreakEntry).where(
            m.BreakEntry.employee_id == user.id, m.BreakEntry.date == today,
            m.BreakEntry.end_minute.is_(None),
        )
    ).scalar_one_or_none()
    if active is not None:
        now = dt.datetime.now()
        active.end_minute = clamp_break_end(active.start_minute, now.hour * 60 + now.minute)
        active.ended_at = dt.datetime.utcnow()
        db.commit()
        flash(request, f"Break ended — {active.duration_minutes} min.", "ok")
    return RedirectResponse("/today", status_code=303)


def _finish_task_timer(db: Session, user: m.Employee, timer: m.ActiveTaskTimer, cfg: dict):
    """Converts a running ActiveTaskTimer into a real TaskEntry, through
    the exact same validate_entry() every manually-typed row goes through
    (overlap / 4h-cap / details-length / locked-day checks) — an
    auto-captured entry is never held to looser rules than a typed one.
    Returns (True, None) on success; on failure returns (False, message)
    and leaves the timer running/untouched so nothing is silently lost —
    the employee can fix Details and try Stop again, or keep working."""
    now = dt.datetime.now()
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
    today = dt.date.today()

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

    now = dt.datetime.now()
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
    today = dt.date.today()
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
    today = dt.date.today()
    active = db.execute(
        select(m.PunchSession).where(
            m.PunchSession.employee_id == user.id, m.PunchSession.date == today,
            m.PunchSession.punched_out_at.is_(None),
        )
    ).scalar_one_or_none()
    if active is not None:
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
    engine.recompute_employee(db, user, first, min(last, dt.date.today()), cfg)

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

    ledger = engine.running_ledger(db, user, first, min(last, dt.date.today()))
    balance = ledger[-1]["balance"] if ledger else 0
    comp = compensation.monthly_summary(db, user, year, month)
    (py, pm), (ny, nm) = prev_next_month(year, month)
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
            "comp": comp,
            "prev_ym": f"{py}-{pm:02d}",
            "next_ym": f"{ny}-{nm:02d}",
            "today": dt.date.today(),
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
    return render(
        request, "leave.html",
        {"user": user, "records": records, "leave_types": m.LEAVE_TYPES, "today": dt.date.today()},
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


@router.post("/suggestions")
def suggest_list_item(
    request: Request,
    kind: str = Form(...),
    name: str = Form(...),
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Employee/lead-suggested Project or Task (Ganesh, 2026-08-01) —
    usable by whoever suggested it right away (see
    _visible_projects_and_tasks and validate_entry above), invisible to
    everyone else until a team lead approves it (see app/routes/admin.py
    suggestions_page / suggestion_approve)."""
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
    flash(request, f"{label} '{name}' suggested — you can use it right away; a team lead will review it soon.", "ok")
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
    return render(request, "profile.html", {"user": user, "pd": user.personal_details, "bd": user.bank_details})


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
