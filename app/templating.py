"""Shared Jinja2 environment with app filters."""
import inspect
import json
import os
import time

from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy import func, select

from app import models as m
from app.util import (
    STATUS_LABELS,
    STATUS_NAMES,
    fmt_date,
    fmt_datetime,
    fmt_hm,
    fmt_hm_signed,
    fmt_hours,
    fmt_time,
    humanize_audit_action,
    mask_tail,
    month_label,
    today_local,
)

# Feature flag (Ganesh, 2026-08-06): Ticketing System code is finished and
# tested, but he wants to deploy the Time by Project/Task report on its own
# first and ship Ticketing separately later. Flip to True (and undo the two
# router/mount guards in app/main.py) when it's ready to go live — nothing
# else needs to change, the code itself was never touched. Reads from an env
# var so it can also be flipped per-environment without a code change/redeploy
# if that's ever useful (e.g. enabled in a staging deploy first).
TICKETING_ENABLED = os.environ.get("TICKETING_ENABLED", "0") == "1"

# Same pattern as TICKETING_ENABLED above. Held back from the 2026-08-13
# deploy (default off) so My Month's read-only lookback and the button-
# highlight fix could ship on their own first; back on by default as of
# 2026-08-13 (Ganesh) now that deploy has landed. The env var still works
# as an override either direction — set HOLIDAY_MANAGEMENT_ENABLED=0 on a
# host if it ever needs to go dark again without a code change. See
# app/routes/employee.py's holidays_page()/update_location() and
# app/routes/admin.py's holiday_* routes for the matching guards.
HOLIDAY_MANAGEMENT_ENABLED = os.environ.get("HOLIDAY_MANAGEMENT_ENABLED", "1") == "1"

# Leave Management V2 (Ganesh, 2026-08-21) — default ON as of 2026-08-22.
# This touches real pay-adjacent decisions (accrual, partial approval, PIP
# forcing unpaid), and per CLAUDE.md a change to app/engine.py normally
# needs a real `pytest tests/ -q` + `legacy.verify_strikes` run first.
# pytest ran on Ganesh's machine: 468/469 passed, one pre-existing
# SQLAlchemy-version-drift flake in test_util.py (unrelated to Leave V2 —
# see feedback-timekeeping-dependency-drift) fixed the same day but not
# yet re-confirmed green by a second run. verify_strikes could NOT run — his
# checkout is missing the legacy .ods bundle/tms.db/import_report.json
# entirely (deliberately git-ignored real HR data, tracked separately as
# an open item to get from Steve — see the missing-legacy-data memory).
# Ganesh explicitly chose to enable anyway rather than wait on that
# bundle — this is a known, accepted gap, not an oversight; re-run
# verify_strikes and treat any strike-count drift on a day with leave as
# a signal to investigate leave_minutes_on()/leave_balance_v2() once the
# bundle is finally in hand. Env var still works as an override either
# direction — set LEAVE_MANAGEMENT_V2_ENABLED=0 to go back to the old 4
# leave types without a code change. See app/routes/employee.py's
# my_leave()/request_leave() and app/routes/admin.py's leave_page()/
# leave_approve() for the guards this flag controls.
LEAVE_MANAGEMENT_V2_ENABLED = os.environ.get("LEAVE_MANAGEMENT_V2_ENABLED", "1") == "1"

