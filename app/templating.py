"""Shared Jinja2 environment with app filters."""
import inspect
import json
import os
import time

from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy import func, select
from sqlalchemy.orm import object_session

from app import engine, models as m
from app.util import (
    STATUS_LABELS,
    STATUS_NAMES,
    approval_progress_steps,
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

# Multilevel approval (Ganesh, 2026-09-01: "i want multilevel approval for
# leave and overtime management... first team lead... admin should get
# request then admin can verify and they can deny or accept with reason
# then... it should go to super admin then super admin can verify it and
# can approve or reject"). Same on/off-by-env-var convention as every flag
# above — defaults on, set to 0 on a host to instantly fall back to today's
# single-step Super-Admin-only approval with no code change. Covers Leave
# requests, Overtime requests, and Overtime <-> Missed Hours match requests
# (CompensationLink) per Ganesh's own scope answer; Special Paid Time and
# every admin-direct entry (leave_add/overtime_grant/add_complink) are
# untouched — nobody requested those, so there's nothing for a Team Lead to
# review (see LeaveRecord.requires_lead_review's own comment in
# app/models.py). See app/routes/admin.py's leave_page()/overtime_page()
# and the new */lead-review routes for what this guards.
MULTILEVEL_APPROVAL_ENABLED = os.environ.get("MULTILEVEL_APPROVAL_ENABLED", "1") == "1"

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
templates.env.globals["MULTILEVEL_APPROVAL_ENABLED"] = MULTILEVEL_APPROVAL_ENABLED
templates.env.globals["LEAD_ACCEPTED"] = m.LEAD_ACCEPTED
templates.env.globals["LEAD_DENIED"] = m.LEAD_DENIED
# Multilevel approval progress tracker (Ganesh, 2026-09-02) — registered as
# a plain callable global, not wired through each route's own render()
# context, since it's a pure function of (record, flag) that every Leave/
# Overtime/CompensationLink template already has both of in scope (the row
# being looped over, and MULTILEVEL_APPROVAL_ENABLED already global) — see
# approval_progress_steps()'s own docstring in app/util.py.
templates.env.globals["approval_progress_steps"] = approval_progress_steps
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
    Employee.is_super_admin docstring) could only VIEW Leave/Overtime as of
    2026-08-30 ("add leaves and overtime viewable access to admins",
    confirmed VIEW ONLY via AskUserQuestion) — Team Requests was no longer
    hidden from their nav (see base.html), but every actual decision stayed
    Super-Admin-only. Multilevel approval (2026-09-01) gives them a REAL
    action again, just a narrower one: they can accept or deny a request as
    its first-stage "Team Lead" review (always with a reason), before a
    Super Admin makes the actual final call — see LeaveRecord.
    requires_lead_review's comment in app/models.py for the full shape.
    This badge now counts what's actually awaiting THIS admin's own lead-
    stage decision (department-scoped for both Leave and Overtime — the
    reviewer-scoping rule is now the same for both, replacing Overtime's
    old per-person led_by() scoping), not the older "every pending
    request" count a Super Admin's own badge still shows below.
    _pending_complink_count() (org-wide, Overtime <-> Missed Hours match
    requests) is now ALSO computed department-scoped for this tier, for the
    same reason — the final decision on those stays Super-Admin-only
    (approve_complink/reject_complink are still require_super_admin), but
    the new lead-review stage on them is not. Support Inbox is still
    super-admin-only and isn't in their nav, so its count is still skipped
    for them."""
    def _pending_overtime_count(employee_ids=None) -> int:
        # employee_ids=None -> org-wide (super admin); otherwise only
        # requests from the given set of employee ids. Only ever called
        # with None now (see the department-scoped branch below, which
        # queries OvertimeApproval directly instead) — kept as a small
        # helper since the super-admin branch further down still uses it.
        q = select(func.count()).select_from(m.OvertimeApproval).where(
            m.OvertimeApproval.status == m.OT_REQUESTED
        )
        if employee_ids is not None:
            if not employee_ids:
                return 0
            q = q.where(m.OvertimeApproval.employee_id.in_(employee_ids))
        return db.execute(q).scalar() or 0

    def _pending_complink_count(dept=None) -> int:
        # Overtime-for-Missed-Hours match requests awaiting a decision
        # (Ganesh, 2026-08-22) — admin wasn't being notified of these
        # anywhere before this. The FINAL decision (approve_complink/
        # reject_complink) is still require_super_admin end to end, so
        # dept=None (org-wide, every such request) is still what feeds the
        # Super Admin's own badge below. Multilevel approval (2026-09-01)
        # gives a department-scoped admin a real lead-review action on
        # these too, so dept=<their department> now also returns a
        # meaningful, narrower count (scoped to the REQUESTING employee's
        # department, and further narrowed to "not yet lead-reviewed" —
        # same shape as the leave/overtime lead filters above).
        # Folded into pending_overtime, not pending_leave — the decision
        # card lives on Overtime Management (moved there from Leave
        # Management 2026-08-22, see overtime_page()'s pending_matches), so
        # the badge matches where the action is either way.
        if not LEAVE_MANAGEMENT_V2_ENABLED:
            return 0
        conds = [
            m.CompensationLink.status == m.LEAVE_REQUESTED,
            m.CompensationLink.requested_by_employee.is_(True),
        ]
        q = select(func.count()).select_from(m.CompensationLink).where(*conds)
        if dept is not None:
            if MULTILEVEL_APPROVAL_ENABLED:
                conds += [m.CompensationLink.requires_lead_review.is_(True), m.CompensationLink.lead_decision.is_(None)]
            q = (
                select(func.count()).select_from(m.CompensationLink)
                .join(m.Employee, m.Employee.id == m.CompensationLink.employee_id)
                .where(*conds, func.coalesce(func.nullif(m.Employee.department, ""), "—") == dept)
            )
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
        # Multilevel approval (2026-09-01): a department-scoped admin's own
        # badge counts requests actually AWAITING THEIR review — once they've
        # accepted/denied, the ball is in the Super Admin's court and it
        # should stop nagging this admin's nav. With the flag off, this
        # collapses back to exactly the old "every pending request in my
        # department" count (requires_lead_review is False for anything
        # created while the flag was off, but a stale True row from before
        # a flag flip would otherwise still gate it, so the flag itself is
        # checked directly rather than relying only on the per-row column).
        leave_lead_filter = [m.LeaveRecord.status == m.LEAVE_REQUESTED]
        ot_lead_filter = [m.OvertimeApproval.status == m.OT_REQUESTED]
        if MULTILEVEL_APPROVAL_ENABLED:
            leave_lead_filter += [m.LeaveRecord.requires_lead_review.is_(True), m.LeaveRecord.lead_decision.is_(None)]
            ot_lead_filter += [m.OvertimeApproval.requires_lead_review.is_(True), m.OvertimeApproval.lead_decision.is_(None)]
        dept_pending_leave = db.execute(
            select(func.count()).select_from(m.LeaveRecord)
            .join(m.Employee, m.Employee.id == m.LeaveRecord.employee_id)
            .where(
                *leave_lead_filter,
                func.coalesce(func.nullif(m.Employee.department, ""), "—") == dept,
            )
        ).scalar() or 0
        # Overtime's admin-facing scoping switched from led_by() (per-person
        # reports_to) to admin_department_scope() (department match) on
        # 2026-09-01 to match Leave's own reviewer-scoping rule — see
        # OvertimeApproval.requires_lead_review's comment in app/models.py.
        dept_pending_overtime = db.execute(
            select(func.count()).select_from(m.OvertimeApproval)
            .join(m.Employee, m.Employee.id == m.OvertimeApproval.employee_id)
            .where(
                *ot_lead_filter,
                func.coalesce(func.nullif(m.Employee.department, ""), "—") == dept,
            )
        ).scalar() or 0
        return {
            "open_support": 0,
            "pending_suggestions": _pending_suggestions_count(dept),
            "pending_leave": dept_pending_leave,
            "pending_overtime": dept_pending_overtime + _pending_complink_count(dept),
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
    if user.personal_details is None:
        return True
    # Bank & statutory details toggle (Ganesh, 2026-09-03, default disabled
    # — see CONFIG_DEFAULTS in app/models.py) — while the section is hidden,
    # an employee has no way to fill in bank_details at all, so this must
    # stop requiring it or every employee would get nagged forever with no
    # way to dismiss the underlying cause. object_session(user) reuses the
    # same live SQLAlchemy session this function's own docstring already
    # relies on for personal_details/bank_details lazy-loading, rather than
    # threading a separate db argument through render()'s many employee-page
    # call sites (most of which don't currently pass one at all). Falls back
    # to "don't require it" if no live session is found — the safe default,
    # and also what CONFIG_DEFAULTS itself defaults to.
    sess = object_session(user)
    employment_details_enabled = (
        engine.get_config(sess).get("employment_details_enabled") == "1" if sess is not None else False
    )
    if not employment_details_enabled:
        return False
    return user.bank_details is None


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
