"""Admin screens (PRD §7): compliance dashboard, person detail, roster,
lists, leave + compensation, config, audit."""
import datetime as dt
import io
import json
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, StreamingResponse
from openpyxl import load_workbook
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import bulk_upload, compensation, engine, leave_bulk_upload, lists_bulk_upload, models as m
from app.auth import Forbidden, admin_department_scope, led_by, require_admin, require_super_admin
from app.db import get_db
from app.templating import TICKETING_ENABLED, flash, render
from app.util import (
    FormError,
    ROLE_EMPLOYEE,
    audit,
    next_employee_code,
    parse_date_field,
    parse_hours_field,
    parse_int_field,
    parse_ym,
    prev_next_month,
    role_to_flags,
    today_local,
)

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
    today = today_local()

    # live months stay fresh on load (cheap at this scale; nightly job optional)
    if first <= today:
        engine.recompute_all(db, first, min(last, today))

    # Department-scoped admin (team lead): None for a super admin (no
    # restriction). Filtering all_emps here — before any downstream
    # list/count is built from it — scopes the whole page in one place;
    # nothing below needs to know the difference between the two tiers.
    scope = admin_department_scope(admin)

    all_emps = list(
        db.execute(
            select(m.Employee)
            .where(m.Employee.active.is_(True), m.Employee.tracked.is_(True))
            .order_by(m.Employee.department, m.Employee.name)
        ).scalars()
    )
    if scope is not None:
        all_emps = [e for e in all_emps if (e.department or "—") == scope]
        if dept is not None:
            dept = scope  # ignore/override ?dept= tampering — always their own
    all_depts = sorted({e.department or "—" for e in all_emps})
    dept_counts = {}
    for e in all_emps:
        key = e.department or "—"
        dept_counts[key] = dept_counts.get(key, 0) + 1
    total_emps = len(all_emps)
    emps = [e for e in all_emps if (e.department or "—") == dept] if dept else all_emps

    # The detail grid (every employee's day-by-day cells) only renders once
    # a department has actually been picked — dept is None on first load
    # ("/admin" with no query string) vs "" once the "All departments" card
    # is explicitly clicked ("/admin?dept="). Checked here, before dept is
    # coerced to "" below for display/filtering, since that coercion would
    # otherwise erase the distinction the template needs for this branch.
    show_grid = dept is not None

    # live today-snapshot for the landing KPI cards — independent of which
    # month's grid is being browsed below (see engine.today_attendance)
    attendance = engine.today_attendance(db, cfg, today)
    # same snapshot, broken down per department — feeds the "N present
    # today" number on each department card (dept_counts stays the total
    # headcount, shown alongside it).
    dept_present = {}
    for e in attendance["logged"]:
        key = e.department or "—"
        dept_present[key] = dept_present.get(key, 0) + 1

    by_emp = engine.statuses_for_month(db, year, month)
    comp_erases = cfg.get("comp_erases_strike") == "1"
    threshold = engine.cfg_int(cfg, "strike_threshold")
    days = [first + dt.timedelta(days=i) for i in range((last - first).days + 1)]

    # "Needs attention" + "Recent activity" — only rendered on the landing
    # view (not show_grid), so skip the extra queries when they won't be
    # used. Violations are computed org-wide from all_emps (not the
    # dept-filtered `emps`) since this is meant to surface everything that
    # needs a look, regardless of which department card was last clicked.
    pending_leave_rows, open_support_rows, violations, recent_audit = [], [], [], []
    if not show_grid:
        pending_leave_rows = list(
            db.execute(
                select(m.LeaveRecord)
                .where(m.LeaveRecord.status == m.LEAVE_REQUESTED)
                .order_by(m.LeaveRecord.created_at)
                .limit(5)
            ).scalars()
        )
        # Support Inbox and Audit Log are super-admin-only screens (see
        # require_super_admin) — a department-scoped admin doesn't get
        # these landing-page previews either, since neither can be safely
        # scoped to "their department only" (support queries aren't
        # department-tagged, and audit entries span every entity type).
        if scope is None:
            open_support_rows = list(
                db.execute(
                    select(m.SupportQuery)
                    .where(m.SupportQuery.status == m.SUPPORT_OPEN)
                    .order_by(m.SupportQuery.created_at)
                    .limit(5)
                ).scalars()
            )
            recent_audit = list(
                db.execute(select(m.AuditLog).order_by(m.AuditLog.at.desc()).limit(8)).scalars()
            )
        else:
            scoped_ids = {e.id for e in all_emps}
            pending_leave_rows = [lv for lv in pending_leave_rows if lv.employee_id in scoped_ids]
        # Ganesh, 2026-08-01: also surface anyone sitting on an open (not
        # yet compensation-linked) shortfall day, not just employees who've
        # already crossed the strike threshold — so an admin can catch and
        # fix a single Partial/Missing day via Person Detail's
        # Compensation links before it ever becomes a violation.
        # strikes_in() already only counts effective_status() days still in
        # STRIKE_STATUSES (a compensated day reads Complete and drops out),
        # so e_strikes doubles as "how many open shortfall days" — no
        # separate query needed. is_violation flags the third tuple element
        # so the template can badge threshold-crossers differently from a
        # lone still-fixable shortfall.
        for e in all_emps:
            e_strikes = engine.strikes_in(by_emp.get(e.id, {}).values(), comp_erases)
            if e_strikes > 0:
                violations.append((e, e_strikes, e_strikes >= threshold))
        violations.sort(key=lambda row: (not row[2], -row[1]))
        violations = violations[:8]

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
            "dept_counts": dept_counts,
            "dept_present": dept_present,
            "total_emps": total_emps,
            "show_grid": show_grid,
            "attendance": attendance,
            "pending_leave_rows": pending_leave_rows,
            "open_support_rows": open_support_rows,
            "violations": violations,
            "recent_audit": recent_audit,
            "dept": dept or "",
            "exceptions": exceptions,
            "threshold": threshold,
            "comp_erases": comp_erases,
            "today": today,
            "prev_ym": f"{py}-{pm:02d}",
            "next_ym": f"{ny}-{nm:02d}",
            "ym": f"{year}-{month:02d}",
        },
        db=db,
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
    n = engine.recompute_all(db, first, min(last, today_local()))
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
    scope = admin_department_scope(admin)
    if scope is not None and (emp.department or "—") != scope:
        # Department-scoped admin trying to view someone outside their own
        # team (e.g. by guessing/editing the URL) — same "you don't have
        # access" redirect as any other Forbidden.
        raise Forbidden()
    cfg = engine.get_config(db)
    year, month = parse_ym(ym)
    first, last = engine.month_range(year, month)
    today = today_local()
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
    breaks = list(
        db.execute(
            select(m.BreakEntry)
            .where(m.BreakEntry.employee_id == emp.id, m.BreakEntry.date.between(first, last))
            .order_by(m.BreakEntry.date.desc(), m.BreakEntry.start_minute)
        ).scalars()
    )
    comp_erases = cfg.get("comp_erases_strike") == "1"
    strikes = engine.strikes_in(statuses, comp_erases)
    shortfalls = [
        r for r in statuses
        if (r.variance_minutes or 0) < 0 and r.effective_status(comp_erases) in m.STRIKE_STATUSES
    ]
    surpluses = [r for r in statuses if (r.variance_minutes or 0) > 0]
    comp = compensation.monthly_summary(db, emp, year, month, today)
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
            "breaks": breaks,
            "max_break_minutes": engine.cfg_int(cfg, "max_break_minutes"),
            "links": [
                (lk, [dt.date.fromisoformat(x) for x in json.loads(lk.surplus_dates or "[]")])
                for lk in links
            ],
            "strikes": strikes,
            "threshold": engine.cfg_int(cfg, "strike_threshold"),
            "comp_erases": comp_erases,
            "shortfalls": shortfalls,
            "surpluses": surpluses,
            "comp": comp,
            "year": year,
            "month": month,
            "ym": f"{year}-{month:02d}",
            "prev_ym": f"{py}-{pm:02d}",
            "next_ym": f"{ny}-{nm:02d}",
            "today": today,
        },
        db=db,
    )