# Task Logs report's daily summary was originally an LLM call gated by a
# TASK_SUMMARY_ENABLED flag (app/llm.py, Anthropic Messages API) — replaced
# 2026-08-29 with reports.rule_based_day_summary(), a pure/deterministic
# function with no network call, no API key, and no failure mode, so there
# is nothing left to flag on/off. app/llm.py is gone; a pre-2026-08-29
# TaskDaySummary row (if any host still has one) is simply never read
# anymore — see that model's docstring in app/models.py.

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
# Footer copyright year (Ganesh, 2026-08-14). Computed once at process
# start like static_version above, not per-request — a running server
# happening to still be up at midnight on New Year's Eve is an acceptable
# edge case for a footer credit line. BUSINESS_TZ via today_local(), same
# as every other "what date is it" question in this app.
templates.env.globals["footer_year"] = today_local().year
templates.env.globals["month_label"] = month_label
templates.env.globals["humanize_audit_action"] = humanize_audit_action
templates.env.globals["STATUS_LABELS"] = STATUS_LABELS
templates.env.globals["STATUS_NAMES"] = STATUS_NAMES
templates.env.globals["STATUSES"] = [m.COMPLETE, m.PARTIAL, m.MISSING, m.LEAVE, m.HOLIDAY, m.WEEKEND]
templates.env.globals["TICKET_TYPE_LABELS"] = m.TICKET_TYPE_LABELS
templates.env.globals["TICKET_PRIORITY_LABELS"] = m.TICKET_PRIORITY_LABELS
templates.env.globals["TICKET_STATUS_LABELS"] = m.TICKET_STATUS_LABELS
templates.env.globals["TICKET_TYPES"] = list(m.TICKET_TYPES)
templates.env.globals["TICKET_PRIORITIES"] = list(m.TICKET_PRIORITIES)
templates.env.globals["TICKET_STATUSES"] = list(m.TICKET_STATUSES)
templates.env.globals["TICKETING_ENABLED"] = TICKETING_ENABLED
templates.env.globals["HOLIDAY_MANAGEMENT_ENABLED"] = HOLIDAY_MANAGEMENT_ENABLED
templates.env.globals["LEAVE_MANAGEMENT_V2_ENABLED"] = LEAVE_MANAGEMENT_V2_ENABLED
templates.env.globals["LEAVE_TYPES_V2"] = list(m.LEAVE_TYPES_V2)
templates.env.globals["LEAVE_DURATIONS"] = list(m.LEAVE_DURATIONS)
templates.env.globals["BEREAVEMENT_RELATIONS"] = list(m.BEREAVEMENT_RELATIONS)


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
    Employee.is_super_admin docstring) no longer has Leave Management or
    Overtime Management in their nav at all (Ganesh, 2026-08-28 — narrowed
    to the 5-item Team Lead access list: Add Project/Task names, Assign,
    Approve suggestions, View task logs, View reports), so this function
    skips computing pending_leave/pending_overtime for them entirely —
    those counts would have nowhere to display and no action they could
    take. Support Inbox is likewise super-admin-only and isn't in their
    nav, so its count is skipped for them too."""
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

    def _pending_complink_count() -> int:
        # Overtime-for-Missed-Hours match requests awaiting a decision
        # (Ganesh, 2026-08-22) — admin wasn't being notified of these
        # anywhere before this (no nav badge counted them, and the
        # Compensation links tables showed a still-pending request
        # identically to an already-approved link; see the status-badge
        # fix in those templates the same day). approve_complink/
        # reject_complink are both require_super_admin (unlike
        # pending_leave/pending_overtime above, which a department-scoped
        # Team Lead can also act on), so this is only ever added into the
        # super-admin badge below, never the department-scoped one.
        # Folded into pending_overtime, not pending_leave — the decision
        # card itself lives on Overtime Management (moved there from
        # Leave Management the same day, see overtime_page()'s
        # pending_matches), so the badge now matches where the action is.
        if not LEAVE_MANAGEMENT_V2_ENABLED:
            return 0
        return db.execute(
            select(func.count()).select_from(m.CompensationLink).where(
                m.CompensationLink.status == m.LEAVE_REQUESTED,
                m.CompensationLink.requested_by_employee.is_(True),
            )
        ).scalar() or 0

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
        return {
            "open_support": 0,
            "pending_suggestions": _pending_suggestions_count(dept),
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
        "pending_overtime": _pending_overtime_count(None) + _pending_complink_count(),
    }


def _needs_profile_reminder(request, user) -> bool:
    """Mandatory-but-not-blocking profile-completion popup (Ganesh,
    2026-08-21, including for new hires — nothing here checks start_date,
    so it applies the same to someone who joined today as someone who's
    been here two years and never got around to it). 'Incomplete' means
    either self-service card on Profile hasn't been saved even once yet —
    see app/models.py Employee.personal_details/bank_details, both None
    until the employee submits app/routes/employee.py's
    personal_details_save()/employment_details_save() at least once. Not
    field-by-field: the two cards themselves are the unit of
    'complete enough', same granularity Profile already presents them at.

    'Once per login, reappears until complete': shows on every page in the
    employee zone (see base.html) until the employee clicks 'Remind me
    later' (sets profile_reminder_dismissed in the session — see
    dismiss_profile_reminder() below), and comes back on their NEXT login
    because auth.py's logout clears the whole session. A session that's
    never explicitly logged out of (browser left open, cookie not yet
    expired) will keep the dismissal instead of re-nagging mid-session —
    that's the intended trade-off, not a bug.

    Relies on `user` still being attached to the request's live SQLAlchemy
    session so personal_details/bank_details can lazy-load here — true for
    every real request (FastAPI caches Depends(get_db) per request, and
    the session isn't closed until the response is fully sent), so this
    needs no separate `db` argument."""
    if user is None or getattr(user, "id", None) is None:
        return False
    if request.session.get("profile_reminder_dismissed"):
        return False
    return user.personal_details is None or user.bank_details is None


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
    ctx["show_profile_reminder"] = _needs_profile_reminder(request, user)
    if _REQUEST_FIRST:
        return templates.TemplateResponse(request, name, ctx, status_code=status_code)
    return templates.TemplateResponse(name, ctx, status_code=status_code)
