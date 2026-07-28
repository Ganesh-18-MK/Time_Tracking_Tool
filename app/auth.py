"""Authentication.

POC runs AUTH_MODE=dev: a pick-a-user login screen with no passwords, so the
app is demoable immediately. The production path per PRD §9 is Entra ID —
swap `dev` for an MSAL OAuth flow that maps the tenant user's email to the
Employee row; the session/role gating below stays identical. See README.
"""
import os

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models as m
from app.db import get_db

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