@router.post("/person/{emp_id}/unlock")
def unlock_day(
    emp_id: int,
    request: Request,
    date: str = Form(...),
    reason: str = Form(...),
    ym: str = Form(""),
    admin: m.Employee = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    try:
        day = parse_date_field(date)
    except FormError as e:
        flash(request, e.message, "err")
        return RedirectResponse(f"/admin/person/{emp_id}?ym={ym}", status_code=303)
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
    admin: m.Employee = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    emp = db.get(m.Employee, emp_id)
    try:
        day = parse_date_field(date)
    except FormError as e:
        flash(request, e.message, "err")
        return RedirectResponse(f"/admin/person/{emp_id}?ym={ym}", status_code=303)
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
    surplus_dates: list = Form([]),  # checkboxes, same convention as assignments_save's project_ids
    note: str = Form(""),
    ym: str = Form(""),
    admin: m.Employee = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    try:
        shortfall = parse_date_field(shortfall_date, "Shortfall date")
        surplus = sorted({dt.date.fromisoformat(x.strip()).isoformat()
                          for x in surplus_dates if x.strip()})
    except (FormError, ValueError) as e:
        flash(request, e.message if isinstance(e, FormError)
              else "Surplus dates must be valid ISO dates.", "err")
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
        shortfall_date=shortfall,
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
    admin: m.Employee = Depends(require_super_admin),
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
    admin: m.Employee = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    q = select(m.Employee).order_by(m.Employee.active.desc(), m.Employee.department, m.Employee.name)
    emps = list(db.execute(q).scalars())
    if show == "active":
        emps = [e for e in emps if e.active]

    # department pill row above the table — counts reflect whatever's
    # actually listed (respects the active/all toggle above)
    dept_counts: dict = {}
    for e in emps:
        key = e.department or "—"
        dept_counts[key] = dept_counts.get(key, 0) + 1
    all_depts = sorted(dept_counts)
    # dropdown always offers active people to report to, regardless of the
    # active/all toggle above (which only controls the table listing).
    # is_admin-only (Ganesh's manager, 2026-08-03): a "Reports to" pick is
    # now also that employee's Team Lead for Overtime Requests (see
    # app/auth.py led_by()) — someone with no admin access couldn't act on
    # a request routed to them anyway, so they're not offered as a choice.
    lead_choices = [e for e in emps if e.active and e.is_admin]

    return render(
        request, "admin/roster.html",
        {
            "user": admin, "emps": emps, "show": show, "dept_counts": dept_counts,
            "all_depts": all_depts, "lead_choices": lead_choices,
        },
        db=db,
    )


def _emp_from_form(
    db: Session, emp: m.Employee, name, email, department, designation, target_hours,
    work_days, start_date, active, tracked, role, dob="", phone="", country_code="",
    reports_to_id="", is_developer=False,
):
    emp.name = name.strip()
    emp.country_code = country_code.strip() or None
    emp.phone = phone.strip() or None
    # None (not "") so multiple blank emails never collide on the unique
    # constraint; signup later matches an employee by this exact field.
    new_email = email.strip() or None
    if new_email is not None:
        clash = db.execute(
            select(m.Employee).where(
                m.Employee.email == new_email, m.Employee.id != (emp.id or -1)
            )
        ).scalar_one_or_none()
        if clash is not None:
            raise FormError(f"Email '{new_email}' is already used by {clash.name}.")
    emp.email = new_email
    emp.department = department.strip()
    emp.designation = designation.strip()
    target_minutes = parse_hours_field(target_hours or "8", "Daily target hours")
    if target_minutes <= 0:
        raise FormError("Daily target hours must be greater than zero.")
    emp.daily_target_minutes = target_minutes
    try:
        emp.work_days = (
            ",".join(str(d) for d in sorted({int(x) for x in work_days})) if work_days else "0,1,2,3,4"
        )
    except (ValueError, TypeError):
        raise FormError("Work days must be whole numbers 0-6 (Monday=0).")
    emp.start_date = parse_date_field(start_date, "Start date") if start_date else None
    emp.date_of_birth = parse_date_field(dob, "Date of birth") if dob else None
    emp.active = bool(active)
    emp.tracked = bool(tracked)
    emp.is_admin, emp.is_super_admin = role_to_flags(role)
    # Ticketing System (Ganesh, 2026-08-06) — Developer is a third,
    # independent axis, not tied to role at all (see Employee.is_developer's
    # docstring): a plain employee can be a developer just as much as an
    # admin can, so this isn't part of role_to_flags. While TICKETING_ENABLED
    # is off the checkbox is hidden from both roster forms (see
    # app/templating.py), so the form never submits this field at all —
    # leave the stored value untouched rather than force it to False on
    # every save, which would silently wipe it the instant the flag flips
    # back on.
    if TICKETING_ENABLED:
        emp.is_developer = bool(is_developer)
    # blank/"0" -> no reporting lead set; never allow someone to report to
    # themselves, or to someone who isn't an admin (silently ignored rather
    # than a hard error — a stray/crafted selection shouldn't block saving
    # the rest of the form). The is_admin check guards the same invariant
    # the roster/roster_edit dropdowns already enforce by only listing
    # admins as choices (Ganesh's manager, 2026-08-03: "Reports to" is now
    # also Team Lead authority for Overtime Requests, see app/auth.py
    # led_by() — a non-admin could never act on anything routed to them).
    rid = int(reports_to_id) if str(reports_to_id).strip().isdigit() else None
    if rid and rid != emp.id:
        lead = db.get(m.Employee, rid)
        rid = rid if lead is not None and lead.is_admin else None
    emp.reports_to_id = rid or None


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
    dob: str = Form(""),
    country_code: str = Form(""),
    phone: str = Form(""),
    active: str = Form("1"),
    tracked: str = Form("1"),
    role: str = Form(ROLE_EMPLOYEE),
    reports_to_id: str = Form(""),
    is_developer: str = Form(""),
    admin: m.Employee = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    emp = m.Employee(name=name.strip())
    try:
        _emp_from_form(db, emp, name, email, department, designation, target_hours,
                       work_days, start_date, active == "1", tracked == "1", role,
                       dob, phone, country_code, reports_to_id, is_developer == "1")
    except FormError as e:
        flash(request, e.message, "err")
        return RedirectResponse("/admin/roster", status_code=303)
    emp.employee_code = next_employee_code(db)
    db.add(emp)
    db.commit()
    audit(db, admin.name, "roster_add", "Employee", emp.id, {"name": emp.name})
    flash(request, f"Added {emp.name}.", "ok")
    return RedirectResponse("/admin/roster", status_code=303)


@router.post("/roster/{emp_id}/reset-password")
def roster_reset_password(
    emp_id: int,
    request: Request,
    admin: m.Employee = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    """Clears the stored hash so the employee can re-run /signup. Stand-in
    for a self-service reset flow until there's an email service to send
    reset links from (AUTH_MODE=password only has meaning if there's a
    password to clear)."""
    emp = db.get(m.Employee, emp_id)
    if emp is None:
        return RedirectResponse("/admin/roster", status_code=303)
    emp.password_hash = None
    db.commit()
    audit(db, admin.name, "reset_password", "Employee", emp.id, {"name": emp.name})
    flash(request, f"Password cleared for {emp.name} — they can run Sign up again.", "ok")
    return RedirectResponse(f"/admin/roster/{emp_id}/edit", status_code=303)


@router.get("/roster/{emp_id}/edit")
def roster_edit_page(
    emp_id: int,
    request: Request,
    admin: m.Employee = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    emp = db.get(m.Employee, emp_id)
    if emp is None:
        return RedirectResponse("/admin/roster", status_code=303)
    # is_admin-only — see the matching comment in roster_page's lead_choices.
    lead_choices = list(
        db.execute(
            select(m.Employee)
            .where(m.Employee.active.is_(True), m.Employee.is_admin.is_(True), m.Employee.id != emp_id)
            .order_by(m.Employee.name)
        ).scalars()
    )
    return render(
        request, "admin/roster_edit.html",
        {"user": admin, "emp": emp, "lead_choices": lead_choices}, db=db,
    )


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
    dob: str = Form(""),
    country_code: str = Form(""),
    phone: str = Form(""),
    active: str = Form(""),
    tracked: str = Form(""),
    role: str = Form(ROLE_EMPLOYEE),
    reports_to_id: str = Form(""),
    is_developer: str = Form(""),
    admin: m.Employee = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    emp = db.get(m.Employee, emp_id)
    if emp is None:
        return RedirectResponse("/admin/roster", status_code=303)
    before = {
        "name": emp.name, "department": emp.department, "designation": emp.designation,
        "target": emp.daily_target_minutes, "work_days": emp.work_days,
        "active": emp.active, "tracked": emp.tracked, "is_admin": emp.is_admin,
        "is_super_admin": emp.is_super_admin, "is_developer": emp.is_developer,
    }
    try:
        _emp_from_form(db, emp, name, email, department, designation, target_hours,
                       work_days, start_date, active == "1", tracked == "1", role,
                       dob, phone, country_code, reports_to_id, is_developer == "1")
    except FormError as e:
        flash(request, e.message, "err")
        return RedirectResponse("/admin/roster", status_code=303)
    db.commit()
    audit(db, admin.name, "roster_edit", "Employee", emp.id, {"before": before})
    flash(request, f"Saved {emp.name}." + ("" if emp.active else " (deactivated — history kept, dropped from compliance runs)"), "ok")
    return RedirectResponse("/admin/roster", status_code=303)


# --------------------------------------------------------------------------
# Bulk upload (Roster -> Bulk upload) — parsing rules live in app/bulk_upload.py
# --------------------------------------------------------------------------
@router.get("/roster/bulk-upload")
def roster_bulk_upload_page(
    request: Request,
    admin: m.Employee = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    return render(request, "admin/roster_bulk_upload.html", {"user": admin, "result": None}, db=db)


@router.get("/roster/bulk-upload/sample.xlsx")
def roster_bulk_upload_sample(admin: m.Employee = Depends(require_super_admin)):
    buf = io.BytesIO()
    bulk_upload.build_sample_workbook().save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="employee_upload_template.xlsx"'},
    )


@router.get("/roster/bulk-upload/existing.xlsx")
def roster_bulk_upload_existing(
    admin: m.Employee = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    buf = io.BytesIO()
    bulk_upload.build_existing_employees_workbook(db).save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="existing_employees.xlsx"'},
    )


@router.post("/roster/bulk-upload")
def roster_bulk_upload(
    request: Request,
    file: UploadFile = File(...),
    admin: m.Employee = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm")):
        flash(request, "Please upload an .xlsx file — use the sample template.", "err")
        return RedirectResponse("/admin/roster/bulk-upload", status_code=303)
    try:
        wb = load_workbook(io.BytesIO(file.file.read()), data_only=True)
    except Exception:
        flash(request, "Couldn't read that file — is it a valid, unprotected .xlsx?", "err")
        return RedirectResponse("/admin/roster/bulk-upload", status_code=303)

    result = bulk_upload.process_upload(db, wb)
    if result["header_error"]:
        flash(request, result["header_error"], "err")
        return RedirectResponse("/admin/roster/bulk-upload", status_code=303)
    if result["added"] or result["updated"]:
        audit(db, admin.name, "roster_bulk_upload", "Employee", "",
              {"added": result["added"], "updated": result["updated"],
               "deactivated": result["deactivated"], "skipped": len(result["skipped"])})
    summary = f"{result['added']} employee(s) added, {result['updated']} updated"
    summary += f" ({result['deactivated']} deactivated)." if result["deactivated"] else "."
    if result["skipped"]:
        summary += f" {len(result['skipped'])} row(s) skipped — see details below."
    flash(request, summary, "ok" if (result["added"] or result["updated"]) else "err")
    return render(request, "admin/roster_bulk_upload.html", {"user": admin, "result": result}, db=db)


# --------------------------------------------------------------------------
# Lists (Project/Employer + Task dropdowns)
# --------------------------------------------------------------------------
@router.get("/lists")
def lists_page(
    request: Request,
    admin: m.Employee = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    projects = list(db.execute(select(m.Project).order_by(m.Project.active.desc(), m.Project.name)).scalars())
    tasks = list(db.execute(select(m.TaskType).order_by(m.TaskType.active.desc(), m.TaskType.name)).scalars())
    return render(request, "admin/lists.html", {"user": admin, "projects": projects, "tasks": tasks}, db=db)


@router.post("/lists/add")
def lists_add(
    request: Request,
    kind: str = Form(...),
    name: str = Form(...),
    admin: m.Employee = Depends(require_super_admin),
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


# --------------------------------------------------------------------------
# Bulk upload (Projects & Tasks -> Bulk upload) — one column per sheet,
# add-only; parsing rules live in app/lists_bulk_upload.py
# --------------------------------------------------------------------------
@router.get("/lists/bulk-upload")
def lists_bulk_upload_page(
    request: Request,
    admin: m.Employee = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    return render(request, "admin/lists_bulk_upload.html", {"user": admin, "result": None, "result_kind": None}, db=db)


@router.get("/lists/bulk-upload/sample.xlsx")
def lists_bulk_upload_sample(
    kind: str,
    admin: m.Employee = Depends(require_super_admin),
):
    buf = io.BytesIO()
    lists_bulk_upload.build_sample_workbook(kind).save(buf)
    buf.seek(0)
    filename = "projects_upload_template.xlsx" if kind == "project" else "tasks_upload_template.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/lists/bulk-upload/existing.xlsx")
def lists_bulk_upload_existing(
    kind: str,
    admin: m.Employee = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    buf = io.BytesIO()
    lists_bulk_upload.build_existing_workbook(db, kind).save(buf)
    buf.seek(0)
    filename = "existing_projects.xlsx" if kind == "project" else "existing_tasks.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/lists/bulk-upload")
def lists_bulk_upload_post(
    request: Request,
    kind: str = Form(...),
    file: UploadFile = File(...),
    admin: m.Employee = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    if kind not in ("project", "task"):
        flash(request, "Unknown list — use the Projects or Tasks upload form.", "err")
        return RedirectResponse("/admin/lists/bulk-upload", status_code=303)
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm")):
        flash(request, "Please upload an .xlsx file — use the sample template.", "err")
        return RedirectResponse("/admin/lists/bulk-upload", status_code=303)
    try:
        wb = load_workbook(io.BytesIO(file.file.read()), data_only=True)
    except Exception:
        flash(request, "Couldn't read that file — is it a valid, unprotected .xlsx?", "err")
        return RedirectResponse("/admin/lists/bulk-upload", status_code=303)

    result = lists_bulk_upload.process_upload(db, wb, kind)
    if result["header_error"]:
        flash(request, result["header_error"], "err")
        return RedirectResponse("/admin/lists/bulk-upload", status_code=303)
    if result["added"]:
        audit(db, admin.name, f"lists_bulk_upload_{kind}", kind, "",
              {"added": result["added"], "skipped": len(result["skipped"])})
    label = "project(s)" if kind == "project" else "task(s)"
    summary = f"{result['added']} {label} added."
    if result["skipped"]:
        summary += f" {len(result['skipped'])} row(s) skipped — see details below."
    flash(request, summary, "ok" if result["added"] else "err")
    return render(
        request, "admin/lists_bulk_upload.html",
        {"user": admin, "result": result, "result_kind": kind}, db=db,
    )


@router.post("/lists/{kind}/{item_id}/toggle")
def lists_toggle(
    kind: str,
    item_id: int,
    request: Request,
    admin: m.Employee = Depends(require_super_admin),
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
# Suggestions (Ganesh, 2026-08-01) — employee/lead-suggested Projects/Tasks
# awaiting review. Deliberately require_admin, not require_super_admin,
# same tier as Leave Requests: "Team lead approval gatekeeping" was the
# explicit ask, so a department-scoped admin needs to be able to act on
# their own team's suggestions without needing org-wide access. Scoped by
# the SUBMITTER's department (a suggestion has no department of its own).
# --------------------------------------------------------------------------
def _pending_suggestions(db: Session, admin: m.Employee):
    scope = admin_department_scope(admin)
    projects = list(
        db.execute(
            select(m.Project).where(m.Project.status == m.LIST_PENDING).order_by(m.Project.created_at)
        ).scalars()
    )
    tasks = list(
        db.execute(
            select(m.TaskType).where(m.TaskType.status == m.LIST_PENDING).order_by(m.TaskType.created_at)
        ).scalars()
    )
    if scope is not None:
        projects = [p for p in projects if p.created_by and (p.created_by.department or "—") == scope]
        tasks = [t for t in tasks if t.created_by and (t.created_by.department or "—") == scope]
    return projects, tasks


@router.get("/suggestions")
def suggestions_page(
    request: Request,
    admin: m.Employee = Depends(require_admin),
    db: Session = Depends(get_db),
):
    projects, tasks = _pending_suggestions(db, admin)
    return render(
        request, "admin/suggestions.html",
        {"user": admin, "projects": projects, "tasks": tasks}, db=db,
    )


def _suggestion_or_forbidden(db: Session, admin: m.Employee, kind: str, item_id: int):
    model = m.Project if kind == "project" else m.TaskType
    item = db.get(model, item_id)
    if item is None:
        return None
    scope = admin_department_scope(admin)
    if scope is not None:
        submitter_dept = (item.created_by.department or "—") if item.created_by else None
        if submitter_dept != scope:
            # Not just a UI filter — a department-scoped admin can't approve/
            # reject someone else's team's suggestion even via a direct POST.
            raise Forbidden()
    return item


@router.post("/suggestions/{kind}/{item_id}/approve")
def suggestion_approve(
    kind: str,
    item_id: int,
    request: Request,
    admin: m.Employee = Depends(require_admin),
    db: Session = Depends(get_db),
):
    item = _suggestion_or_forbidden(db, admin, kind, item_id)
    if item is None:
        return RedirectResponse("/admin/suggestions", status_code=303)
    item.status = m.LIST_APPROVED
    item.reviewed_by = admin.name
    item.reviewed_at = dt.datetime.utcnow()
    db.commit()
    audit(db, admin.name, f"suggestion_approve_{kind}", kind, item.name, {})
    flash(request, f"Approved '{item.name}' — now visible to everyone.", "ok")
    return RedirectResponse("/admin/suggestions", status_code=303)


@router.post("/suggestions/{kind}/{item_id}/reject")
def suggestion_reject(
    kind: str,
    item_id: int,
    request: Request,
    review_note: str = Form(""),
    admin: m.Employee = Depends(require_admin),
    db: Session = Depends(get_db),
):
    item = _suggestion_or_forbidden(db, admin, kind, item_id)
    if item is None:
        return RedirectResponse("/admin/suggestions", status_code=303)
    item.status = m.LIST_REJECTED
    # also deactivate: a rejected suggestion must stop being usable even by
    # the person who submitted it (the "usable while pending" carve-out only
    # applies to genuinely pending rows — see validate_entry/
    # _visible_projects_and_tasks in app/routes/employee.py)
    item.active = False
    item.reviewed_by = admin.name
    item.reviewed_at = dt.datetime.utcnow()
    item.review_note = review_note.strip()
    db.commit()
    audit(db, admin.name, f"suggestion_reject_{kind}", kind, item.name, {"reason": review_note.strip()})
    flash(request, f"Rejected '{item.name}'.", "ok")
    return RedirectResponse("/admin/suggestions", status_code=303)


# --------------------------------------------------------------------------
# Project/Task assignment (Ganesh, 2026-08-01) — a team lead's "this
# employee works on this project/task" marker. Deliberately advisory, NOT
# enforced: app/validation.py never rejects a TaskEntry against an
# unassigned project — this only changes what's shown first/highlighted on
# the Today entry form (see app/routes/employee.py
# _visible_projects_and_tasks). Same require_admin + department-scope tier
# as Leave Requests/Suggestions — a team lead manages their own team only.
# --------------------------------------------------------------------------
@router.get("/assignments")
def assignments_page(
    request: Request,
    employee_id: Optional[int] = None,
    admin: m.Employee = Depends(require_admin),
    db: Session = Depends(get_db),
):
    scope = admin_department_scope(admin)
    emps = list(
        db.execute(select(m.Employee).where(m.Employee.active.is_(True)).order_by(m.Employee.name)).scalars()
    )
    if scope is not None:
        emps = [e for e in emps if (e.department or "—") == scope]

    selected = next((e for e in emps if e.id == employee_id), None) if employee_id is not None else None

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

    assigned_project_ids, assigned_task_ids = set(), set()
    if selected is not None:
        assigned_project_ids = {
            row[0] for row in db.execute(
                select(m.ProjectAssignment.project_id).where(m.ProjectAssignment.employee_id == selected.id)
            ).all()
        }
        assigned_task_ids = {
            row[0] for row in db.execute(
                select(m.TaskAssignment.task_type_id).where(m.TaskAssignment.employee_id == selected.id)
            ).all()
        }

    return render(
        request, "admin/assignments.html",
        {
            "user": admin, "emps": emps, "selected": selected, "projects": projects, "tasks": tasks,
            "assigned_project_ids": assigned_project_ids, "assigned_task_ids": assigned_task_ids,
        },
        db=db,
    )


@router.post("/assignments/{employee_id}")
def assignments_save(
    employee_id: int,
    request: Request,
    project_ids: list = Form([]),
    task_type_ids: list = Form([]),
    admin: m.Employee = Depends(require_admin),
    db: Session = Depends(get_db),
):
    emp = db.get(m.Employee, employee_id)
    if emp is None:
        return RedirectResponse("/admin/assignments", status_code=303)
    scope = admin_department_scope(admin)
    if scope is not None and (emp.department or "—") != scope:
        raise Forbidden()

    # replace-all: simplest correct way to sync a set of checkboxes without
    # tracking individual add/remove diffs
    db.execute(delete(m.ProjectAssignment).where(m.ProjectAssignment.employee_id == employee_id))
    db.execute(delete(m.TaskAssignment).where(m.TaskAssignment.employee_id == employee_id))
    for pid in project_ids:
        db.add(m.ProjectAssignment(employee_id=employee_id, project_id=int(pid), assigned_by=admin.name))
    for tid in task_type_ids:
        db.add(m.TaskAssignment(employee_id=employee_id, task_type_id=int(tid), assigned_by=admin.name))
    db.commit()
    audit(db, admin.name, "assignments_save", "Employee", employee_id,
          {"projects": len(project_ids), "tasks": len(task_type_ids)})
    flash(request, f"Saved assignments for {emp.name}.", "ok")
    return RedirectResponse(f"/admin/assignments?employee_id={employee_id}", status_code=303)


# --------------------------------------------------------------------------
# Leave entry (admin-entered in POC — open question 5)
# --------------------------------------------------------------------------
@router.get("/leave")
def leave_page(
    request: Request,
    admin: m.Employee = Depends(require_admin),
    db: Session = Depends(get_db),
):
    # Department-scoped admin (team lead) — Leave Requests is one of the
    # three screens they keep (see Employee.is_super_admin docstring), but
    # only for their own team: the employee picker, pending queue and
    # approved history are all filtered to it. LeaveRecord has no
    # department column of its own, so this filters by employee_id
    # membership in the scoped roster instead.
    scope = admin_department_scope(admin)
    emps = list(
        db.execute(
            select(m.Employee).where(m.Employee.active.is_(True)).order_by(m.Employee.name)
        ).scalars()
    )
    if scope is not None:
        emps = [e for e in emps if (e.department or "—") == scope]
    scoped_ids = {e.id for e in emps} if scope is not None else None

    pending = list(
        db.execute(
            select(m.LeaveRecord)
            .where(m.LeaveRecord.status == m.LEAVE_REQUESTED)
            .order_by(m.LeaveRecord.created_at)
        ).scalars()
    )
    approved_q = (
        select(m.LeaveRecord)
        .where(m.LeaveRecord.status == m.LEAVE_APPROVED)
        .order_by(m.LeaveRecord.created_at.desc())
    )
    if scoped_ids is None:
        approved_q = approved_q.limit(100)
    approved = list(db.execute(approved_q).scalars())
    if scoped_ids is not None:
        # filter *before* trimming to 100 — otherwise a small team's older
        # approved leave could get pushed out by other departments' more
        # recent rows before the scoping filter ever sees them
        pending = [lv for lv in pending if lv.employee_id in scoped_ids]
        approved = [lv for lv in approved if lv.employee_id in scoped_ids][:100]
    balances = {e.id: engine.leave_balance(db, e) for e in emps}
    return render(
        request, "admin/leave.html",
        {
            "user": admin, "emps": emps, "pending": pending, "approved": approved,
            "leave_types": m.LEAVE_TYPES, "balances": balances, "balance_year": today_local().year,
        },
        db=db,
    )


# --------------------------------------------------------------------------
# Bulk leave-allocation assignment (Leave -> Bulk assign leaves) — parsing
# rules live in app/leave_bulk_upload.py
# --------------------------------------------------------------------------
@router.get("/leave/bulk-upload")
def leave_bulk_upload_page(
    request: Request,
    admin: m.Employee = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    return render(request, "admin/leave_bulk_upload.html", {"user": admin, "result": None}, db=db)


@router.get("/leave/bulk-upload/sample.xlsx")
def leave_bulk_upload_sample(admin: m.Employee = Depends(require_super_admin)):
    buf = io.BytesIO()
    leave_bulk_upload.build_sample_workbook().save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="leave_allocation_template.xlsx"'},
    )


@router.get("/leave/bulk-upload/existing.xlsx")
def leave_bulk_upload_existing(
    admin: m.Employee = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    buf = io.BytesIO()
    leave_bulk_upload.build_existing_allocations_workbook(db).save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="existing_leave_allocations.xlsx"'},
    )


@router.post("/leave/bulk-upload")
def leave_bulk_upload_post(
    request: Request,
    file: UploadFile = File(...),
    admin: m.Employee = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm")):
        flash(request, "Please upload an .xlsx file — use the sample template.", "err")
        return RedirectResponse("/admin/leave/bulk-upload", status_code=303)
    try:
        wb = load_workbook(io.BytesIO(file.file.read()), data_only=True)
    except Exception:
        flash(request, "Couldn't read that file — is it a valid, unprotected .xlsx?", "err")
        return RedirectResponse("/admin/leave/bulk-upload", status_code=303)

    result = leave_bulk_upload.process_upload(db, wb)
    if result["header_error"]:
        flash(request, result["header_error"], "err")
        return RedirectResponse("/admin/leave/bulk-upload", status_code=303)
    if result["updated"]:
        audit(db, admin.name, "leave_bulk_upload", "Employee", "",
              {"updated": result["updated"], "skipped": len(result["skipped"])})
    summary = f"{result['updated']} employee(s) had leave allocations updated."
    if result["skipped"]:
        summary += f" {len(result['skipped'])} row(s) skipped — see details below."
    flash(request, summary, "ok" if result["updated"] else "err")
    return render(request, "admin/leave_bulk_upload.html", {"user": admin, "result": result}, db=db)


@router.post("/leave/{leave_id}/approve")
def leave_approve(
    leave_id: int,
    request: Request,
    review_note: str = Form(""),
    admin: m.Employee = Depends(require_admin),
    db: Session = Depends(get_db),
):
    lv = db.get(m.LeaveRecord, leave_id)
    if lv is None:
        return RedirectResponse("/admin/leave", status_code=303)
    emp = db.get(m.Employee, lv.employee_id)
    scope = admin_department_scope(admin)
    if scope is not None and emp is not None and (emp.department or "—") != scope:
        # Not just a UI filter — block a department-scoped admin from
        # approving/rejecting leave for someone outside their team even if
        # they craft the POST directly.
        raise Forbidden()
    lv.status = m.LEAVE_APPROVED
    lv.reviewed_by = admin.name
    lv.reviewed_at = dt.datetime.utcnow()
    lv.review_note = review_note.strip()
    db.commit()
    audit(db, admin.name, "leave_approve", "LeaveRecord", lv.id,
          {"employee": emp.name if emp else lv.employee_id, "range": f"{lv.start_date}..{lv.end_date}"})
    if emp is not None:
        engine.recompute_employee(db, emp, lv.start_date, min(lv.end_date, today_local()))
    flash(request, f"Approved leave for {emp.name if emp else lv.employee_id}.", "ok")
    return RedirectResponse("/admin/leave", status_code=303)


@router.post("/leave/{leave_id}/reject")
def leave_reject(
    leave_id: int,
    request: Request,
    review_note: str = Form(""),
    admin: m.Employee = Depends(require_admin),
    db: Session = Depends(get_db),
):
    lv = db.get(m.LeaveRecord, leave_id)
    if lv is None:
        return RedirectResponse("/admin/leave", status_code=303)
    emp = db.get(m.Employee, lv.employee_id)
    scope = admin_department_scope(admin)
    if scope is not None and emp is not None and (emp.department or "—") != scope:
        raise Forbidden()
    lv.status = m.LEAVE_REJECTED
    lv.reviewed_by = admin.name
    lv.reviewed_at = dt.datetime.utcnow()
    lv.review_note = review_note.strip()
    db.commit()
    audit(db, admin.name, "leave_reject", "LeaveRecord", lv.id,
          {"employee": emp.name if emp else lv.employee_id, "reason": review_note.strip()})
    if emp is not None:
        engine.recompute_employee(db, emp, lv.start_date, min(lv.end_date, today_local()))
    flash(request, f"Rejected leave request for {emp.name if emp else lv.employee_id}.", "ok")
    return RedirectResponse("/admin/leave", status_code=303)


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
    scope = admin_department_scope(admin)
    if scope is not None and (emp.department or "—") != scope:
        raise Forbidden()
    try:
        start = parse_date_field(start_date, "Start date")
        end = parse_date_field(end_date, "End date") if end_date else start
    except FormError as e:
        flash(request, e.message, "err")
        return RedirectResponse("/admin/leave", status_code=303)
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
        status=m.LEAVE_APPROVED,  # admin direct-entry is already-approved by definition
    )
    db.add(lv)
    db.commit()
    audit(db, admin.name, "leave_add", "LeaveRecord", lv.id,
          {"employee": emp.name, "range": f"{start}..{end}", "type": type, "minutes": minutes})
    engine.recompute_employee(db, emp, start, min(end, today_local()))
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
        scope = admin_department_scope(admin)
        if scope is not None and emp is not None and (emp.department or "—") != scope:
            raise Forbidden()
        db.delete(lv)
        db.commit()
        audit(db, admin.name, "leave_delete", "LeaveRecord", leave_id,
              {"employee": emp.name if emp else lv.employee_id})
        if emp is not None:
            engine.recompute_employee(db, emp, lv.start_date, min(lv.end_date, today_local()))
    return RedirectResponse("/admin/leave", status_code=303)


# --------------------------------------------------------------------------
# Overtime Requests (Ganesh's manager, 2026-08-03) — same submit -> queue ->
# act shape as Leave Requests just above, but scoped per-person via
# app.auth.led_by() (Team Lead = whoever an employee's reports_to is, IF
# that person is_admin) instead of by department. Approving/rejecting
# doesn't touch engine.py/DayStatus/strikes at all — see OvertimeApproval's
# model docstring — so nothing here needs an engine.recompute_employee call
# the way Leave's approve/reject/add/delete do above.
# --------------------------------------------------------------------------
@router.get("/overtime")
def overtime_page(
    request: Request,
    admin: m.Employee = Depends(require_admin),
    db: Session = Depends(get_db),
):
    scope = led_by(admin, db)  # None => Super Admin, sees/can act on everyone
    emps = list(
        db.execute(
            select(m.Employee).where(m.Employee.active.is_(True)).order_by(m.Employee.name)
        ).scalars()
    )
    if scope is not None:
        emps = [e for e in emps if e.id in scope]

    pending = list(
        db.execute(
            select(m.OvertimeApproval)
            .where(m.OvertimeApproval.status == m.OT_REQUESTED)
            .order_by(m.OvertimeApproval.created_at)
        ).scalars()
    )
    approved_q = (
        select(m.OvertimeApproval)
        .where(m.OvertimeApproval.status == m.OT_APPROVED)
        .order_by(m.OvertimeApproval.created_at.desc())
    )
    if scope is None:
        approved_q = approved_q.limit(100)
    approved = list(db.execute(approved_q).scalars())
    if scope is not None:
        # filter *before* trimming to 100 — same reasoning as Leave's
        # leave_page above: a small team's older approvals could otherwise
        # get pushed out by other leads' more recent rows first.
        pending = [ot for ot in pending if ot.employee_id in scope]
        approved = [ot for ot in approved if ot.employee_id in scope][:100]

    return render(
        request, "admin/overtime.html",
        {"user": admin, "emps": emps, "pending": pending, "approved": approved},
        db=db,
    )


def _ot_forbidden_unless_scoped(admin: m.Employee, ot: m.OvertimeApproval, db: Session) -> None:
    scope = led_by(admin, db)
    if scope is not None and ot.employee_id not in scope:
        # Not just a UI filter — block a Lead from approving/rejecting
        # overtime for someone who isn't their direct report even if they
        # craft the POST directly, same defense-in-depth as Leave's checks.
        raise Forbidden()


@router.post("/overtime/{ot_id}/approve")
def overtime_approve(
    ot_id: int,
    request: Request,
    review_note: str = Form(""),
    admin: m.Employee = Depends(require_admin),
    db: Session = Depends(get_db),
):
    ot = db.get(m.OvertimeApproval, ot_id)
    if ot is None:
        return RedirectResponse("/admin/overtime", status_code=303)
    _ot_forbidden_unless_scoped(admin, ot, db)
    emp = db.get(m.Employee, ot.employee_id)
    ot.status = m.OT_APPROVED
    ot.reviewed_by = admin.name
    ot.reviewed_at = dt.datetime.utcnow()
    ot.review_note = review_note.strip()
    db.commit()
    audit(db, admin.name, "overtime_approve", "OvertimeApproval", ot.id,
          {"employee": emp.name if emp else ot.employee_id, "range": f"{ot.start_date}..{ot.end_date}"})
    flash(request, f"Approved overtime for {emp.name if emp else ot.employee_id}.", "ok")
    return RedirectResponse("/admin/overtime", status_code=303)


@router.post("/overtime/{ot_id}/reject")
def overtime_reject(
    ot_id: int,
    request: Request,
    review_note: str = Form(""),
    admin: m.Employee = Depends(require_admin),
    db: Session = Depends(get_db),
):
    ot = db.get(m.OvertimeApproval, ot_id)
    if ot is None:
        return RedirectResponse("/admin/overtime", status_code=303)
    _ot_forbidden_unless_scoped(admin, ot, db)
    emp = db.get(m.Employee, ot.employee_id)
    ot.status = m.OT_REJECTED
    ot.reviewed_by = admin.name
    ot.reviewed_at = dt.datetime.utcnow()
    ot.review_note = review_note.strip()
    db.commit()
    audit(db, admin.name, "overtime_reject", "OvertimeApproval", ot.id,
          {"employee": emp.name if emp else ot.employee_id, "reason": review_note.strip()})
    flash(request, f"Rejected overtime request for {emp.name if emp else ot.employee_id}.", "ok")
    return RedirectResponse("/admin/overtime", status_code=303)


@router.post("/overtime/grant")
def overtime_grant(
    request: Request,
    employee_id: int = Form(...),
    start_date: str = Form(...),
    end_date: str = Form(""),
    note: str = Form(""),
    admin: m.Employee = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """A Lead/Admin approving overtime *without* a preceding employee
    request — covers both granting it proactively ahead of a known busy
    period, and reviewing after the fact (start/end can be in the past;
    nothing here cares which direction time runs, see model docstring)."""
    emp = db.get(m.Employee, employee_id)
    if emp is None:
        return RedirectResponse("/admin/overtime", status_code=303)
    scope = led_by(admin, db)
    if scope is not None and employee_id not in scope:
        raise Forbidden()
    try:
        start = parse_date_field(start_date, "Start date")
        end = parse_date_field(end_date, "End date") if end_date else start
    except FormError as e:
        flash(request, e.message, "err")
        return RedirectResponse("/admin/overtime", status_code=303)
    if end < start:
        flash(request, "End date is before start date.", "err")
        return RedirectResponse("/admin/overtime", status_code=303)
    ot = m.OvertimeApproval(
        employee_id=employee_id, start_date=start, end_date=end, note=note.strip(),
        requested_by=admin.name,
        status=m.OT_APPROVED,  # lead/admin direct-entry is already-approved by definition
        reviewed_by=admin.name, reviewed_at=dt.datetime.utcnow(),
    )
    db.add(ot)
    db.commit()
    audit(db, admin.name, "overtime_grant", "OvertimeApproval", ot.id,
          {"employee": emp.name, "range": f"{start}..{end}"})
    flash(request, f"Overtime approved for {emp.name}.", "ok")
    return RedirectResponse("/admin/overtime", status_code=303)


@router.post("/overtime/{ot_id}/delete")
def overtime_delete(
    ot_id: int,
    request: Request,
    admin: m.Employee = Depends(require_admin),
    db: Session = Depends(get_db),
):
    ot = db.get(m.OvertimeApproval, ot_id)
    if ot is not None:
        _ot_forbidden_unless_scoped(admin, ot, db)
        emp = db.get(m.Employee, ot.employee_id)
        db.delete(ot)
        db.commit()
        audit(db, admin.name, "overtime_delete", "OvertimeApproval", ot_id,
              {"employee": emp.name if emp else ot.employee_id})
    return RedirectResponse("/admin/overtime", status_code=303)


# --------------------------------------------------------------------------
# Config (PRD §10 — every open question is a dial here) + holidays
# --------------------------------------------------------------------------
@router.get("/config")
def config_page(
    request: Request,
    admin: m.Employee = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    cfg = engine.get_config(db)
    holidays = list(db.execute(select(m.Holiday).order_by(m.Holiday.date)).scalars())
    return render(request, "admin/config.html", {"user": admin, "cfg": cfg, "holidays": holidays}, db=db)


@router.post("/config")
def config_save(
    request: Request,
    tolerance_hours: str = Form("1.0"),
    strike_threshold: str = Form("5"),
    max_row_hours: str = Form("4"),
    backdate_working_days: str = Form("1"),
    gap_flag_minutes: str = Form("15"),
    min_details_chars: str = Form("5"),
    max_break_minutes: str = Form("30"),
    comp_erases_strike: str = Form(""),
    live_start_date: str = Form(""),
    admin: m.Employee = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    before = engine.get_config(db)
    try:
        values = {
            "tolerance_minutes": str(parse_hours_field(tolerance_hours, "Tolerance")),
            "strike_threshold": str(parse_int_field(strike_threshold, "Strike threshold")),
            "max_row_minutes": str(parse_hours_field(max_row_hours, "Max row length")),
            "backdate_working_days": str(parse_int_field(backdate_working_days, "Backdate window")),
            "gap_flag_minutes": str(parse_int_field(gap_flag_minutes, "Gap flag minutes")),
            "min_details_chars": str(parse_int_field(min_details_chars, "Minimum details length")),
            "max_break_minutes": str(parse_int_field(max_break_minutes, "Break allowance")),
            "comp_erases_strike": "1" if comp_erases_strike == "1" else "0",
            "live_start_date": live_start_date.strip(),
        }
    except FormError as e:
        flash(request, e.message, "err")
        return RedirectResponse("/admin/config", status_code=303)
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
    admin: m.Employee = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    try:
        d = parse_date_field(date)
    except FormError as e:
        flash(request, e.message, "err")
        return RedirectResponse("/admin/config", status_code=303)
    if db.execute(select(m.Holiday).where(m.Holiday.date == d)).scalar_one_or_none() is None:
        db.add(m.Holiday(date=d, name=name.strip()))
        db.commit()
        audit(db, admin.name, "holiday_add", "Holiday", date, {"name": name.strip()})
    return RedirectResponse("/admin/config", status_code=303)


@router.post("/holidays/{holiday_id}/delete")
def holiday_delete(
    holiday_id: int,
    request: Request,
    admin: m.Employee = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    h = db.get(m.Holiday, holiday_id)
    if h is not None:
        db.delete(h)
        db.commit()
        audit(db, admin.name, "holiday_delete", "Holiday", h.date.isoformat(), {})
    return RedirectResponse("/admin/config", status_code=303)


# --------------------------------------------------------------------------
# Support inbox (employee-submitted questions from /support)
# --------------------------------------------------------------------------
@router.get("/support")
def support_inbox(
    request: Request,
    status: str = "open",
    admin: m.Employee = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    q = select(m.SupportQuery).order_by(m.SupportQuery.created_at.desc())
    rows = list(db.execute(q).scalars())
    if status == "open":
        rows = [r for r in rows if r.status == m.SUPPORT_OPEN]
    return render(
        request, "admin/support.html",
        {"user": admin, "rows": rows, "status": status}, db=db,
    )


@router.post("/support/{query_id}/resolve")
def support_resolve(
    query_id: int,
    request: Request,
    reply: str = Form(""),
    admin: m.Employee = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    q = db.get(m.SupportQuery, query_id)
    if q is None:
        return RedirectResponse("/admin/support", status_code=303)
    q.status = m.SUPPORT_RESOLVED
    q.admin_reply = reply.strip()
    q.resolved_by = admin.name
    q.resolved_at = dt.datetime.utcnow()
    db.commit()
    audit(db, admin.name, "support_resolved", "SupportQuery", q.id, {"reply": reply.strip()[:200]})
    flash(request, "Marked resolved.", "ok")
    return RedirectResponse("/admin/support", status_code=303)


# --------------------------------------------------------------------------
# Audit log
# --------------------------------------------------------------------------
@router.get("/audit")
def audit_page(
    request: Request,
    admin: m.Employee = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    rows = list(
        db.execute(select(m.AuditLog).order_by(m.AuditLog.at.desc()).limit(300)).scalars()
    )
    return render(request, "admin/audit.html", {"user": admin, "rows": rows}, db=db)
