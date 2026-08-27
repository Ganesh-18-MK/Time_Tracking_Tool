"""Formatting + audit helpers shared by routes and templates."""
import datetime as dt
import io
import json
import os
import re
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models as m

# The firm's home timezone (manager request, 2026-08-10): every "now"/"today"
# the app captures from the real world is expressed in THIS one fixed
# timezone, regardless of the server container's own OS clock (Cloud Run
# defaults to UTC) or of wherever an employee physically is. An employee in
# IST clicking Start at 8:00 PM local time is captured as ~9:30 AM CDT the
# same instant, not 8:00 PM. CLAUDE.md's "no timezones" hard rule means no
# PER-EMPLOYEE timezone handling (nobody's individual location is tracked or
# converted for) — not "the app has zero timezone awareness." Handles
# CST/CDT automatically (America/Chicago, not a fixed UTC offset).
BUSINESS_TZ = ZoneInfo("America/Chicago")


def now_local() -> dt.datetime:
    """The current wall-clock moment in BUSINESS_TZ. Use this instead of
    dt.datetime.now() anywhere a start_minute/end_minute (clock-face-of-day,
    minutes since midnight) needs to be captured from the real world —
    dt.datetime.now() returns the container's raw OS time (UTC), which is
    not what should ever be shown to or stored for an employee. See
    BUSINESS_TZ above for why."""
    return dt.datetime.now(dt.timezone.utc).astimezone(BUSINESS_TZ)


def today_local() -> dt.date:
    """The current calendar date in BUSINESS_TZ. Use this instead of
    dt.date.today() (which reads the container's raw UTC date) anywhere
    "today" drives business logic — which day a submission/entry/punch
    belongs to, compliance recompute windows, a report's default date
    range, etc. See BUSINESS_TZ above."""
    return now_local().date()


def fmt_hm(minutes: Optional[int]) -> str:
    """480 -> '8:00', 259 -> '4:19'. None -> em dash."""
    if minutes is None:
        return "—"
    sign = "-" if minutes < 0 else ""
    minutes = abs(int(minutes))
    return f"{sign}{minutes // 60}:{minutes % 60:02d}"


def fmt_hm_signed(minutes: Optional[int]) -> str:
    if minutes is None:
        return "—"
    if minutes == 0:
        return "0:00"
    return ("+" if minutes > 0 else "-") + fmt_hm(abs(minutes))


def fmt_time(minute: Optional[int]) -> str:
    """Minutes-since-midnight -> '9:30 AM'."""
    if minute is None:
        return "—"
    h, mi = divmod(int(minute), 60)
    suffix = "AM" if h < 12 or h == 24 else "PM"
    display_h = h % 12 or 12
    return f"{display_h}:{mi:02d} {suffix}"


def fmt_date(value: Optional[dt.date]) -> str:
    """date(2026, 8, 3) -> '08/03/2026' (manager-requested normalization,
    2026-08-03 — every human-readable date on screen and in xlsx exports
    uses this one format now, regardless of what used to be shown: '3 Aug',
    '03 August 2026', etc). None -> em dash, same convention as fmt_hm.
    Deliberately NOT used for <input type="date"> values or any form
    field/URL param that gets parsed back (those stay ISO/YYYY-MM-DD —
    that's the HTML spec for date inputs and what parse_date_field expects,
    not a "display" format a user reads)."""
    if value is None:
        return "—"
    return value.strftime("%m/%d/%Y")


def fmt_datetime(value: Optional[dt.datetime], seconds: bool = False) -> str:
    """datetime(...) -> '08/03/2026 14:32' (or '...:07' with seconds=True).
    Same MM/DD/YYYY date portion as fmt_date, just with the time appended —
    used for timestamp columns (Audit Log, Support Inbox, suggestions,
    submitted-at banners) that used to show '03 Aug 14:32'."""
    if value is None:
        return "—"
    return value.strftime("%m/%d/%Y %H:%M:%S" if seconds else "%m/%d/%Y %H:%M")


