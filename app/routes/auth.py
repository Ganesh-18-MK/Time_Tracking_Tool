"""Login/logout/signup.

AUTH_MODE=dev: pick a user, no passwords (local dev only) — admin and
employee buttons are shown separately for clarity but nothing checks a
password.
AUTH_MODE=password: two distinct portals instead of one generic form —
Employee Login (sign in + signup link) and Admin Login (sign in only,
rejects any account that isn't flagged admin in the roster, and never says
*why* a login failed so it can't be used to fish for which emails have
admin access). Who's an admin is entirely driven by Employee.is_admin,
which admins set in Roster — that's "the list" this gates against.
"""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import models as m
from app import rate_limit
from app.auth import AUTH_MODE, authenticate, current_user, find_by_email, login_choices
from app.db import get_db
from app.security import hash_password
from app.templating import flash, render

MIN_PASSWORD_LEN = 8

router = APIRouter()


def _lockout_message(wait_seconds: int) -> str:
    minutes = max(1, (wait_seconds + 59) // 60)
    return f"Too many failed attempts. Try again in {minutes} minute{'s' if minutes != 1 else ''}."


@router.get("/")
def home(request: Request, user: m.Employee = Depends(current_user)):
    return RedirectResponse("/admin" if user.is_admin else "/today", status_code=303)


@router.get("/login")
def login_page(request: Request, db: Session = Depends(get_db)):
    if AUTH_MODE == "dev":
        admins, employees = login_choices(db)
        return render(
            request, "login.html",
            {"admins": admins, "employees": employees, "auth_mode": AUTH_MODE},
        )
    return render(request, "login.html", {"auth_mode": AUTH_MODE})


# --------------------------------------------------------------------------
# Employee portal — registered before /login/{employee_id} below so a
# literal "/login/employee" is never mis-matched against that dev-only
# route (which would otherwise try to parse "employee" as an int and 422).
# --------------------------------------------------------------------------
@router.get("/login/employee")
def login_employee_page(request: Request):
    if AUTH_MODE == "dev":
        return RedirectResponse("/login", status_code=303)
    return render(request, "login_employee.html", {})


@router.post("/login/employee")
def login_employee(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    if AUTH_MODE == "dev":
        return RedirectResponse("/login", status_code=303)
    wait = rate_limit.seconds_until_unlock("login", email)
    if wait:
        flash(request, _lockout_message(wait), "err")
        return RedirectResponse("/login/employee", status_code=303)
    emp = authenticate(db, email, password)
    if emp is None:
        rate_limit.record_failure("login", email)
        flash(request, "Email or password is incorrect.", "err")
        return RedirectResponse("/login/employee", status_code=303)
    if emp.is_admin:
        flash(request, "This is an admin account — please use Admin Login instead.", "err")
        return RedirectResponse("/login", status_code=303)
    rate_limit.clear("login", email)
    request.session["employee_id"] = emp.id
    return RedirectResponse("/", status_code=303)


# --------------------------------------------------------------------------
# Admin portal — sign-in only (no self-signup link shown; admins are set up
# by whoever configures the roster). Always the same generic error.
# --------------------------------------------------------------------------
@router.get("/login/admin")
def login_admin_page(request: Request):
    if AUTH_MODE == "dev":
        return RedirectResponse("/login", status_code=303)
    return render(request, "login_admin.html", {})


@router.post("/login/admin")
def login_admin(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    if AUTH_MODE == "dev":
        return RedirectResponse("/login", status_code=303)
    wait = rate_limit.seconds_until_unlock("login", email)
    if wait:
        flash(request, _lockout_message(wait), "err")
        return RedirectResponse("/login/admin", status_code=303)
    emp = authenticate(db, email, password)
    if emp is None or not emp.is_admin:
        # deliberately identical whether the email doesn't exist, the
        # password is wrong, or it's a perfectly valid employee account —
        # never confirm or deny which emails have admin access. Still
        # counts as a failure for lockout purposes either way, so the
        # lockout itself can't be used to infer admin status either.
        rate_limit.record_failure("login", email)
        flash(request, "Access denied — this account doesn't have admin access.", "err")
        return RedirectResponse("/login/admin", status_code=303)
    rate_limit.clear("login", email)
    request.session["employee_id"] = emp.id
    return RedirectResponse("/", status_code=303)


@router.post("/login/{employee_id}")
def login_dev(employee_id: int, request: Request, db: Session = Depends(get_db)):
    # Dev-only shortcut. Gated so it can never become a password-mode
    # auth bypass if AUTH_MODE flips in an environment that still has this
    # route wired up.
    if AUTH_MODE != "dev":
        return RedirectResponse("/login", status_code=303)
    emp = db.get(m.Employee, employee_id)
    if emp is None or not emp.active:
        return RedirectResponse("/login", status_code=303)
    request.session["employee_id"] = emp.id
    return RedirectResponse("/", status_code=303)


@router.get("/signup")
def signup_page(request: Request):
    return render(request, "signup.html", {"auth_mode": AUTH_MODE})


@router.post("/signup")
def signup(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    confirm: str = Form(...),
    db: Session = Depends(get_db),
):
    wait = rate_limit.seconds_until_unlock("signup", email)
    if wait:
        flash(request, _lockout_message(wait), "err")
        return RedirectResponse("/signup", status_code=303)
    emp = find_by_email(db, email)
    if emp is None:
        # counted against the lockout too — repeated "not on roster"
        # attempts are exactly how someone would fish for which emails
        # exist in the roster, so slow that down the same as a bad password.
        rate_limit.record_failure("signup", email)
        flash(
            request,
            "That email isn't on the roster yet — ask an admin to add you before signing up.",
            "err",
        )
        return RedirectResponse("/signup", status_code=303)
    if not emp.active:
        flash(request, "This account is deactivated. Contact an admin.", "err")
        return RedirectResponse("/signup", status_code=303)
    if emp.password_hash:
        flash(request, "This account already has a password — use Sign in, or ask an admin to reset it.", "err")
        return RedirectResponse("/login", status_code=303)
    if len(password) < MIN_PASSWORD_LEN:
        flash(request, f"Password must be at least {MIN_PASSWORD_LEN} characters.", "err")
        return RedirectResponse("/signup", status_code=303)
    if password != confirm:
        flash(request, "Passwords don't match.", "err")
        return RedirectResponse("/signup", status_code=303)
    emp.password_hash = hash_password(password)
    db.commit()
    flash(request, "Account set up — sign in below.", "ok")
    return RedirectResponse("/login", status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
