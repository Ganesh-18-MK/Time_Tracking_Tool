"""Shared Jinja2 environment with app filters."""
import inspect
import json
import os
import time

from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy import func, select

from app import models as m
from app.auth import led_by
from app.util import (
    STATUS_LABELS,
    STATUS_NAMES,
    fmt_date,
    fmt_datetime,
    fmt_hm,
    fmt_hm_signed,
    fmt_hours,
    fmt_time,
    mask_tail,
    month_label,
)

templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
)
templates.env.filters["hm"] = fmt_hm
templates.env.filters["hm_signed"] = fmt_hm_signed
templates.env.filters["hours"] = fmt_hours
templates.env.filters["clock"] = fmt_time
templates.env.filters["mask"] = mask_tail
# MM/DD/YYYY normalization (manager request, 2026-08-03) — every
# human-readable date/timestamp on screen goes through one of these two
# filters now instead of ad-hoc strftime() calls scattered across templates.
templates.env.filters["mdy"] = fmt_date
templates.env.filters["mdy_dt"] = fmt_datetime


def tojson_filter(value) -> Markup:
    """Dump a plain value (list/dict of str/int/etc — not ORM objects) as
    JSON safe to inline inside a <script> tag. Escapes </script>-breaking
    characters the way Flask's own `tojson` does; Jinja's autoescaping is
    for HTML, not JS-string context, so json.dumps output still needs this
    before being marked safe."""
    dumped = json.dumps(value).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return Markup(dumped)


templates.env.filters["tojson"] = tojson_filter
# Cache-buster for CSS/JS: browsers aggressively cache /static/* since the
# links carry no version string. Appending ?v=<process-start-time> forces a
# fresh fetch after every restart, so a CSS/JS edit that "isn't showing up"
# is never actually a stale-browser-cache mystery.
templates.env.globals["static_version"] = str(int(time.time()))
templates.env.globals["month_label"] = month_label
templates.env.globals["STATUS_LABELS"] = STATUS_LABELS
templates.env.globals["STATUS_NAMES"] = STATUS_NAMES
templates.env.globals["STATUSES"] = [m.COMPLETE, m.PARTIAL, m.MISSING, m.LEAVE, m.HOLIDAY, m.WEEKEND]


def flash(request, message: str, kind: str = "ok") -> None:
    request.session.setdefault("flash", []).append({"kind": kind, "msg": message})


def pop_flashes(request):
    return request.session.pop("flash", [])


# Starlette changed TemplateResponse's calling convention across versions:
# older releases take (name, context) and pull `request` out of the context
# dict; newer releases require `request` as the first positional argument.
# requirements.txt pins no versions, so detect which one is installed instead
# of hardcoding a version number that will drift out of date.
_first_param = next(iter(inspect.signature(templates.TemplateResponse).parameters), "")
_REQUEST_FIRST = _first_param == "request"


def _admin_nav_badges(db, user) -> dict:
    """Small live counts shown next to Leave/Support in the admin nav.
    Computed once here (rather than in every admin route) so every admin
    screen shows the same up-to-date pending count without each route
    needing to remember to add it.

    A department-scoped admin (is_admin=True, is_super_admin=False — see
    Employee.is_super_admin docstring) only ever sees Leave Requests for
    their own department, so their badge is scoped to match — otherwise
    it would count pending leave they have no way to act on. Support
    Inbox is super-admin-only and isn't even in their nav, so its count
    is skipped for them entirely."""
    def _pending_overtime_count(employee_ids=None) -> int:
        # employee_ids=None -> org-wide (super admin, unscoped like
        # app/auth.py led_by() itself); otherwise only requests from that
        # admin's direct reports (Team Lead scoping is per-person via
        # reports_to_id, not by department — see led_by()'s docstring).
        q = select(func.count()).select_from(m.OvertimeApproval).where(
            m.OvertimeApproval.status == m.OT_REQUESTED
        )
        if employee_ids is not None:
            if not employee_ids:
                return 0
            q = q.where(m.OvertimeApproval.employee_id.in_(employee_ids))
        return db.execute(q).scalar() or 0

    def _pending_suggestions_count(dept=None) -> int:
        # dept=None -> org-wide (super admin); otherwise scoped to whichever
        # department the SUBMITTER (not the suggestion itself, which has no
        # department of its own) belongs to.
        total = 0
        for model in (m.Project, m.TaskType):
            q = select(func.count()).select_from(model).where(model.status == m.LIST_PENDING)
            if dept is not None:
                q = (
                    select(func.count())
                    .select_from(model)
                    .join(m.Employee, m.Employee.id == model.created_by_employee_id)
                    .where(
                        model.status == m.LIST_PENDING,
                        func.coalesce(func.nullif(m.Employee.department, ""), "—") == dept,
                    )
                )
            total += db.execute(q).scalar() or 0
        return total

    if getattr(user, "is_admin", False) and not getattr(user, "is_super_admin", False):
        dept = user.department or "—"
        pending_leave = db.execute(
            select(func.count())
            .select_from(m.LeaveRecord)
            .join(m.Employee, m.Employee.id == m.LeaveRecord.employee_id)
            .where(
                m.LeaveRecord.status == m.LEAVE_REQUESTED,
                func.coalesce(func.nullif(m.Employee.department, ""), "—") == dept,
            )
        ).scalar() or 0
        return {
            "pending_leave": pending_leave, "open_support": 0,
            "pending_suggestions": _pending_suggestions_count(dept),
            "pending_overtime": _pending_overtime_count(led_by(user, db)),
        }
    pending_leave = db.execute(
        select(func.count()).select_from(m.LeaveRecord).where(
            m.LeaveRecord.status == m.LEAVE_REQUESTED
        )
    ).scalar() or 0
    open_support = db.execute(
        select(func.count()).select_from(m.SupportQuery).where(
            m.SupportQuery.status == m.SUPPORT_OPEN
        )
    ).scalar() or 0
    return {
        "pending_leave": pending_leave, "open_support": open_support,
        "pending_suggestions": _pending_suggestions_count(None),
        "pending_overtime": _pending_overtime_count(None),
    }


def render(request, name: str, ctx: dict, db=None, status_code: int = 200):
    ctx = dict(ctx)
    ctx["request"] = request
    ctx["flashes"] = pop_flashes(request)
    # db is optional: only admin routes pass it, and only admin users get
    # the extra queries — employee-page renders (db=None, or user isn't
    # admin) skip this entirely.
    user = ctx.get("user")
    if db is not None and user is not None and getattr(user, "is_admin", False):
        ctx["nav_badges"] = _admin_nav_badges(db, user)
    if _REQUEST_FIRST:
        return templates.TemplateResponse(request, name, ctx, status_code=status_code)
    return templates.TemplateResponse(name, ctx, status_code=status_code)
