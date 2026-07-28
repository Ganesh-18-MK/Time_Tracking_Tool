"""Login/logout. Dev mode: pick a user (no passwords). See app/auth.py."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import models as m
from app.auth import AUTH_MODE, current_user, login_choices
from app.db import get_db
from app.templating import render

router = APIRouter()


@router.get("/")
def home(request: Request, user: m.Employee = Depends(current_user)):
    return RedirectResponse("/admin" if user.is_admin else "/today", status_code=303)


@router.get("/login")
def login_page(request: Request, db: Session = Depends(get_db)):
    admins, employees = login_choices(db)
    return render(
        request,
        "login.html",
        {"admins": admins, "employees": employees, "auth_mode": AUTH_MODE},
    )


@router.post("/login/{employee_id}")
def login(employee_id: int, request: Request, db: Session = Depends(get_db)):
    emp = db.get(m.Employee, employee_id)
    if emp is None or not emp.active:
        return RedirectResponse("/login", status_code=303)
    request.session["employee_id"] = emp.id
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
