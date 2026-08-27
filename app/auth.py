"""Authentication.

Three modes via AUTH_MODE:
  * dev (default)  — pick-a-user login screen, no passwords. Local dev only.
  * password        — employee self-signup + email/password login (see
                       app/security.py, app/routes/auth.py /signup, /login).
  * entra           — not implemented yet; the long-term target per PRD §9
                       (MSAL OAuth mapping the tenant user's email to this
                       Employee row). Swap it in here; session/role gating
                       below stays identical regardless of mode.
"""
import os
from typing import Optional

from fastapi import Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import models as m
from app.db import get_db
from app.security import verify_password

AUTH_MODE = os.environ.get("AUTH_MODE", "dev")


class RequiresLogin(Exception):
    pass


class Forbidden(Exception):
    pass


def current_user(request: Request, db: Session = Depends(get_db)) -> m.Employee:
    emp_id = request.session.get("employee_id")
    if not emp_id:
        raise RequiresLogin()
    emp = db.get(m.Employee, emp_id)
    if emp is None or not emp.active:
        request.session.clear()
        raise RequiresLogin()
    return emp


def require_admin(user: m.Employee = Depends(current_user)) -> m.Employee:
    if not user.is_admin:
        raise Forbidden()
    return user


def require_super_admin(admin: m.Employee = Depends(require_admin)) -> m.Employee:
    """Gate for admin screens that stay org-wide only.

    Narrowed to this exact boundary (Ganesh, 2026-08-28): a department-
    scoped admin (Team Lead, is_admin=True, is_super_admin=False) was
    previously able to reach every admin screen; they're now restricted
    to exactly 5 capabilities — add Project/Task names (form + bulk
    upload), assign Projects/Tasks to their team, approve suggested
    Project/Task names from their team, view task logs for their team
    (Person Detail, read-only), and view Time/Project/Strikes/Attendance
    reports for their team. Everything else stays super-admin-only:
    Roster, Settings, Audit Log, Support Inbox, Leave Management,
    Overtime Management, Projects & Tasks list-maintenance actions
    (Deactivate/Reactivate/Rename — but NOT the Add form or bulk upload,
    which a department admin does keep), and the person-detail actions
    that change history (override/unlock/reject-unlock-request/
    compensation-link add-delete). A department-scoped admin gets
    Forbidden() here same as a non-admin does from require_admin — see
    Employee.is_super_admin's docstring for the tier split."""
    if not admin.is_super_admin:
        raise Forbidden()
    return admin


def require_developer(user: m.Employee = Depends(current_user)) -> m.Employee:
    """Gate for the one Ticketing System action that isn't open to every
    logged-in user: changing a ticket's status (Ganesh, 2026-08-06: "only
    developers can change the status"). Deliberately Depends(current_user)
    rather than Depends(require_admin) — is_developer is a THIRD axis, fully
    independent of is_admin/is_super_admin (see Employee.is_developer's
    docstring) — a plain employee can be a developer, and an admin need not
    be one."""
    if not user.is_developer:
        raise Forbidden()
    return user


def require_developer_or_admin(user: m.Employee = Depends(current_user)) -> m.Employee:
    """Gate for the Developer Usage Report (Ganesh, 2026-08-21, "as a
    developer I want to know how many people are using what option") —
    open to either axis independently: a plain-employee Developer who
    isn't an admin, or an admin who isn't flagged as a Developer (Ganesh
    himself, most likely). Neither require_admin nor require_developer
    alone covers both cases; this is deliberately its own dependency
    rather than nesting one inside the other, so it doesn't accidentally
    inherit require_admin's department-scoping assumptions — this report
    is org-wide, same as Audit Logs, not department-scoped like the other
    three Reports pages."""
    if not (user.is_developer or user.is_admin):
        raise Forbidden()
    return user


def admin_department_scope(admin: m.Employee) -> Optional[str]:
    """None => no restriction (super admin sees every department).
    Otherwise the exact department string a department-scoped admin is
    limited to on Dashboard/Leave Requests/Reports — matches the "—"
    fallback used everywhere else for a blank Employee.department."""
    return None if admin.is_super_admin else (admin.department or "—")


def led_by(admin: m.Employee, db: Session) -> Optional[set]:
    """Same None-means-unscoped convention as admin_department_scope, but
    for Overtime Requests — scoped per-person via Employee.reports_to_id
    (Ganesh's manager, 2026-08-03: a Team Lead approves for specific people,
    not a whole department) instead of by department string.

    None => no restriction (Super Admin sees/can act on every request,
    including ones from employees with no admin reports_to assigned —
    that's the "unassigned employees route to Super Admin" fallback the
    manager asked for: it isn't special-cased anywhere, it just falls out
    of Super Admin already being unscoped here exactly like it is for
    admin_department_scope).

    Otherwise, the set of employee_ids who report directly to this admin —
    a department-scoped admin who happens to be someone's Team Lead only
    sees/acts on that specific person's requests, not their whole
    department (Team Lead and "department admin" are independent axes:
    being a dept-scoped admin doesn't make you every department member's
    lead, only an explicit reports_to assignment does)."""
    if admin.is_super_admin:
        return None
    return {
        e.id for e in db.execute(
            select(m.Employee).where(m.Employee.reports_to_id == admin.id)
        ).scalars()
    }


def login_choices(db: Session):
    emps = list(
        db.execute(
            select(m.Employee).where(m.Employee.active.is_(True)).order_by(m.Employee.name)
        ).scalars()
    )
    return [e for e in emps if e.is_admin], [e for e in emps if not e.is_admin]


def find_by_email(db: Session, email: str) -> Optional[m.Employee]:
    """Case-insensitive lookup — used by both /signup (claim a roster row)
    and /login (password mode)."""
    email = (email or "").strip()
    if not email:
        return None
    return db.execute(
        select(m.Employee).where(func.lower(m.Employee.email) == email.lower())
    ).scalar_one_or_none()


def authenticate(db: Session, email: str, password: str) -> Optional[m.Employee]:
    """AUTH_MODE=password login check. Returns None on any failure — wrong
    email, inactive account, no password set yet, or wrong password — and
    deliberately doesn't distinguish which, so failed logins can't be used
    to enumerate valid employee emails."""
    emp = find_by_email(db, email)
    if emp is None or not emp.active or not emp.password_hash:
        return None
    if not verify_password(password, emp.password_hash):
        return None
    return emp
