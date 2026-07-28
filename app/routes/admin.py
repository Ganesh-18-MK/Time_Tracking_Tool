"""Admin screens (PRD §7): compliance dashboard, person detail, roster,
lists, leave + compensation, config, audit."""
import datetime as dt
import json
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import engine, models as m
from app.auth import require_admin
from app.db import get_db
from app.templating import flash, render
from app.util import audit, parse_ym, prev_next_month

router = APIRouter(prefix="/admin")


# --------------------------------------------------------------------------
# Compliance dashboard — the monthly sheet, rebuilt live (the core payoff)
# --------------------------------------------------------------------------
@router.get("")
def dashboard(
    request: Request,
    ym: Optional[str] = None,
    dept: Optional[str] = None,
    exceptions: int = 0,
    admin: m.Employee = Depends(require_admin),
    db: Session = Depends(get_db),
):
    cfg = engine.get_config(db)
    year, month = parse_ym(ym)
    first, last = engine.month_range(year, month)
    today = dt.date.today()

    # live months stay fresh on load (cheap at this scale; nightly job optional)
    if first <= today:
        engine.recompute_all(db, first, min(last, today))

    emps = list(
        db.execute(
            select(m.Employee)
            .where(m.Employee.active.is_(True), m.Employee.tracked.is_(True))
            .order_by(m.Employee.department, m.Employee.name)
        ).scalars()
    )
    all_depts = sorted({e.department or "—" for e in emps})
    if dept:
        emps = [e for e in emps if (e.department or "—") == dept]

    by_emp = engine.statuses_for_month(db, year, month)
    comp_erases = cfg.get("comp_erases_strike") == "1"
    threshold = engine.cfg_int(cfg, "strike_threshold")
    days = [first + dt.timedelta(days=i) for i in range((last - first).days + 1)]

    groups = {}
    for e in emps:
        rows = by_emp.get(e.id, {})
        strikes = engine.strikes_in(rows.values(), comp_erases)
        rec = {
            "emp": e,
            "cells": [rows.get(d) for d in days],
            "strikes": strikes,
            "violation": strikes >= threshold,
        }
        if exceptions and strikes == 0:
            continue
        groups.setdefault(e.department or "—", []).append(rec)

    (py, pm), (ny, nm) = prev_next_month(year, month)
    return render(
        request,
        "admin/dashboard.html",
        {
            "user": admin,
            "year": year,
            "month": month,
            "days": days,
            "groups": groups,
            "all_depts": all_depts,
            "dept": dept or "",
            "exceptions": exceptions,
            "threshold": threshold,
            "comp_erases": comp_erases,
            "today": today,
            "prev_ym": f"{py}-{pm:02d}",
            "next_ym": f"{ny}-{nm:02d}",
            "ym": f"{year}-{month:02d}",
        },
    )


@router.post("/recompute")
def recompute(
    request: Request,
    ym: str = Form(...),
    admin: m.Employee = Depends(require_admin),
    db: Session = Depends(get_db),
):
    year, month = parse_ym(ym)
    first, last = engine.month_range(year, month)
    n = engine.recompute_all(db, first, min(last, dt.date.today()))
    audit(db, admin.name, "recompute_month", "DayStatus", ym, {"rows": n})
    flash(request, f"Recomputed {n} day-statuses for {ym}.", "ok")
    return RedirectResponse(f"/admin?ym={ym}", status_code=303)


