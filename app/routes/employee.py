"""Employee screens: Today (log + submit) and My Month (PRD §7)."""
import datetime as dt
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import engine, models as m
from app.auth import current_user
from app.db import get_db
from app.templating import flash, render
from app.util import audit, parse_hhmm
from app.validation import EntryError, earliest_allowed_date, gap_flags, validate_entry

router = APIRouter()


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
    target = max(0, emp.daily_target_minutes - leave_min)
    return {
        "entries": entries,
        "total": total,
        "sub": sub,
        "target": target,
        "leave_min": leave_min,
        "flags": gap_flags(entries, engine.cfg_int(cfg, "gap_flag_minutes")),
    }


@router.get("/today")
def today_page(
    request: Request,
    date: Optional[str] = None,
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    cfg = engine.get_config(db)
    day = dt.date.fromisoformat(date) if date else dt.date.today()
    projects = list(
        db.execute(
            select(m.Project).where(m.Project.active.is_(True)).order_by(m.Project.name)
        ).scalars()
    )
    tasks = list(
        db.execute(
            select(m.TaskType).where(m.TaskType.active.is_(True)).order_by(m.TaskType.name)
        ).scalars()
    )
    ctx = _day_context(db, user, day, cfg)
    last_end = max((e.end_minute for e in ctx["entries"]), default=None)
    ctx.update(
        {
            "suggest_start": f"{last_end // 60:02d}:{last_end % 60:02d}" if last_end else "",
            "user": user,
            "day": day,
            "today": dt.date.today(),
            "allowed_dates": _allowed_dates(db, user, cfg),
            "projects": projects,
            "tasks": tasks,
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

    # leave by category (PRD §5: computed, not typed)
    leave_totals = {}
    for lv in db.execute(
        select(m.LeaveRecord).where(
            m.LeaveRecord.employee_id == user.id,
            m.LeaveRecord.start_date <= last,
            m.LeaveRecord.end_date >= first,
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
            "prev_ym": f"{py}-{pm:02d}",
            "next_ym": f"{ny}-{nm:02d}",
            "today": dt.date.today(),
        },
    )