def parse_hhmm(value: str) -> int:
    """'09:30' (from <input type=time>) -> minutes since midnight."""
    parts = value.strip().split(":")
    return int(parts[0]) * 60 + int(parts[1])


def clamp_break_end(start_minute: int, end_minute: int) -> int:
    """A break's end-of-day clamp: an end time numerically *before* its
    start means the break ran past midnight (e.g. started 23:58, ended
    00:02) — clamp to end of day, same no-rows-past-midnight convention
    used elsewhere. Equal minutes (started and ended within the same clock
    minute — a real, valid ~0-minute break) must NOT hit this clamp; a
    previous off-by-one (<=) here turned a same-minute break into a
    fabricated multi-hour one."""
    return 1440 if end_minute < start_minute else end_minute


# ---- admin form parsing -----------------------------------------------------
# Every admin POST route below eventually calls dt.date.fromisoformat/int/float
# on raw Form(...) strings. Route through these so a fat-fingered field flashes
# a message instead of a raw 500.
class FormError(Exception):
    """A form field failed to parse. Routes catch this and flash a
    user-readable message instead of letting the request 500."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def parse_date_field(value: str, label: str = "Date") -> dt.date:
    try:
        return dt.date.fromisoformat((value or "").strip())
    except (ValueError, TypeError):
        raise FormError(f"{label} must be a valid date (YYYY-MM-DD).")


def parse_int_field(value, label: str) -> int:
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        raise FormError(f"{label} must be a whole number.")


def parse_hours_field(value, label: str) -> int:
    """'8' / '1.5' -> minutes. Used for target/tolerance/row-length dials."""
    try:
        return int(round(float(value) * 60))
    except (ValueError, TypeError):
        raise FormError(f"{label} must be a number.")


# ---- employee ID generation --------------------------------------------
# "LOMK001", "LOMK002", ... — monotonic and never reused (based on the
# highest number ever assigned, including deactivated employees), so a
# departed employee's old ID is never handed to someone new.
EMPLOYEE_CODE_PREFIX = "LOMK"
_CODE_RE = re.compile(rf"^{EMPLOYEE_CODE_PREFIX}(\d+)$")


def format_employee_code(n: int) -> str:
    return f"{EMPLOYEE_CODE_PREFIX}{n:03d}"


def highest_employee_code_number(db: Session) -> int:
    best = 0
    for (code,) in db.execute(select(m.Employee.employee_code)).all():
        if not code:
            continue
        match = _CODE_RE.match(code)
        if match:
            best = max(best, int(match.group(1)))
    return best


def next_employee_code(db: Session) -> str:
    """For single-row creation (Roster -> Add person). Bulk upload assigns
    a block of codes itself instead of re-querying per row — see
    app/bulk_upload.py."""
    return format_employee_code(highest_employee_code_number(db) + 1)


def ensure_employee_codes(db: Session) -> None:
    """Backfill employee_code for rows created before this column existed.
    Assigns in id order (i.e. original creation order) so codes stay stable
    across repeated runs. A no-op once every row already has one — safe to
    call on every app startup (see app/main.py)."""
    missing = list(
        db.execute(
            select(m.Employee).where(m.Employee.employee_code.is_(None)).order_by(m.Employee.id)
        ).scalars()
    )
    if not missing:
        return
    n = highest_employee_code_number(db) + 1
    for emp in missing:
        emp.employee_code = format_employee_code(n)
        n += 1
    db.commit()


# Three-tier role, shared by Roster -> Add/Edit person and the bulk-upload
# Role column (app/bulk_upload.py parse_role) so both places agree on the
# exact same strings and (is_admin, is_super_admin) mapping. See
# Employee.is_super_admin's docstring for what each tier can see.
ROLE_EMPLOYEE = "employee"
ROLE_ADMIN = "admin"
ROLE_SUPER_ADMIN = "super_admin"
ROLE_CHOICES = (ROLE_EMPLOYEE, ROLE_ADMIN, ROLE_SUPER_ADMIN)


def role_to_flags(role: str):
    """'employee'/'admin'/'super_admin' -> (is_admin, is_super_admin)."""
    if role == ROLE_SUPER_ADMIN:
        return True, True
    if role == ROLE_ADMIN:
        return True, False
    return False, False


def flags_to_role(is_admin: bool, is_super_admin: bool) -> str:
    if is_admin and is_super_admin:
        return ROLE_SUPER_ADMIN
    if is_admin:
        return ROLE_ADMIN
    return ROLE_EMPLOYEE


def ensure_super_admin_backfill(db: Session) -> None:
    """Backfill for the is_super_admin column added 2026-07-31 (department-
    scoped admin / super admin split). SQLite's ADD COLUMN gives every
    existing row NULL, not the ORM-level default — without this, every
    admin who could already see all departments would silently be demoted
    to department-scoped on upgrade. Anyone who was already is_admin=True
    becomes is_super_admin=True; only NEW admins created after this point
    default to department-scoped (see Employee.is_super_admin docstring).
    A no-op once every admin already has the flag set — safe on every
    startup, same pattern as ensure_employee_codes."""
    rows = list(
        db.execute(
            select(m.Employee).where(
                m.Employee.is_admin.is_(True),
                m.Employee.is_super_admin.isnot(True),
            )
        ).scalars()
    )
    if not rows:
        return
    for emp in rows:
        emp.is_super_admin = True
    db.commit()


def ensure_list_status_backfill(db: Session) -> None:
    """Backfill for the Project/TaskType `status` column added 2026-08-01
    (employee/lead suggestions with approval). Same root cause as
    ensure_super_admin_backfill above: SQLite's ADD COLUMN gives every
    existing row NULL, not the ORM-level default — without this, every
    project/task created before this feature shipped would silently
    disappear from the Today picker and be rejected by validate_entry
    (both only treat status == LIST_APPROVED as usable — NULL doesn't match).
    Every pre-existing row is
    an already-in-use, already-trusted entry, not a suggestion awaiting
    review, so it backfills straight to approved rather than pending.
    A no-op once every row already has a status — safe on every startup,
    same pattern as ensure_employee_codes / ensure_super_admin_backfill."""
    changed = False
    for model in (m.Project, m.TaskType):
        rows = list(db.execute(select(model).where(model.status.is_(None))).scalars())
        for row in rows:
            row.status = m.LIST_APPROVED
            changed = True
    if changed:
        db.commit()


def ensure_location_backfill(db: Session) -> None:
    """Backfill for `Employee.location` and `Holiday.location`, both added
    2026-08-12 (per-country holiday management — the team now has both US
    and India staff). Same root cause as ensure_list_status_backfill above:
    SQLite's ADD COLUMN gives every existing row NULL, not the ORM-level
    default — without this, every employee and every already-imported
    historical holiday would silently fail to match either LOCATION_US or
    LOCATION_INDIA in engine.holidays_set()/is_working_day(), so nobody's
    calendar would show any holidays at all until they explicitly picked a
    country. Backfills to m.DEFAULT_LOCATION ("India"), matching the
    original all-offshore scope — a no-op for anyone/anything created after
    this feature shipped, since the column default already applies there.
    A no-op once every row already has a location — safe on every startup,
    same pattern as ensure_employee_codes / ensure_list_status_backfill."""
    changed = False
    emp_rows = list(db.execute(select(m.Employee).where(m.Employee.location.is_(None))).scalars())
    for row in emp_rows:
        row.location = m.DEFAULT_LOCATION
        changed = True
    holiday_rows = list(db.execute(select(m.Holiday).where(m.Holiday.location.is_(None))).scalars())
    for row in holiday_rows:
        row.location = m.DEFAULT_LOCATION
        changed = True
    if changed:
        db.commit()


def ensure_leave_v2_backfill(db: Session) -> None:
    """Backfill for `Employee.is_on_pip`, added 2026-08-21 (Leave
    Management V2). Same root cause as ensure_location_backfill above:
    SQLite's ADD COLUMN gives every existing row NULL, not the ORM-level
    `default=False` — without this, `if emp.is_on_pip` still works fine
    (SQL NULL is falsy in Python exactly like False, same reasoning as
    is_developer's docstring), but leaves the column in a NULL rather than
    an explicit False state, which the admin-facing PIP toggle checkbox
    would otherwise render inconsistently for. A no-op once every row
    already has a real boolean, safe on every startup.

    `Employee.probation_days` deliberately gets NO backfill here — NULL is
    already its correct, meaningful value ("use the company default from
    Config.probation_days_default"), not a gap to fill in, same convention
    as the entitlement columns (casual_leave_days etc.)."""
    rows = list(db.execute(select(m.Employee).where(m.Employee.is_on_pip.is_(None))).scalars())
    if not rows:
        return
    for emp in rows:
        emp.is_on_pip = False
    db.commit()


def ensure_bootstrap_admins(db: Session) -> None:
    """Creates the initial Super Admin account(s) from the BOOTSTRAP_ADMINS
    env var, but ONLY if the employees table is completely empty.

    Solves a chicken-and-egg problem on a brand-new deploy: Postgres starts
    with zero rows, and /signup only lets someone *claim* an existing
    roster row (see app/routes/auth.py) — it never creates one. Without
    this, nobody, including the first admin, could ever sign in on a fresh
    production database.

    Format: "Name:email,Name:email,..." (see deploy_azure.sh). No-op if
    the env var isn't set (local dev's tms.db is seeded by hand instead —
    see seed_dummy_data.py), and no-op the instant any employee exists —
    so BOOTSTRAP_ADMINS can be left in Azure App Settings permanently
    without ever touching real data after the very first startup. Same
    safe-to-call-every-startup pattern as ensure_employee_codes /
    ensure_super_admin_backfill above."""
    raw = os.environ.get("BOOTSTRAP_ADMINS", "").strip()
    if not raw:
        return
    if db.execute(select(m.Employee)).first() is not None:
        return
    n = highest_employee_code_number(db) + 1
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue
        name, email = (part.strip() for part in entry.split(":", 1))
        if not name or not email:
            continue
        db.add(
            m.Employee(
                name=name,
                email=email,
                employee_code=format_employee_code(n),
                active=True,
                is_admin=True,
                is_super_admin=True,
                tracked=False,  # admin accounts excluded from compliance runs, same Roster default
            )
        )
        n += 1
    db.commit()


def xlsx_response(wb: Workbook, filename: str) -> StreamingResponse:
    """Shared by every route that streams an openpyxl Workbook back as a
    download (exports.py, bulk_upload's templates, reports.py) so the
    buf/save/seek/StreamingResponse boilerplate exists in exactly one place."""
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def mask_tail(value: Optional[str], keep: int = 4) -> str:
    """'1234567890123' -> '•••••••••0123'. Used for bank account numbers and
    statutory IDs (PAN/Aadhaar/UAN/ESI) on the Employment Details card —
    these are never shown in full again once saved, to the employee or to
    an admin viewing Roster -> person detail (see app/models.py
    EmployeeBankDetails). Blank/None -> "Not added yet" so the UI reads as
    an empty state, not a broken mask."""
    text = (value or "").strip()
    if not text:
        return "Not added yet"
    if len(text) <= keep:
        return "•" * len(text)
    return "•" * (len(text) - keep) + text[-keep:]


def normalize_title_case(name: str) -> str:
    """'leads console' -> 'Leads Console'. Used on employee/lead-suggested
    Project/TaskType names (Ganesh, 2026-08-21) so two people suggesting the
    same thing with different casing ('leads console' vs 'Leads console')
    land as one consistently-capitalized entry instead of two near-duplicate
    rows sitting in the same pending-approval queue.

    Collapses repeated whitespace and trims. Each whitespace-separated word
    gets its first letter capitalized -- EXCEPT a word that already contains
    an uppercase letter anywhere in it ('iPhone', 'McDonald', 'HR',
    'QuickBooks'), which is left exactly as typed. That's deliberate: a
    plain blind .title() call would mangle those into 'Iphone'/'Mcdonald'/
    'Hr'/'Quickbooks', which is worse than not normalizing at all. Only
    words typed in all-lowercase get capitalized."""
    words = (name or "").strip().split()
    out = []
    for w in words:
        out.append(w if w != w.lower() else w[:1].upper() + w[1:])
    return " ".join(out)


_FIRST_LETTER_RE = re.compile(r"[a-zA-Z]")


def capitalize_first(text: str) -> str:
    """'working on india flag' -> 'Working on india flag'. Applied to
    free-text details fields (Plan for the Day, Add Row, Auto time
    capture) when saved (Ganesh, 2026-08-22) — capitalizes only the first
    *letter* of each line; everything else is left exactly as typed.
    Deliberately NOT normalize_title_case() above — that capitalizes
    every word and exists for short Project/Task labels, not a typed
    sentence or multi-line note, where capitalizing every word would read
    wrong ('Have To Work On The New System').

    Per-line, not just the string's first character (Ganesh, 2026-08-22
    bugfix, same day as the original — real usage turned out to almost
    always be a manually-typed numbered list with no space after the
    number, e.g. '1.need to work on X\\n2.reviewed Y', so capitalizing
    only index 0 capitalized the leading '1' — a no-op — and left every
    actual word lowercase, making the feature look like it did nothing.
    Each line now has its first a-z/A-Z character uppercased wherever it
    falls, so '1.need to work' -> '1.Need to work', '- fixed the bug'
    -> '- Fixed the bug', and a plain 'working on india flag' still
    behaves exactly as before. A line with no letters at all (blank, or
    just punctuation/numbers) is left untouched. Caller is expected to
    have already .strip()'d `text` as a whole; this doesn't strip or
    collapse whitespace itself."""
    if not text:
        return text
    return "\n".join(_FIRST_LETTER_RE.sub(lambda m: m.group(0).upper(), line, count=1)
                      for line in text.split("\n"))


# Human-readable labels for AuditLog.action codes (Ganesh, 2026-08-22) —
# used only by the Dashboard's "Recent activity" preview widget, which
# reads like a manager-facing summary ("Submitted day", "Updated config"),
# not the full /admin/audit trail, which deliberately keeps raw action
# codes + entity/detail columns as-is since that page is a searchable
# technical log meant to be grepped/filtered by the exact code an
# investigation is looking for. Every entry here is a snapshot of the
# audit() call sites that existed on 2026-08-22 (see util.audit callers
# across app/routes/*.py) — a *new* audit() call site with an unmapped
# action code doesn't break anything, it just falls back to the same
# "action_code" -> "Action code" title-casing the Dashboard already used
# before this existed, so this dict is a nice-to-have, not something that
# needs to be kept in lockstep with every new audit() call.
_AUDIT_ACTION_LABELS = {
    "submit_day": "Submitted day",
    "resubmit_day": "Resubmitted day",
    "entry_details_edited": "Edited task details",
    "break_details_edited": "Edited break details",
    "leave_requested": "Requested leave",
    "leave_request_withdrawn": "Withdrew leave request",
    "leave_approve": "Approved leave",
    "leave_reject": "Rejected leave",
    "leave_add": "Added leave",
    "leave_delete": "Deleted leave",
    "leave_bulk_upload": "Bulk-uploaded leave",
    "compensation_match_requested": "Requested overtime match",
    "delete_compensation_link": "Deleted compensation link",
    "reject_compensation_match": "Rejected compensation match",
    "overtime_requested": "Requested overtime",
    "overtime_request_withdrawn": "Withdrew overtime request",
    "overtime_approve": "Approved overtime",
    "overtime_reject": "Rejected overtime",
    "overtime_grant": "Granted overtime",
    "overtime_delete": "Deleted overtime",
    "special_paid_grant": "Granted special paid time",
    "config_change": "Updated config",
    "clear_override": "Cleared override",
    "recompute_month": "Recomputed month",
    "roster_add": "Added employee",
    "roster_edit": "Edited employee",
    "roster_bulk_upload": "Bulk-uploaded roster",
    "reset_password": "Reset password",
    "location_change": "Changed location",
    "profile_personal_details_updated": "Updated personal details",
    "profile_employment_details_updated": "Updated employment details",
    "assignments_save": "Saved assignments",
    "holiday_add": "Added holiday",
    "holiday_delete": "Deleted holiday",
    "holiday_bulk_upload": "Bulk-uploaded holidays",
    "support_query_submitted": "Submitted a support question",
    "support_resolved": "Resolved a support question",
    "ticket_raised": "Raised a ticket",
    "ticket_commented": "Commented on a ticket",
    "ticket_status_changed": "Changed ticket status",
    "approve_complink": "Approved compensation match",
    "reject_complink": "Rejected compensation match",
}
# Ordered so a more specific prefix (e.g. "suggestion_approve_") is checked
# before a shorter one that could also match by accident.
_AUDIT_ACTION_PREFIXES = [
    ("suggestion_approve_", "Approved {} suggestion"),
    ("suggestion_edit_", "Edited {} suggestion"),
    ("suggestion_reject_", "Rejected {} suggestion"),
    ("lists_bulk_upload_", "Bulk-uploaded {} list"),
    ("toggle_", "Toggled {} active"),
    ("add_", "Added {}"),
]


def humanize_audit_action(action: str) -> str:
    """AuditLog.action code -> short human phrase for the Dashboard's
    Recent activity widget. See _AUDIT_ACTION_LABELS above for scope."""
    if action in _AUDIT_ACTION_LABELS:
        return _AUDIT_ACTION_LABELS[action]
    for prefix, template in _AUDIT_ACTION_PREFIXES:
        if action.startswith(prefix):
            return template.format(action[len(prefix):].replace("_", " "))
    return action.replace("_", " ").capitalize()


def punch_remaining_minutes(target_minutes: int, completed_punch_minutes: int) -> int:
    """Countdown-to-target remaining minutes for the Punch In/Out widget on
    Today, computed fresh server-side on every page load; the browser just
    ticks the *currently open* session down in real time from there (see
    today.html), the same way the existing break timer already works —
    every break-excess-extends-target adjustment happens by reloading with
    a new `target`, not by the client recomputing the rule itself.

    Deliberately NOT clamped at 0 — a negative result is overtime, not an
    error, and must stay visible as such (formatted "+H:MM over" by the
    template via hm_signed) rather than silently reading as "done"."""
    return target_minutes - completed_punch_minutes


def overtime_minutes(punched_minutes: int, target_minutes: Optional[int]) -> int:
    """How much of a day's completed Punch In/Out time was beyond that day's
    target — the number shown live on Today (once the countdown goes past
    zero) and aggregated into the Attendance Report's Overtime column (see
    app/reports.py). `target_minutes` should be the day's already-computed,
    already-adjusted target (DayStatus.target_minutes — includes leave and
    break-allowance extension), not a raw daily_target_minutes; passing None
    (e.g. a legacy-imported day with no computed target) reads as "can't
    say", not "zero target", so it returns 0 rather than counting the whole
    punched duration as overtime."""
    if target_minutes is None:
        return 0
    return max(0, punched_minutes - target_minutes)


def overtime_row_flags(durations, target_minutes: int) -> list:
    """Task Planning (Ganesh, 2026-08-21) — which of today's task log rows
    fall after the day's target was already reached, for the distinct
    row-coloring on Today (see today.html's `overtime-row` class). Takes
    plain per-row minute durations in the SAME chronological order the
    rows are displayed (app/routes/employee.py's _day_context already
    orders TaskEntry by start_minute), returns a same-length list of
    booleans — True when the running total BEFORE that row already
    reached target. Pure/no side effects, deliberately: callers zip the
    result back onto their own row objects (a transient `.is_overtime`
    attribute, never persisted) rather than this function knowing
    anything about TaskEntry/ORM objects, so it's trivially testable in
    isolation. `target_minutes <= 0` (e.g. a full day of approved leave,
    nothing expected) means nothing is flagged — every result is False,
    not everything."""
    flags = []
    running = 0
    for d in durations:
        flags.append(target_minutes > 0 and running >= target_minutes)
        running += d
    return flags


def punch_out_error(day_submission: Optional[m.DaySubmission]) -> Optional[str]:
    """Guard for Punch Out (Ganesh, 2026-08-11 — employees were punching out
    with the day's task rows never actually Submit Day'd, so the punched
    duration had nothing backing it in the task log, and compliance had no
    real total to compute against). Punch In/Out is always keyed to "today"
    (see the punch_in/punch_out routes), so this only ever needs today's own
    DaySubmission row, not a date parameter — same "right now" convention as
    the break widget. `sub.locked` is the existing "day is submitted" signal
    used everywhere else (validate_entry, entry_details_edit_error), so this
    reuses it rather than inventing a second one. Returns None when punching
    out is allowed, or a user-facing error otherwise."""
    if day_submission is None or not day_submission.locked:
        return "Submit today's task log (Submit Day) before punching out."
    return None


def fmt_hours(minutes: Optional[int]) -> str:
    """480 -> '8.0h' for compact numeric display."""
    if minutes is None:
        return "—"
    return f"{minutes / 60:.1f}h"


def month_label(year: int, month: int) -> str:
    # '08/2026', not 'August 2026' — matches the MM/DD/YYYY normalization
    # (2026-08-03); a month picker has no day component so MM/YYYY is the
    # closest numeric equivalent.
    return dt.date(year, month, 1).strftime("%m/%Y")


def prev_next_month(year: int, month: int):
    first = dt.date(year, month, 1)
    prev_last = first - dt.timedelta(days=1)
    nxt = (first.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    return (prev_last.year, prev_last.month), (nxt.year, nxt.month)


def parse_ym(ym: Optional[str], default: Optional[dt.date] = None):
    d = default or today_local()
    if ym:
        try:
            y, mo = ym.split("-")
            return int(y), int(mo)
        except ValueError:
            pass
    return d.year, d.month


def audit(
    db: Session,
    actor: str,
    action: str,
    entity: str = "",
    entity_id: str = "",
    detail: Optional[dict] = None,
) -> None:
    """Bug fix (Ganesh, 2026-08-27 — reported via Teams: a generic "Something
    went wrong" 500 on Projects & Tasks -> Add, reproducible "several times",
    plus once on Deactivate; the item was actually saved either way, only
    the audit-log write that immediately follows blew up). Root cause:
    AuditLog.actor/action/entity/entity_id are all VARCHAR(N) columns, but
    call sites across this app (lists_add, lists_toggle,
    suggestion_approve/edit, and others) pass raw free-text — most often a
    Project/Employer or Task name typed by an admin or employee — straight
    through as entity_id with no length check. SQLite (local dev) has no
    real column-length enforcement, so this was invisible there; Postgres
    (the real production database, see CLAUDE.md's GCP deploy notes) DOES
    enforce it and raises a hard error the instant a name runs past 60
    characters, which is neither caught nor validated against anywhere
    upstream — it surfaces as this handler's generic error page even
    though the actual Project/TaskType/etc. row had already committed
    successfully one line earlier (exactly what "it appears to add the
    projects" anyway was describing). Truncating defensively here, in the
    one shared helper, fixes every call site at once rather than needing
    each one individually capped and re-capped forever as new ones are
    added — this is intentionally a blunt safety net, not a validation
    rule; nothing upstream needs to change to start relying on it."""
    db.add(
        m.AuditLog(
            actor=actor[:120],
            action=action[:60],
            entity=entity[:60],
            entity_id=str(entity_id)[:60],
            detail=json.dumps(detail or {}, default=str),
        )
    )
    db.commit()


STATUS_LABELS = {
    m.COMPLETE: "Y",
    m.PARTIAL: "PARTIAL",
    m.MISSING: "N",
    m.LEAVE: "LEAVE",
    m.HOLIDAY: "HOL",
    m.WEEKEND: "",
}

STATUS_NAMES = {
    m.COMPLETE: "Complete",
    m.PARTIAL: "Partial",
    m.MISSING: "Missing",
    m.LEAVE: "Leave",
    m.HOLIDAY: "Holiday",
    m.WEEKEND: "Weekend",
}