# --------------------------------------------------------------------------
# Person detail: full log, ledger, leave history, overrides, comp links
# --------------------------------------------------------------------------
@router.get("/person/{emp_id}")
def person(
    emp_id: int,
    request: Request,
    ym: Optional[str] = None,
    admin: m.Employee = Depends(require_admin),
    db: Session = Depends(get_db),
):
    emp = db.get(m.Employee, emp_id)
    if emp is None:
        return RedirectResponse("/admin/roster", status_code=303)
    cfg = engine.get_config(db)
    year, month = parse_ym(ym)
    first, last = engine.month_range(year, month)
    today = dt.date.today()
    if first <= today:
        engine.recompute_employee(db, emp, first, min(last, today), cfg)

    statuses = list(
        db.execute(
            select(m.DayStatus)
            .where(m.DayStatus.employee_id == emp.id, m.DayStatus.date.between(first, last))
            .order_by(m.DayStatus.date)
        ).scalars()
    )
    ledger = engine.running_ledger(db, emp, first, min(last, today))
    subs = {
        s.date: s
        for s in db.execute(
            select(m.DaySubmission).where(
                m.DaySubmission.employee_id == emp.id,
                m.DaySubmission.date.between(first, last),
            )
        ).scalars()
    }
    entries = list(
        db.execute(
            select(m.TaskEntry)
            .where(m.TaskEntry.employee_id == emp.id, m.TaskEntry.date.between(first, last))
            .order_by(m.TaskEntry.date.desc(), m.TaskEntry.start_minute)
        ).scalars()
    )
    by_day = {}
    for e in entries:
        by_day.setdefault(e.date, []).append(e)
    leaves = list(
        db.execute(
            select(m.LeaveRecord)
            .where(m.LeaveRecord.employee_id == emp.id)
            .order_by(m.LeaveRecord.start_date.desc())
        ).scalars()
    )
    links = list(
        db.execute(
            select(m.CompensationLink)
            .where(m.CompensationLink.employee_id == emp.id)
            .order_by(m.CompensationLink.shortfall_date.desc())
        ).scalars()
    )
    comp_erases = cfg.get("comp_erases_strike") == "1"
    strikes = engine.strikes_in(statuses, comp_erases)
    shortfalls = [
        r for r in statuses
        if (r.variance_minutes or 0) < 0 and r.effective_status(comp_erases) in m.STRIKE_STATUSES
    ]
    surpluses = [r for r in statuses if (r.variance_minutes or 0) > 0]
    (py, pm), (ny, nm) = prev_next_month(year, month)
    return render(
        request,
        "admin/person.html",
        {
            "user": admin,
            "emp": emp,
            "statuses": statuses,
            "ledger": ledger,
            "balance": ledger[-1]["balance"] if ledger else 0,
            "subs": subs,
            "by_day": sorted(by_day.items(), reverse=True),
            "leaves": leaves,
            "links": [
                (lk, [dt.date.fromisoformat(x) for x in json.loads(lk.surplus_dates or "[]")])
                for lk in links
            ],
            "strikes": strikes,
            "threshold": engine.cfg_int(cfg, "strike_threshold"),
            "comp_erases": comp_erases,
            "shortfalls": shortfalls,
            "surpluses": surpluses,
            "year": year,
            "month": month,
            "ym": f"{year}-{month:02d}",
            "prev_ym": f"{py}-{pm:02d}",
            "next_ym": f"{ny}-{nm:02d}",
            "today": today,
        },
    )


@router.post("/person/{emp_id}/unlock")
def unlock_day(
    emp_id: int,
    request: Request,
    date: str = Form(...),
    reason: str = Form(...),
    ym: str = Form(""),
    admin: m.Employee = Depends(require_admin),
    db: Session = Depends(get_db),
):
    day = dt.date.fromisoformat(date)
    sub = db.execute(
        select(m.DaySubmission).where(
            m.DaySubmission.employee_id == emp_id, m.DaySubmission.date == day
        )
    ).scalar_one_or_none()
    if sub is None:
        flash(request, "No submission found for that day.", "err")
    else:
        sub.locked = False
        sub.unlock_count += 1
        db.commit()
        # every unlock is logged: who, when, what (PRD §4)
        audit(
            db, admin.name, "unlock_day", "DaySubmission", f"{emp_id}:{date}",
            {"reason": reason, "unlock_count": sub.unlock_count},
        )
        flash(request, f"Unlocked {date} — the employee can now edit and resubmit.", "ok")
    return RedirectResponse(f"/admin/person/{emp_id}?ym={ym}", status_code=303)


