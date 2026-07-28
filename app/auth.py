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