@router.post("/person/{emp_id}/override")
def override_day(
    emp_id: int,
    request: Request,
    date: str = Form(...),
    status: str = Form(""),
    reason: str = Form(""),
    ym: str = Form(""),
    admin: m.Employee = Depends(require_admin),
    db: Session = Depends(get_db),
):
    emp = db.get(m.Employee, emp_id)
    day = dt.date.fromisoformat(date)
    row = db.execute(
        select(m.DayStatus).where(
            m.DayStatus.employee_id == emp_id, m.DayStatus.date == day
        )
    ).scalar_one_or_none()
    if row is None:
        # materialize a base row so the override has something to sit on
        engine.recompute_employee(db, emp, day, day)
        row = db.execute(
            select(m.DayStatus).where(
                m.DayStatus.employee_id == emp_id, m.DayStatus.date == day
            )
        ).scalar_one_or_none()
        if row is None:
            row = m.DayStatus(
                employee_id=emp_id, date=day, status=m.MISSING,
                actual_minutes=0, target_minutes=emp.daily_target_minutes,
                variance_minutes=None, source="computed",
            )
            db.add(row)
            db.commit()
    before = row.override_status or row.status
    if status == "":  # clear override
        row.override_status = None
        row.override_reason = ""
        row.override_by = ""
        row.override_at = None
        db.commit()
        audit(db, admin.name, "clear_override", "DayStatus", f"{emp_id}:{date}", {"was": before})
        flash(request, f"Override cleared for {date}.", "ok")
    else:
        if not reason.strip():
            flash(request, "Override requires a reason (it changes the strike count).", "err")
            return RedirectResponse(f"/admin/person/{emp_id}?ym={ym}", status_code=303)
        row.override_status = status
        row.override_reason = reason.strip()
        row.override_by = admin.name
        row.override_at = dt.datetime.utcnow()
        db.commit()
        audit(
            db, admin.name, "override_status", "DayStatus", f"{emp_id}:{date}",
            {"before": before, "after": status, "reason": reason.strip()},
        )
        flash(request, f"Status for {date} overridden to {status}.", "ok")
    return RedirectResponse(f"/admin/person/{emp_id}?ym={ym}", status_code=303)


@router.post("/person/{emp_id}/complink")
def add_complink(
    emp_id: int,
    request: Request,
    shortfall_date: str = Form(...),
    surplus_dates: str = Form(...),
    note: str = Form(""),
    ym: str = Form(""),
    admin: m.Employee = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        surplus = sorted({dt.date.fromisoformat(x.strip()).isoformat()
                          for x in surplus_dates.replace(";", ",").split(",") if x.strip()})
    except ValueError:
        flash(request, "Surplus dates must be ISO dates separated by commas.", "err")
        return RedirectResponse(f"/admin/person/{emp_id}?ym={ym}", status_code=303)
    if not surplus:
        flash(request, "Pick at least one surplus day.", "err")
        return RedirectResponse(f"/admin/person/{emp_id}?ym={ym}", status_code=303)
    # a surplus day backs at most one shortfall (keeps the math honest)
    taken = engine.surplus_links_by_date(db, emp_id)
    clash = [s for s in surplus if dt.date.fromisoformat(s) in taken]
    if clash:
        flash(request, f"Surplus day(s) already linked to another shortfall: {', '.join(clash)}", "err")
        return RedirectResponse(f"/admin/person/{emp_id}?ym={ym}", status_code=303)
    link = m.CompensationLink(
        employee_id=emp_id,
        shortfall_date=dt.date.fromisoformat(shortfall_date),
        surplus_dates=json.dumps(surplus),
        note=note.strip(),
        linked_by=admin.name,
    )
    db.add(link)
    db.commit()
    engine.evaluate_link(db, link)
    audit(
        db, admin.name, "compensation_link", "CompensationLink", link.id,
        {"shortfall": shortfall_date, "surplus": surplus, "fully": link.fully_compensated,
         "note": note.strip()},
    )
    flash(
        request,
        "Compensation link created"
        + (" — shortfall fully covered, day now reads Complete."
           if link.fully_compensated else " — not yet fully covered (kept as-is)."),
        "ok",
    )
    return RedirectResponse(f"/admin/person/{emp_id}?ym={ym}", status_code=303)


@router.post("/complink/{link_id}/delete")
def delete_complink(
    link_id: int,
    request: Request,
    ym: str = Form(""),
    admin: m.Employee = Depends(require_admin),
    db: Session = Depends(get_db),
):
    link = db.get(m.CompensationLink, link_id)
    if link is not None:
        emp_id = link.employee_id
        row = db.execute(
            select(m.DayStatus).where(
                m.DayStatus.employee_id == emp_id, m.DayStatus.date == link.shortfall_date
            )
        ).scalar_one_or_none()
        if row is not None:
            row.compensated = False
        db.delete(link)
        db.commit()
        audit(db, admin.name, "delete_compensation_link", "CompensationLink", link_id,
              {"shortfall": link.shortfall_date.isoformat()})
        return RedirectResponse(f"/admin/person/{emp_id}?ym={ym}", status_code=303)
    return RedirectResponse("/admin", status_code=303)


# --------------------------------------------------------------------------
# Roster
# --------------------------------------------------------------------------
@router.get("/roster")
def roster(
    request: Request,
    show: str = "active",
    admin: m.Employee = Depends(require_admin),
    db: Session = Depends(get_db),
):
    q = select(m.Employee).order_by(m.Employee.active.desc(), m.Employee.department, m.Employee.name)
    emps = list(db.execute(q).scalars())
    if show == "active":
        emps = [e for e in emps if e.active]
    return render(request, "admin/roster.html", {"user": admin, "emps": emps, "show": show})


def _emp_from_form(
    emp: m.Employee, name, email, department, designation, target_hours,
    work_days, start_date, active, tracked, is_admin,
):
    emp.name = name.strip()
    emp.email = email.strip()
    emp.department = department.strip()
    emp.designation = designation.strip()
    emp.daily_target_minutes = int(round(float(target_hours or 8) * 60))
    emp.work_days = ",".join(str(d) for d in sorted({int(x) for x in work_days})) if work_days else "0,1,2,3,4"
    emp.start_date = dt.date.fromisoformat(start_date) if start_date else None
    emp.active = bool(active)
    emp.tracked = bool(tracked)
    emp.is_admin = bool(is_admin)


@router.post("/roster/add")
def roster_add(
    request: Request,
    name: str = Form(...),
    email: str = Form(""),
    department: str = Form(""),
    designation: str = Form(""),
    target_hours: str = Form("8"),
    work_days: list = Form(["0", "1", "2", "3", "4"]),
    start_date: str = Form(""),
    active: str = Form("1"),
    tracked: str = Form("1"),
    is_admin: str = Form(""),
    admin: m.Employee = Depends(require_admin),
    db: Session = Depends(get_db),
):
    emp = m.Employee(name=name.strip())
    _emp_from_form(emp, name, email, department, designation, target_hours,
                   work_days, start_date, active == "1", tracked == "1", is_admin == "1")
    db.add(emp)
    db.commit()
    audit(db, admin.name, "roster_add", "Employee", emp.id, {"name": emp.name})
    flash(request, f"Added {emp.name}.", "ok")
    return RedirectResponse("/admin/roster", status_code=303)


@router.get("/roster/{emp_id}/edit")
def roster_edit_page(
    emp_id: int,
    request: Request,
    admin: m.Employee = Depends(require_admin),
    db: Session = Depends(get_db),
):
    emp = db.get(m.Employee, emp_id)
    if emp is None:
        return RedirectResponse("/admin/roster", status_code=303)
    return render(request, "admin/roster_edit.html", {"user": admin, "emp": emp})


@router.post("/roster/{emp_id}/edit")
def roster_edit(
    emp_id: int,
    request: Request,
    name: str = Form(...),
    email: str = Form(""),
    department: str = Form(""),
    designation: str = Form(""),
    target_hours: str = Form("8"),
    work_days: list = Form([]),
    start_date: str = Form(""),
    active: str = Form(""),
    tracked: str = Form(""),
    is_admin: str = Form(""),
    admin: m.Employee = Depends(require_admin),
    db: Session = Depends(get_db),
):
    emp = db.get(m.Employee, emp_id)
    if emp is None:
        return RedirectResponse("/admin/roster", status_code=303)
    before = {
        "name": emp.name, "department": emp.department, "designation": emp.designation,
        "target": emp.daily_target_minutes, "work_days": emp.work_days,
        "active": emp.active, "tracked": emp.tracked, "is_admin": emp.is_admin,
    }
    _emp_from_form(emp, name, email, department, designation, target_hours,
                   work_days, start_date, active == "1", tracked == "1", is_admin == "1")
    db.commit()
    audit(db, admin.name, "roster_edit", "Employee", emp.id, {"before": before})
    flash(request, f"Saved {emp.name}." + ("" if emp.active else " (deactivated — history kept, dropped from compliance runs)"), "ok")
    return RedirectResponse("/admin/roster", status_code=303)


# --------------------------------------------------------------------------
# Lists (Project/Employer + Task dropdowns)
# --------------------------------------------------------------------------
@router.get("/lists")
def lists_page(
    request: Request,
    admin: m.Employee = Depends(require_admin),
    db: Session = Depends(get_db),
):
    projects = list(db.execute(select(m.Project).order_by(m.Project.active.desc(), m.Project.name)).scalars())
    tasks = list(db.execute(select(m.TaskType).order_by(m.TaskType.active.desc(), m.TaskType.name)).scalars())
    return render(request, "admin/lists.html", {"user": admin, "projects": projects, "tasks": tasks})


@router.post("/lists/add")
def lists_add(
    request: Request,
    kind: str = Form(...),
    name: str = Form(...),
    admin: m.Employee = Depends(require_admin),
    db: Session = Depends(get_db),
):
    model = m.Project if kind == "project" else m.TaskType
    name = name.strip()
    if name:
        exists = db.execute(select(model).where(model.name == name)).scalar_one_or_none()
        if exists is None:
            db.add(model(name=name))
            db.commit()
            audit(db, admin.name, f"add_{kind}", kind, name, {})
        else:
            flash(request, f"'{name}' already exists.", "err")
    return RedirectResponse("/admin/lists", status_code=303)


@router.post("/lists/{kind}/{item_id}/toggle")
def lists_toggle(
    kind: str,
    item_id: int,
    request: Request,
    admin: m.Employee = Depends(require_admin),
    db: Session = Depends(get_db),
):
    model = m.Project if kind == "project" else m.TaskType
    item = db.get(model, item_id)
    if item is not None:
        # deactivating hides from new entries without breaking old rows (PRD §7)
        item.active = not item.active
        db.commit()
        audit(db, admin.name, f"toggle_{kind}", kind, item.name, {"active": item.active})
    return RedirectResponse("/admin/lists", status_code=303)


# --------------------------------------------------------------------------
# Leave entry (admin-entered in POC — open question 5)
# --------------------------------------------------------------------------
@router.get("/leave")
def leave_page(
    request: Request,
    admin: m.Employee = Depends(require_admin),
    db: Session = Depends(get_db),
):
    emps = list(
        db.execute(
            select(m.Employee).where(m.Employee.active.is_(True)).order_by(m.Employee.name)
        ).scalars()
    )
    recent = list(
        db.execute(
            select(m.LeaveRecord).order_by(m.LeaveRecord.created_at.desc()).limit(60)
        ).scalars()
    )
    return render(
        request, "admin/leave.html",
        {"user": admin, "emps": emps, "recent": recent, "leave_types": m.LEAVE_TYPES},
    )


@router.post("/leave/add")
def leave_add(
    request: Request,
    employee_id: int = Form(...),
    start_date: str = Form(...),
    end_date: str = Form(""),
    type: str = Form("Other"),
    hours: str = Form(""),
    note: str = Form(""),
    admin: m.Employee = Depends(require_admin),
    db: Session = Depends(get_db),
):
    emp = db.get(m.Employee, employee_id)
    if emp is None:
        return RedirectResponse("/admin/leave", status_code=303)
    start = dt.date.fromisoformat(start_date)
    end = dt.date.fromisoformat(end_date) if end_date else start
    if end < start:
        flash(request, "End date is before start date.", "err")
        return RedirectResponse("/admin/leave", status_code=303)
    minutes = None  # full day = person's daily target (PRD §5)
    if hours.strip():
        try:
            minutes = int(round(float(hours) * 60))
        except ValueError:
            flash(request, "Hours must be a number (leave blank for full day).", "err")
            return RedirectResponse("/admin/leave", status_code=303)
    if type == "Other" and not note.strip():
        flash(request, "'Other' leave needs a note (PRD §5).", "err")
        return RedirectResponse("/admin/leave", status_code=303)
    lv = m.LeaveRecord(
        employee_id=employee_id, start_date=start, end_date=end,
        type=type, minutes_per_day=minutes, note=note.strip(), entered_by=admin.name,
    )
    db.add(lv)
    db.commit()
    audit(db, admin.name, "leave_add", "LeaveRecord", lv.id,
          {"employee": emp.name, "range": f"{start}..{end}", "type": type, "minutes": minutes})
    engine.recompute_employee(db, emp, start, min(end, dt.date.today()))
    flash(request, f"Leave recorded for {emp.name}.", "ok")
    return RedirectResponse("/admin/leave", status_code=303)


@router.post("/leave/{leave_id}/delete")
def leave_delete(
    leave_id: int,
    request: Request,
    admin: m.Employee = Depends(require_admin),
    db: Session = Depends(get_db),
):
    lv = db.get(m.LeaveRecord, leave_id)
    if lv is not None:
        emp = db.get(m.Employee, lv.employee_id)
        db.delete(lv)
        db.commit()
        audit(db, admin.name, "leave_delete", "LeaveRecord", leave_id,
              {"employee": emp.name if emp else lv.employee_id})
        if emp is not None:
            engine.recompute_employee(db, emp, lv.start_date, min(lv.end_date, dt.date.today()))
    return RedirectResponse("/admin/leave", status_code=303)


# --------------------------------------------------------------------------
# Config (PRD §10 — every open question is a dial here) + holidays
# --------------------------------------------------------------------------
@router.get("/config")
def config_page(
    request: Request,
    admin: m.Employee = Depends(require_admin),
    db: Session = Depends(get_db),
):
    cfg = engine.get_config(db)
    holidays = list(db.execute(select(m.Holiday).order_by(m.Holiday.date)).scalars())
    return render(request, "admin/config.html", {"user": admin, "cfg": cfg, "holidays": holidays})


@router.post("/config")
def config_save(
    request: Request,
    tolerance_hours: str = Form("1.0"),
    strike_threshold: str = Form("5"),
    max_row_hours: str = Form("4"),
    backdate_working_days: str = Form("1"),
    gap_flag_minutes: str = Form("15"),
    min_details_chars: str = Form("5"),
    comp_erases_strike: str = Form(""),
    live_start_date: str = Form(""),
    admin: m.Employee = Depends(require_admin),
    db: Session = Depends(get_db),
):
    before = engine.get_config(db)
    values = {
        "tolerance_minutes": str(int(round(float(tolerance_hours) * 60))),
        "strike_threshold": str(int(strike_threshold)),
        "max_row_minutes": str(int(round(float(max_row_hours) * 60))),
        "backdate_working_days": str(int(backdate_working_days)),
        "gap_flag_minutes": str(int(gap_flag_minutes)),
        "min_details_chars": str(int(min_details_chars)),
        "comp_erases_strike": "1" if comp_erases_strike == "1" else "0",
        "live_start_date": live_start_date.strip(),
    }
    for k, v in values.items():
        row = db.get(m.Config, k)
        if row is None:
            db.add(m.Config(key=k, value=v))
        else:
            row.value = v
    db.commit()
    audit(db, admin.name, "config_change", "Config", "",
          {"before": {k: before.get(k) for k in values}, "after": values})
    flash(request, "Config saved. Recompute affected months to apply retroactively.", "ok")
    return RedirectResponse("/admin/config", status_code=303)


@router.post("/holidays/add")
def holiday_add(
    request: Request,
    date: str = Form(...),
    name: str = Form(""),
    admin: m.Employee = Depends(require_admin),
    db: Session = Depends(get_db),
):
    d = dt.date.fromisoformat(date)
    if db.execute(select(m.Holiday).where(m.Holiday.date == d)).scalar_one_or_none() is None:
        db.add(m.Holiday(date=d, name=name.strip()))
        db.commit()
        audit(db, admin.name, "holiday_add", "Holiday", date, {"name": name.strip()})
    return RedirectResponse("/admin/config", status_code=303)


@router.post("/holidays/{holiday_id}/delete")
def holiday_delete(
    holiday_id: int,
    request: Request,
    admin: m.Employee = Depends(require_admin),
    db: Session = Depends(get_db),
):
    h = db.get(m.Holiday, holiday_id)
    if h is not None:
        db.delete(h)
        db.commit()
        audit(db, admin.name, "holiday_delete", "Holiday", h.date.isoformat(), {})
    return RedirectResponse("/admin/config", status_code=303)


# --------------------------------------------------------------------------
# Audit log
# --------------------------------------------------------------------------
@router.get("/audit")
def audit_page(
    request: Request,
    admin: m.Employee = Depends(require_admin),
    db: Session = Depends(get_db),
):
    rows = list(
        db.execute(select(m.AuditLog).order_by(m.AuditLog.at.desc()).limit(300)).scalars()
    )
    return render(request, "admin/audit.html", {"user": admin, "rows": rows})
