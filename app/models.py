"""Data model per PRD §8.

Conventions:
  * All durations/targets/variances are integer MINUTES (no float-hour drift,
    matches the minute-level logging the whole system is built on).
  * Times of day are integer minutes since midnight (0..1440). No PER-EMPLOYEE
    timezones — every value is captured in one fixed reference timezone (the
    firm's home Central time, see util.py's BUSINESS_TZ/now_local()/
    today_local()), regardless of the server container's own OS clock or
    wherever an employee physically is. Matches the single-timezone
    assumption the legacy sheets were built on.
  * DayStatus is the materialized compliance view. Rows with source='imported'
    are frozen legacy fact and are never recomputed; source='computed' rows
    are rebuilt from live data at any time.
"""
import datetime as dt
from typing import Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

# ---- statuses ---------------------------------------------------------------
COMPLETE = "complete"
PARTIAL = "partial"
MISSING = "missing"
LEAVE = "leave"
HOLIDAY = "holiday"
WEEKEND = "weekend"
STRIKE_STATUSES = (PARTIAL, MISSING)

LEAVE_TYPES = ("Casual", "Sick", "Vacation", "Other")

# Leave Management V2 (Ganesh, 2026-08-21 — see docs/LEAVE_MANAGEMENT_PLAN.md)
# — the 5 replacement types requested. LEAVE_TYPES above is left untouched
# on purpose: existing LeaveRecord.type values ("Casual"/"Sick"/"Vacation"/
# "Other") are frozen historical fact, same "never rewrite frozen fact"
# principle CLAUDE.md already uses for imported DayStatus rows — old rows
# keep their old string exactly as stored. LEAVE_TYPES_V2 is what every NEW
# request/admin-entry picks from once LEAVE_MANAGEMENT_V2_ENABLED is on
# (see app/templating.py); the employee/admin leave screens switch their
# dropdown to this list, they don't merge the two.
LEAVE_PLANNED = "Planned Time"
LEAVE_UNPLANNED = "Unplanned Time"
LEAVE_UNPAID = "Unpaid Time"
LEAVE_BEREAVEMENT = "Bereavement Time"
LEAVE_SPECIAL_PAID = "Special Paid Time"
LEAVE_TYPES_V2 = (LEAVE_PLANNED, LEAVE_UNPLANNED, LEAVE_UNPAID, LEAVE_BEREAVEMENT, LEAVE_SPECIAL_PAID)
# Only Planned Time accrues automatically and only it is blocked during
# probation (PDF: "Unplanned Time... available immediately, no probation
# period" — the same reasoning extends to Unpaid/Bereavement/Special Paid,
# none of which are earned, so there's nothing to wait for).
LEAVE_TYPES_NO_PROBATION_BLOCK = (LEAVE_UNPLANNED, LEAVE_UNPAID, LEAVE_BEREAVEMENT, LEAVE_SPECIAL_PAID)
# Duration picker (requirement 2) — Half Day / Full Day are derived from the
# employee's own daily_target_minutes (target÷2, target), not a hardcoded
# 4h/8h, so someone on a non-standard schedule still gets a proportionally
# correct number (see docs/LEAVE_MANAGEMENT_PLAN.md §2). Custom reuses the
# existing free-hours input.
LEAVE_DURATION_HALF = "half"
LEAVE_DURATION_FULL = "full"
LEAVE_DURATION_CUSTOM = "custom"
LEAVE_DURATIONS = (LEAVE_DURATION_HALF, LEAVE_DURATION_FULL, LEAVE_DURATION_CUSTOM)
# Bereavement relationship picker (requirement 7) — deliberately a short
# open list, same convention as LOCATIONS/BREAK_TYPES: a free-text "Other"
# covers anything not listed rather than trying to enumerate every possible
# relation up front.
BEREAVEMENT_RELATIONS = ("Spouse", "Child", "Parent", "Sibling", "Other")

# Employee work location (Ganesh, 2026-08-12: holiday management — the team
# now has both US and India staff, and each country's holiday calendar is
# different, so compliance/"is this a working day" can no longer assume one
# single region as it always has before — see engine.holidays_set()/
# is_working_day(), both of which now take a location). Deliberately a short
# open list, not a hardcoded two-value enum/bool, so adding a third country
# later is a one-line change here, same convention as BREAK_TYPES/LEAVE_TYPES
# below. Every existing employee and every already-imported historical
# Holiday row defaults to "India" (the original scope was "~45 offshore
# staff") — see ensure_location_backfill in app/util.py — so nothing changes
# for anyone until they, or an admin, explicitly pick a country.
LOCATION_US = "US"
LOCATION_INDIA = "India"
LOCATIONS = (LOCATION_INDIA, LOCATION_US)
DEFAULT_LOCATION = LOCATION_INDIA


class Employee(Base):
    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), index=True)
    # nullable (not "") so multiple not-yet-set employees don't collide on the
    # unique constraint; signup claims a roster row by matching this field.
    email: Mapped[Optional[str]] = mapped_column(String(200), unique=True, nullable=True)
    # set by the employee via /signup (PBKDF2, see app/security.py); NULL
    # until they've claimed their account. Admin can clear it to force re-signup.
    password_hash: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    department: Mapped[str] = mapped_column(String(120), default="")
    designation: Mapped[str] = mapped_column(String(120), default="")
    daily_target_minutes: Mapped[int] = mapped_column(Integer, default=480)
    # comma-separated weekday numbers, Monday=0 (PRD open question 8 default Mon-Fri)
    work_days: Mapped[str] = mapped_column(String(20), default="0,1,2,3,4")
    start_date: Mapped[dt.date] = mapped_column(Date, nullable=True)
    # optional — collected via Roster/Edit or bulk upload; nothing in the
    # engine reads this today, it's HR reference data only.
    date_of_birth: Mapped[Optional[dt.date]] = mapped_column(Date, nullable=True)
    # kept separate from phone so a country like "+91" is never guessed back
    # out of a combined string on re-export (see app/bulk_upload.py) — free
    # text, not validated against a real dialing-code list.
    country_code: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    # Human-facing, stable ID ("LOMK001", ...) — never reused, never edited
    # by hand. Assigned at creation (see app/util.py next_employee_code) and
    # is the match key for bulk *updating* existing employees; existing rows
    # from before this column existed are backfilled once at startup (see
    # app/util.py ensure_employee_codes, called from app/main.py).
    employee_code: Mapped[Optional[str]] = mapped_column(String(20), unique=True, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    # Two-tier admin (Ganesh, 2026-07-31; access list narrowed 2026-08-28).
    # is_admin alone now means "department-scoped admin / team lead",
    # restricted to exactly 5 capabilities, all filtered to their own
    # Employee.department (or per-person reports_to_id for Assignments):
    # add Project/Task names (Lists "Add" form + bulk upload — but not
    # Deactivate/Reactivate/Rename), assign Projects/Tasks to their team,
    # approve suggested Project/Task names from their team, view task
    # logs for their team (Person Detail — read-only: no override/unlock/
    # compensation-link actions there), and view Time/Project/Strikes/
    # Attendance reports for their team. Leave Management and Overtime
    # Management, previously included, are Super-Admin-only as of the
    # same 2026-08-28 change. is_super_admin=True is the org-wide tier
    # that sees every department and every other admin screen (Roster,
    # Settings, Audit Log, Support Inbox, Leave Management, Overtime
    # Management, Projects & Tasks list-maintenance, bulk uploads,
    # person-detail overrides/unlocks/compensation links) — see
    # app/auth.py require_super_admin (full current boundary lives in its
    # docstring) and app/util.py ensure_super_admin_backfill (existing
    # admins were auto-promoted to super_admin on upgrade so nobody who
    # could see everything before this column existed lost access
    # silently).
    is_super_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    # A THIRD, independent axis from is_admin/is_super_admin (Ganesh,
    # 2026-08-06) — who can work tickets in the new Ticketing System (see
    # Ticket below). A plain Employee can be a Developer; an Admin need
    # not be one. Set via Roster -> Edit only (Super-Admin-gated, same as
    # the reports_to Team Lead picker). No startup backfill needed: unlike
    # Project.status (needs an exact string match), every check on this
    # column is plain truthiness (`if emp.is_developer`), and SQL NULL is
    # falsy there exactly like Python False — see
    # feedback_timekeeping_sqlite_add_column_gap memory note.
    is_developer: Mapped[bool] = mapped_column(Boolean, default=False)
    # tracked=False => excluded from compliance runs (e.g. admin accounts)
    tracked: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    # filename only (e.g. "14.jpg"), not a full path — stored under
    # app/static/uploads/avatars/. NULL until the employee uploads one via
    # /profile. Local disk for now; see note on SupportQuery/deployment re:
    # Render's ephemeral filesystem before this goes live long-term.
    photo_path: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # Annual entitlement, in whole days — set via Leave -> Bulk assign leaves
    # (see app/leave_bulk_upload.py). Display-only for now (PRD open question
    # 6 said "no quotas enforced, totals displayed"; this is that display —
    # nothing blocks an admin from approving leave past these numbers).
    # NULL means "not set yet", shown as 0 rather than blank.
    casual_leave_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    sick_leave_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    vacation_leave_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Who this employee reports to (Ganesh, 2026-08-01) — self-referencing,
    # optional (NULL for anyone not yet assigned a lead, e.g. top-level
    # leadership). Display/reference only for now — nothing in the
    # compliance engine reads this; it's set via Roster -> Edit or the
    # bulk-upload "Reports To" column (matched by employee code, same key
    # bulk *updates* already use — see app/bulk_upload.py).
    reports_to_id: Mapped[Optional[int]] = mapped_column(ForeignKey("employees.id"), nullable=True)
    # Work location / country (Ganesh, 2026-08-12) — drives which country's
    # Holiday calendar this employee's own compliance days/My Month use (see
    # engine.holidays_set()/is_working_day()). Self-service (Profile page,
    # left of the photo upload) or admin-set (Roster -> Add/Edit), same
    # dual-editable pattern as department/designation. Defaults to
    # DEFAULT_LOCATION ("India") rather than NULL/blank, since "which
    # calendar applies" always needs an answer, unlike e.g. date_of_birth
    # where blank is a perfectly fine, honest "not collected yet" state.
    location: Mapped[str] = mapped_column(String(20), default=DEFAULT_LOCATION)

    # Leave Management V2 (Ganesh, 2026-08-21). Both additive/nullable —
    # real production data exists now, no rm-tms.db migration path (same
    # reasoning as every other column added this way, see location above).
    # is_on_pip: Performance Improvement Plan flag (requirement 10 — "no
    # paid leave while on a PIP", enforced in engine.effective_leave_type()
    # and the request routes, not by blocking the request itself: a PIP
    # employee can still request time off, it's just force-converted to
    # Unpaid Time so nobody has to remember to pick the right type by hand).
    # Plain on/off, no start/end date of its own — see the "still open"
    # section of docs/LEAVE_MANAGEMENT_PLAN.md; a start/end-dated PIP is a
    # bigger, separate feature (notifications, auto-expiry) not asked for
    # here, and easy to layer on top of a plain bool later without a schema
    # break. Toggled via Roster -> Edit (Super-Admin-gated, same tier as the
    # reports_to/Developer fields above).
    is_on_pip: Mapped[bool] = mapped_column(Boolean, default=False)
    # probation_days: NULL means "use the company default from
    # Config.probation_days_default" (see CONFIG_DEFAULTS below) — same
    # nullable-means-fall-back-to-config convention as the entitlement
    # columns above, not a value that needs its own backfill (NULL is
    # already the correct, meaningful state for every existing employee
    # until an admin explicitly overrides one person's probation length).
    probation_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    entries = relationship("TaskEntry", back_populates="employee")
    # remote_side=[id]: tells SQLAlchemy this is the "many" side pointing at
    # the "one" parent row on the same table (self-referencing FK) — without
    # it, SQLAlchemy can't tell which side of employees.id <-> reports_to_id
    # is the parent.
    reports_to = relationship("Employee", remote_side=[id], foreign_keys=[reports_to_id])
    # uselist=False: one row per employee, not a list — see
    # EmployeePersonalDetails/EmployeeBankDetails below. Both are optional
    # (None until the employee fills the form in on Profile) and separate
    # from Employee itself so this already-wide table doesn't grow another
    # ~20 mostly-blank columns, and so the two are independently gated on
    # the Employee.email-is-the-signup-key concern (see profile routes).
    personal_details = relationship(
        "EmployeePersonalDetails", back_populates="employee", uselist=False,
        cascade="all, delete-orphan",
    )
    bank_details = relationship(
        "EmployeeBankDetails", back_populates="employee", uselist=False,
        cascade="all, delete-orphan",
    )

    @property
    def work_day_set(self):
        return {int(x) for x in self.work_days.split(",") if x.strip() != ""}


class EmployeePersonalDetails(Base):
    """Self-service 'Personal Details' card on Profile — personal info and
    contact info combined into one record (Ganesh: "personal and contact
    details should be in one box"). Employee.date_of_birth/phone/
    country_code/email already exist and are NOT duplicated here — this
    table only holds what's genuinely new. Display-only reference data for
    HR; nothing in app/engine.py reads it.
    """
    __tablename__ = "employee_personal_details"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), unique=True, index=True)

    blood_type: Mapped[Optional[str]] = mapped_column(String(5), nullable=True)
    gender: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    marital_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    family_members: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    nationality: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    hobbies: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    professional_skills: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    special_skills: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    known_languages: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # "Contact" itself is Employee.country_code + Employee.phone (not
    # duplicated); these are the additional numbers/addresses from the
    # requested screens.
    company_contact: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    alternate_phone: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    emergency_phone: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    whatsapp_number: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    personal_email: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    current_address: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    permanent_address: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    updated_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    updated_by: Mapped[str] = mapped_column(String(120), default="")

    employee = relationship("Employee", back_populates="personal_details")


class EmployeeBankDetails(Base):
    """Self-service 'Employment Details' card on Profile — bank account plus
    the statutory IDs Indian payroll needs (PAN/Aadhaar/UAN/ESI). Every
    field here is sensitive and is masked to everyone, including admins,
    everywhere it's displayed (see app/util.py mask_tail + the 'mask' Jinja
    filter) — the full value is only ever sent to the browser once, at the
    moment the employee types it in; re-submitting blank always means
    "leave this one alone", same convention as the bulk-upload sheets.
    """
    __tablename__ = "employee_bank_details"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), unique=True, index=True)

    account_holder_name: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    account_number: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    ifsc_code: Mapped[Optional[str]] = mapped_column(String(15), nullable=True)
    bank_name: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    branch_name: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    account_type: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    pan_number: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    aadhaar_number: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    uan_number: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    esi_number: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)

    updated_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    updated_by: Mapped[str] = mapped_column(String(120), default="")

    employee = relationship("Employee", back_populates="bank_details")


# Project/TaskType suggestion workflow (Ganesh, 2026-08-01): employees and
# leads can suggest a new one; it stays invisible AND unusable by anyone —
# including whoever suggested it (Ganesh, 2026-08-11: the original
# immediate-use-by-submitter carve-out was removed after an admin reported
# unreviewed suggestions ending up on real logged time before review) —
# until a team lead/admin approves it (see app/routes/employee.py's
# dropdown-visibility filter and validate_entry, both status == LIST_APPROVED
# only). Every row created before this existed, and every row an admin adds
# directly via Lists/bulk-upload, defaults straight to LIST_APPROVED —
# nothing already in the roster is retroactively hidden.
LIST_APPROVED = "approved"
LIST_PENDING = "pending"
LIST_REJECTED = "rejected"
LIST_STATUSES = (LIST_APPROVED, LIST_PENDING, LIST_REJECTED)


class Department(Base):
    """A real managed list of department names (Ganesh, 2026-09-02: "under
    bulk upload i need Departments option where we can add departments then
    while adding employees it will show the existed departments dropdown").

    Before this, "department" was never its own thing anywhere in the app —
    just a free-text string on Employee.department, and every dropdown that
    looked like a department picker (Projects & Tasks' "Manage departments"
    panel, the Reports pages' Department filter, Assign Work's department
    tree, Roster's own department-pill filter) actually just showed whatever
    distinct strings happened to already exist on employees
    (reports.departments_list(), pre-2026-09-02: `sorted({e.department for e
    in employees})`). That meant there was no way to add a department before
    someone was assigned to it, and no way to rename one everywhere at once
    without hand-editing every employee row. This table is deliberately the
    same simple shape as Project/TaskType (id/name/active/created_by/
    created_at) rather than a new pattern — `reports.departments_list()` now
    reads active Department rows instead of scanning Employee data, which is
    what makes it the single source of truth: every existing caller of that
    function (Projects & Tasks' Add-a-project/Manage-departments pickers,
    every Reports page's Department filter, Assign Work's department tree)
    picks this up for free, no template changes needed anywhere else.

    Management UI lives on Roster -> Bulk upload (Ganesh's own explicit
    choice, not the more typical "new page like Projects & Tasks" — see
    admin/roster_bulk_upload.html's "Departments" card and
    lists_department_add/rename/toggle in app/routes/admin.py), Super-
    Admin-only, same tier as every other Roster/Bulk-upload action.

    Existing employees' current free-text department values are auto-
    imported as the starting list on first startup (Ganesh's explicit
    choice over starting empty) — see util.ensure_departments_backfill(),
    wired into main.py's startup sequence like every other one-time
    additive-column backfill in this app.

    Deliberately NOT touched by this feature (a stated scope boundary, not
    an oversight): the Roster bulk-upload Excel sheet's own Department
    column stays free text, same as before — it is not validated against
    this table and does not auto-create new Department rows. Auto-creating
    departments from a bulk file risked silently multiplying near-duplicate
    names from typos with no admin review; an admin who wants a bulk-
    uploaded department to show up in the dropdown still adds it once here
    first, same as any other new department."""

    __tablename__ = "departments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(20), default=LIST_APPROVED)
    created_by_employee_id: Mapped[Optional[int]] = mapped_column(ForeignKey("employees.id"), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    reviewed_by: Mapped[str] = mapped_column(String(120), default="")
    reviewed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    review_note: Mapped[str] = mapped_column(Text, default="")
    # Admin can rewrite a still-pending suggestion's name before deciding
    # (Ganesh, 2026-08-21) — see app/routes/admin.py's suggestion_edit().
    # original_name is only ever set the FIRST time this row is edited (a
    # second edit doesn't overwrite it), so it always shows what the
    # employee actually typed, however many times an admin has since
    # rewritten it. employee_notified_at is reset to NULL on every edit
    # (even a second/third one) so each rewrite gets its own banner on the
    # employee's Today page (app/routes/employee.py) — see
    # _pending_edit_notices() there. All nullable/blank-default so the
    # existing additive-only column migration (app/db.py) handles this
    # with no separate backfill step needed, same reasoning as
    # Employee.is_developer's docstring.
    edited_by: Mapped[str] = mapped_column(String(120), default="")
    edited_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    original_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    employee_notified_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    # Case Type (Ganesh, 2026-08-28) — an admin-set flag on individual
    # Project rows, NOT a rename of Project itself and NOT a separate
    # dropdown: some existing Project/Employer entries represent actual
    # legal case work, others don't (Internal, Admin, Training, etc.), and
    # only an admin marking a specific project as one flips this. When an
    # employee picks a project with is_case_type=True on the Today page
    # (Add Row / Auto time capture / Plan for the Day — all three share
    # the same Project combo), a Client field appears for them to record
    # who the case is for (see TaskEntry.client/PlannedTask.client/
    # ActiveTaskTimer.client below). Same "SQL NULL is falsy" reasoning as
    # Employee.is_developer above applies here too: every check on this
    # column is plain truthiness (`if project.is_case_type`), so the
    # additive-only column migration (app/db.py's _add_missing_columns)
    # adding it as NULL on all ~300 existing Project rows is already the
    # correct answer — "not yet marked as a Case Type" is exactly what an
    # admin hasn't done for any of them yet. Settable at creation (Lists
    # "Add" form — available to both admin tiers, same as the rest of
    # Project creation) and toggle-able afterward via
    # POST /admin/lists/project/{id}/case-type/toggle, Super-Admin-only
    # (same tier as Rename/Deactivate — see require_super_admin).
    is_case_type: Mapped[bool] = mapped_column(Boolean, default=False)

    created_by = relationship("Employee", foreign_keys=[created_by_employee_id])


class TaskType(Base):
    __tablename__ = "task_types"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(20), default=LIST_APPROVED)
    created_by_employee_id: Mapped[Optional[int]] = mapped_column(ForeignKey("employees.id"), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    reviewed_by: Mapped[str] = mapped_column(String(120), default="")
    reviewed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    review_note: Mapped[str] = mapped_column(Text, default="")
    # Same as Project's matching fields above — see that docstring.
    edited_by: Mapped[str] = mapped_column(String(120), default="")
    edited_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    original_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    employee_notified_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    # Freeform grouping label for the Projects & Tasks tree's "All
    # departments" section (Ganesh, 2026-08-30, from a pasted mockup: "these
    # are common for all departments") — groups ONLY the truly unrestricted/
    # shared tasks (zero ProjectTask links) into named buckets like
    # "General"/"Meetings"/"Printing" with an hours rollup per bucket and
    # per task; a project-scoped task already has a home under its own
    # project node, so it isn't shown there and its category is cosmetic.
    # Admin-set freeform text (AskUserQuestion, 2026-08-30: "New freeform
    # field, admin-set" over a fixed preset list) via the "All Tasks"
    # table's own rename form (lists_rename, app/routes/admin.py) — typing
    # a new name creates a new category, no separate management screen.
    # Defaults to "General" for every new task; see
    # ensure_task_category_backfill (app/util.py) for why every
    # pre-existing row also needs an explicit backfill rather than relying
    # on this default (SQLite ADD COLUMN never backfills existing rows).
    category: Mapped[str] = mapped_column(String(60), default="General")

    created_by = relationship("Employee", foreign_keys=[created_by_employee_id])


class ProjectAssignment(Base):
    """A team lead's 'this employee works on this project' marker (Ganesh,
    2026-08-01). Deliberately advisory, NOT enforced — app/validation.py
    does not reject a TaskEntry against an unassigned project. An
    assignment only changes what's shown first/highlighted on the Today
    entry form (see app/routes/employee.py), so nobody is ever blocked
    from logging time just because assignments haven't been set up for
    them yet — safe to roll out gradually, department by department."""

    __tablename__ = "project_assignments"
    __table_args__ = (UniqueConstraint("employee_id", "project_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    assigned_by: Mapped[str] = mapped_column(String(120), default="")
    assigned_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)

    employee = relationship("Employee", foreign_keys=[employee_id])
    project = relationship("Project")


class TaskAssignment(Base):
    """Same idea as ProjectAssignment, for TaskType — see that docstring."""

    __tablename__ = "task_assignments"
    __table_args__ = (UniqueConstraint("employee_id", "task_type_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    task_type_id: Mapped[int] = mapped_column(ForeignKey("task_types.id"), index=True)
    assigned_by: Mapped[str] = mapped_column(String(120), default="")
    assigned_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)

    employee = relationship("Employee", foreign_keys=[employee_id])
    task_type = relationship("TaskType")


class ProjectTask(Base):
    """Which Task Types are valid for a given Project/Employer (Ganesh,
    2026-08-27 — "for each task the task should be different in projects").
    Unlike ProjectAssignment/TaskAssignment above (advisory-only, never
    blocks), this one IS enforced: app/validation.py's
    task_allowed_for_project() rejects a TaskEntry/PlannedTask whose
    project+task pair isn't either linked here or, for a task with NO
    links at all, unrestricted (see that function's docstring for exactly
    why "zero links = every project" rather than "zero links = no
    project"). Many-to-many by design — the same task name can be linked
    to several different projects (a client's "PWD JD" task and a
    different client's "PWD JD" task both point at the one TaskType row;
    they don't need separate rows unless the names themselves differ),
    and a project can have as many linked tasks as it needs.

    Existing tasks/projects created before this feature has zero rows
    here for every one of them — that's what preserves today's "any task,
    any project" behavior until an admin (via Lists -> a task's "Manage
    projects" action, or the Tasks bulk-upload sheet's new Project Name
    column) deliberately narrows a task down to specific project(s). This
    is a one-way narrowing an admin opts into per task, not a migration
    this feature runs automatically — nobody can guess which of ~38 tasks
    belongs to which of ~300 real projects except the admin."""

    __tablename__ = "project_tasks"
    __table_args__ = (UniqueConstraint("project_id", "task_type_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    task_type_id: Mapped[int] = mapped_column(ForeignKey("task_types.id"), index=True)
    created_by: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)

    project = relationship("Project")
    task_type = relationship("TaskType")


class ProjectDepartment(Base):
    """Which departments may access a given Project (Ganesh, 2026-08-28 —
    "give department name then only that department employees can able
    to access that particular projects and tasks under that project").
    Many-to-many by design, same "one row per pair, not a comma-separated
    cell" shape ProjectTask above uses — a project can span more than one
    department (a shared client, say), so it can have several rows here,
    one per department.

    A project with ZERO rows here is unrestricted — every department can
    see and use it — same "zero links = unrestricted" convention
    ProjectTask already established for tasks (see that model's
    docstring); this is what keeps every one of the ~300 existing
    projects working exactly as before, since none of them have any
    department rows yet and nobody is required to add any.

    This is a NEW, stricter layer than ProjectAssignment above — that one
    stays purely advisory (sorts/stars a project in the picker, never
    blocks). ProjectDepartment actually gates which projects even appear
    in an employee's Today page Project dropdown (see
    _visible_projects_and_tasks() in app/routes/employee.py) AND is
    enforced server-side too (validation.project_allowed_for_department(),
    called from validate_entry() and add_plan()) — the identical
    dual-layer precedent task_allowed_for_project()/ProjectTask already
    set, just one level up (project, not task) and keyed by department
    instead of an explicit task+project pair.

    Tasks scoped to a department-restricted project via ProjectTask
    automatically inherit the same restriction — nothing extra needed
    here, since an employee can never reach that project's Task dropdown
    in the first place if the Project itself is already off-limits to
    their department.

    `department` is a plain free-text string, matching Employee.department
    itself — this app has no canonical department table anywhere (see
    reports.departments_list()/admin dashboard()/roster()'s own inlined
    `{e.department or "—" for e in ...}` pattern); department names are
    just whatever an admin already typed into an employee's profile.
    Managed from the Lists page's per-project "Manage departments" panel
    (Super-Admin-only, same tier as Rename/Deactivate/the task-linking
    panel — see Project.is_case_type's docstring for that precedent) or
    via the Projects bulk-upload sheet's optional Department column.

    Deliberately does NOT change who can see/manage a project on the
    Lists admin page itself (Ganesh, 2026-08-28, explicit answer: "Employee-
    facing only") — a department-scoped admin still sees and can add every
    project on Lists regardless of this table; only a regular employee's
    Today page picker and entry validation are affected."""

    __tablename__ = "project_departments"
    __table_args__ = (UniqueConstraint("project_id", "department"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    department: Mapped[str] = mapped_column(String(120), index=True)
    created_by: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)

    project = relationship("Project")


# Feature-usage tracking (Ganesh, 2026-08-21, "as a developer I want to
# know how many people are using what option") — which of the 3 ways a
# TaskEntry row got created. Set at each of the three creation call sites
# in app/routes/employee.py (add_entry, _finish_task_timer) going FORWARD
# ONLY from the day this shipped — every historical/imported row and
# every row logged before this change stays NULL, meaning "unknown," not
# "manual". app/reports.py's feature_usage_report() treats NULL as
# excluded from every method's count rather than lumping it into
# ENTRY_METHOD_MANUAL, so old data never silently inflates (or deflates)
# the adoption percentages.
ENTRY_METHOD_PLAN = "plan"
ENTRY_METHOD_AUTO_TIMER = "auto_timer"
ENTRY_METHOD_MANUAL = "manual_add"
ENTRY_METHODS = (ENTRY_METHOD_PLAN, ENTRY_METHOD_AUTO_TIMER, ENTRY_METHOD_MANUAL)
ENTRY_METHOD_LABELS = {
    ENTRY_METHOD_PLAN: "Plan for the Day",
    ENTRY_METHOD_AUTO_TIMER: "Auto time capture",
    ENTRY_METHOD_MANUAL: "Add Row",
}


class TaskEntry(Base):
    __tablename__ = "task_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    task_type_id: Mapped[int] = mapped_column(ForeignKey("task_types.id"))
    details: Mapped[str] = mapped_column(Text, default="")
    # Case Type / Client (Ganesh, 2026-08-28) — see Project.is_case_type's
    # docstring for the full feature. Free text, one field (not split into
    # company/individual) per Ganesh's own answer: either an individual's
    # name, or "Company Name - Beneficiary Name". Blank/unused for every
    # row logged against a non-case-type project, which is most of them —
    # same "blank means not applicable" convention as
    # LeaveRecord.relation (Bereavement-only). Required at entry time (not
    # here — enforced in app/routes/employee.py's add_entry/
    # start_task_timer/add_plan, not in app/validation.py's validate_entry,
    # since this is a simple presence check tied to which project was
    # picked, not a PRD §4 entry rule like overlap/gap/cap/backdate).
    client: Mapped[str] = mapped_column(Text, default="")
    start_minute: Mapped[int] = mapped_column(Integer)  # minutes since midnight
    end_minute: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    # marks rows migrated from legacy Task Summary files (not validated to new rules)
    imported: Mapped[bool] = mapped_column(Boolean, default=False)
    # See ENTRY_METHOD_* above — one of ENTRY_METHODS, or NULL for any row
    # created before 2026-08-21 (imported or app-created, doesn't matter).
    entry_method: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    employee = relationship("Employee", back_populates="entries")
    project = relationship("Project")
    task_type = relationship("TaskType")

    @property
    def duration_minutes(self) -> int:  # computed, never typed (PRD §4)
        return self.end_minute - self.start_minute


BREAK_LUNCH_DINNER = "Lunch/Dinner"
BREAK_PERSONAL = "Personal"
BREAK_TYPES = (BREAK_LUNCH_DINNER, BREAK_PERSONAL)


class BreakEntry(Base):
    """An explicit Start Break / End Break span. Purely additive: break
    minutes were already excluded from the day's total before this existed,
    since day_total_minutes only sums logged TaskEntry rows and a break was
    just an unlogged gap between them. This model exists for the live-timer
    UX and so a gap the employee explained with a break doesn't also get
    flagged as an unexplained gap (see validation.gap_flags callers).

    break_type gates a business rule enforced in app/routes/employee.py:
    Lunch/Dinner is allowed once per day; Personal may be taken repeatedly."""

    __tablename__ = "break_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    break_type: Mapped[str] = mapped_column(String(40), default=BREAK_PERSONAL)
    start_minute: Mapped[int] = mapped_column(Integer)  # minutes since midnight
    end_minute: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # None while running
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    ended_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    # Optional employee note (Ganesh, 2026-08-14) — the auto-added "General /
    # Break" row on Today (see app/routes/employee.py's _BreakLogRow) only
    # ever showed the fixed break_type; this is editable the same way a
    # TaskEntry's details are, via /breaks/{id}/edit. Nullable, not just
    # default="", since app/db.py's additive-migration guard never backfills
    # existing rows on a live database (adds the column as NULL) — every
    # read site treats `details or ""`/`if details` so a pre-existing break
    # row with no note reads identically to a brand-new one with a blank
    # note; no dedicated ensure_* backfill needed.
    details: Mapped[Optional[str]] = mapped_column(Text, nullable=True, default="")

    employee = relationship("Employee")

    @property
    def duration_minutes(self) -> Optional[int]:
        return None if self.end_minute is None else self.end_minute - self.start_minute


class PunchSession(Base):
    """Punch In / Punch Out — a personal, live countdown-to-target widget on
    Today (Ganesh, 2026-07-30). Deliberately NOT read by app/engine.py or
    anything compliance-related: actual_minutes/strikes/variance still come
    only from logged TaskEntry rows, exactly as before. This table exists
    purely so the live timer survives a page refresh; the countdown math
    itself reuses the existing break-excess-extends-target rule (see
    app/routes/employee.py's _day_context) rather than inventing a second
    one, so what the employee watches tick down always matches what
    Submit Day would actually compute.

    Same open/closed pattern as BreakEntry: punched_out_at is None while a
    session is running. Employees can punch in/out more than once a day
    (e.g. stepping away outside a logged break) — completed sessions are
    just summed."""

    __tablename__ = "punch_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    punched_in_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    punched_out_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)

    employee = relationship("Employee")

    @property
    def duration_minutes(self) -> Optional[int]:
        if self.punched_out_at is None:
            return None
        return int((self.punched_out_at - self.punched_in_at).total_seconds() // 60)


class ActiveTaskTimer(Base):
    """The one currently-running task timer for an employee (Ganesh,
    2026-08-01: "auto time capture... timer similar to break button").

    Deliberately a SEPARATE table from TaskEntry, not a nullable-end_minute
    row on TaskEntry itself: app/engine.py and the strike/day-status math
    only ever read *finished* TaskEntry rows, so keeping "what does a
    running timer mean to compliance" out of TaskEntry entirely means
    engine.py never has to answer that question — a timer only becomes
    real data (an ordinary TaskEntry, indistinguishable from one typed in
    by hand) the moment it's stopped. Same open/closed philosophy as
    BreakEntry/PunchSession, just closed by *creating* a row elsewhere
    instead of filling in an end column on itself.

    Exactly one active timer per employee (unique constraint on
    employee_id) — single-timer, not multi-timer (Ganesh, 2026-08-01).
    Starting a new one auto-stops and saves the previous one as a real
    TaskEntry first (see app/routes/employee.py start_task_timer) rather
    than allowing several to run at once."""

    __tablename__ = "active_task_timers"
    __table_args__ = (UniqueConstraint("employee_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    date: Mapped[dt.date] = mapped_column(Date)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    task_type_id: Mapped[int] = mapped_column(ForeignKey("task_types.id"))
    details: Mapped[str] = mapped_column(Text, default="")
    # Case Type / Client (Ganesh, 2026-08-28) — see TaskEntry.client's
    # docstring. Carried the same way `details` already is: set at Start
    # (or copied from PlannedTask.client when a plan's Start/Resume opens
    # this timer — see start_plan below), optionally topped up at Stop,
    # and copied verbatim into TaskEntry.client the moment
    # _finish_task_timer() closes this timer into a real row.
    client: Mapped[str] = mapped_column(Text, default="")
    # Business-timezone clock-face minute at Start (util.now_local(), NOT
    # started_at below) — same split BreakEntry/PunchSession already use:
    # this is what becomes TaskEntry.start_minute when the timer stops, so
    # it must be expressed the same fixed BUSINESS_TZ every other minute
    # value in this app is (see util.py's BUSINESS_TZ / no-per-employee-
    # timezones rule). started_at is a full UTC timestamp, kept only so the
    # live count-up widget survives a page refresh — it is NEVER read for
    # the minute-of-day value.
    start_minute: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    # Task Planning (Ganesh, 2026-08-21) — set when this running segment was
    # started FROM a planned row (Today's Plan's Start/Resume button)
    # rather than the ad-hoc Auto time capture form; NULL for every ad-hoc
    # timer, exactly as before. Nullable/additive: existing rows and every
    # ad-hoc Start Timer keep behaving identically. See PlannedTask below —
    # this is the only schema link between the two; TaskEntry itself never
    # needs to know a row came from a plan (see PlannedTask's docstring).
    planned_task_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("planned_tasks.id"), nullable=True
    )

    employee = relationship("Employee")
    project = relationship("Project")
    task_type = relationship("TaskType")
    planned_task = relationship("PlannedTask")

    @property
    def elapsed_minutes(self) -> int:
        return int((dt.datetime.utcnow() - self.started_at).total_seconds() // 60)


PLAN_PLANNED = "planned"
PLAN_RUNNING = "running"
PLAN_PAUSED = "paused"
PLAN_DONE = "done"
PLAN_STATUSES = (PLAN_PLANNED, PLAN_RUNNING, PLAN_PAUSED, PLAN_DONE)


class PlannedTask(Base):
    """"Plan for the Day" (Ganesh, 2026-08-21): an employee picks a Project/
    Task and a short plan note *before* working on it, then works it with
    Start / Pause / Resume / Stop instead of one uninterrupted Start/Stop.

    Deliberately does NOT introduce a "worked time spans a pause" concept
    into TaskEntry at all — every Start-to-Pause (or Start-to-Stop) segment
    is finished through the exact same `_finish_task_timer()` an ad-hoc
    Auto time capture timer already uses (app/routes/employee.py), which
    creates one ordinary, independent TaskEntry row per segment. A task
    paused over a meeting and resumed after therefore shows as two normal
    rows in the log, each with its own real start/end clock time — not one
    row with a mysterious internal gap. This is why nothing in
    app/engine.py or app/validation.py needed to change: every segment is,
    to the rest of the app, indistinguishable from a plain hand-typed row,
    so the existing overlap/4-hour-cap/day-total/strike math already
    applies to it correctly with zero new cases to reason about.

    status walks planned -> running -> paused -> ... -> done:
      - planned: added, never started yet. Editable (project/task/details),
        deletable.
      - running: the currently-open segment is this plan's (see
        ActiveTaskTimer.planned_task_id) — at most one row across an
        employee's WHOLE day can be `running` at a time, same single-
        active-timer rule Auto time capture already enforces (the shared
        ActiveTaskTimer table's own UniqueConstraint("employee_id")).
      - paused: was running, got paused (or got auto-finished because the
        employee started a different plan/timer without pausing first —
        same "starting a new one auto-stops the old one" convention
        start_task_timer already uses). Its segments-so-far are already
        real TaskEntry rows; Resume opens a fresh one.
      - done: explicitly stopped for good. Read-only from here.

    created_by_employee_id (nullable) distinguishes a self-planned row from
    one an admin/team lead planned for someone else. Was added ahead of
    time with no route using it yet; TK-04 (Ganesh, 2026-08-28 — "Admin
    creates a project/task in an employee log") is that screen: Person
    Detail's new "Assigned tasks" card (`app/routes/admin.py`'s
    `admin_add_plan()`/`admin_edit_plan()`/`admin_delete_plan()`) sets this
    to the ADMIN's id, which is what the employee-facing "assigned by"
    badge (today.html), the assignment-notification banner
    (`assigned_notified_at` below), and `delete_plan()`'s "an employee
    can't delete an assigned entry" rule all key off — `created_by_employee_id
    != employee_id` means assigned; `== employee_id` (the existing
    self-planned case) means it isn't. No route change was needed for the
    employee to actually WORK an assigned plan — start_plan()/pause_plan()/
    stop_plan() already only ever check `plan.employee_id`, never who
    created it, so Start/Pause/Resume/Stop behave identically either way."""

    __tablename__ = "planned_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    task_type_id: Mapped[int] = mapped_column(ForeignKey("task_types.id"))
    details: Mapped[str] = mapped_column(Text, default="")
    # Case Type / Client (Ganesh, 2026-08-28) — see TaskEntry.client's
    # docstring. Captured once, at add_plan() time (same as Project/Task
    # themselves — not editable afterward via edit_plan(), which stays
    # scoped to just the plan text; delete-and-re-add is the path for a
    # wrong Client, same precedent Project/Task already set). Copied
    # verbatim into ActiveTaskTimer.client every time this plan's Start/
    # Resume opens a fresh segment (see start_plan below), so it flows
    # through to every TaskEntry that segment produces with no repeated
    # typing.
    client: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default=PLAN_PLANNED)
    created_by_employee_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("employees.id"), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    # Set once this specific plan has been auto-carried to a fresh
    # PLAN_PLANNED row on the next day because it was still `planned`/
    # `paused` at Submit Day (Ganesh, 2026-08-22 — see submit_day() in
    # app/routes/employee.py). NULL on every plan that's never been
    # through that path (which is all of them before this column existed,
    # and every one that finished the same day it was made). Deliberately
    # a separate marker rather than changing `status` when carried — the
    # original row's status stays the honest, unmutated record of what
    # actually happened that day (never-rewrite-frozen-history, same
    # instinct as DayStatus.source='imported' elsewhere in this app); this
    # column exists purely to stop a day getting resubmitted after an
    # admin unlock from copying the same unfinished plan to tomorrow twice.
    carried_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    # TK-04 (Ganesh, 2026-08-28) — set once the "an admin assigned you a
    # task" banner (today.html, via _pending_plan_assignment_notices() in
    # app/routes/employee.py) has been shown/dismissed. Same "notify once,
    # dismiss marks just that one" pattern Project/TaskType's
    # employee_notified_at already established for the "an admin rewrote
    # your suggestion" banner. NULL on every plan created before this
    # column existed and on every ordinary self-planned row (nothing ever
    # sets it for those, since _pending_plan_assignment_notices() only
    # looks at rows where created_by_employee_id != employee_id in the
    # first place) — never backfilled, same as every other NULL-is-
    # "doesn't apply"/"not yet" column in this app.
    assigned_notified_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    # Estimated time (Ganesh, 2026-08-31 — "add an estimated time for each
    # planned task"), integer minutes per this app's hard rule (no floats,
    # no separate hour/minute fields — same convention as every other
    # duration in this app, formatted for display via the `hm` Jinja
    # filter). Nullable, not defaulted to 0: None means "no estimate given"
    # (an employee can still add a plan without one — this was never made
    # required), which every read treats as plain falsy (`{% if
    # p.estimated_minutes %}`), same "NULL is a correct, permanent answer"
    # reasoning as Employee.is_developer/Project.is_case_type above. Purely
    # informational — nothing in app/engine.py or app/validation.py reads
    # it, and it plays no part in compliance math, strikes, or the 4h/day
    # cap; it exists only to show up next to a planned item on Today's Plan
    # (employee view) and on the admin Assign Work / Person Detail "Assigned
    # tasks" cards, per Ganesh's own scoping answer for where this should
    # display.
    estimated_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    employee = relationship("Employee", foreign_keys=[employee_id])
    # TK-04 — who created this plan, when it wasn't the employee themself.
    # A second relationship to the same Employee table needs its own
    # foreign_keys= disambiguation, same as `employee` above already needs
    # for employee_id vs this column both pointing at employees.id.
    assigned_by = relationship("Employee", foreign_keys=[created_by_employee_id])
    project = relationship("Project")
    task_type = relationship("TaskType")


class DaySubmission(Base):
    __tablename__ = "day_submissions"
    __table_args__ = (UniqueConstraint("employee_id", "date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    total_minutes: Mapped[int] = mapped_column(Integer, default=0)  # computed
    submitted_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    locked: Mapped[bool] = mapped_column(Boolean, default=True)
    unlock_count: Mapped[int] = mapped_column(Integer, default=0)
    # AI day summary (Ganesh, 2026-08-31) — a 3-4 line LLM-generated summary
    # of that day's task rows, generated once at Submit Day time (see
    # app/llm_summary.py + submit_day() in app/routes/employee.py) and
    # shown ONLY on the admin Task Logs report (reports.daily_task_log_
    # report() prefers this over rule_based_day_summary() when present).
    # Backend swapped twice more on 2026-09-02: Gemini -> self-hosted Ollama
    # -> Groq's hosted API (Ganesh wanted zero infrastructure cost, and
    # Ollama needed a paid always-on host to be reachable from Cloud Run) —
    # see app/llm_summary.py's own docstring for the full reasoning and
    # Groq's no-training-on-any-tier data policy. This column is backend-
    # agnostic through all of it, no schema/migration impact from any swap.
    # Nullable with no backfill needed — unlike TaskType.category or
    # similar additive columns elsewhere in this file, None here is a
    # permanently correct answer for any day submitted before this feature
    # existed (and for any day where the call failed or the LLM backend
    # isn't configured), not a gap to fill in later. summary_error mirrors the old
    # (now-deleted) TaskDaySummary.error's own reasoning: store WHY a
    # summary is missing instead of silently retrying the same failing
    # call on every report view. summary_generated_at is a plain UTC audit
    # timestamp (elapsed-time value, not a clock-face one — see the
    # BUSINESS_TZ hard rule), so an admin/dev can tell how stale a stored
    # summary is.
    summary_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    summary_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    summary_generated_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)


LEAVE_REQUESTED = "requested"
LEAVE_APPROVED = "approved"
LEAVE_REJECTED = "rejected"

# Multilevel approval (Ganesh, 2026-09-01) — a department-scoped admin
# ("Team Lead") reviews a request FIRST (accept or deny, always with a
# reason), then a Super Admin makes the actual final call (status above
# still only ever flips to LEAVE_APPROVED/LEAVE_REJECTED via that final
# decision — engine.py/validation.py read nothing new here, this is purely
# an extra recommendation stage in front of the same status field that
# already existed). Reused across LeaveRecord, OvertimeApproval, and
# CompensationLink below, same "generic status strings, not a new enum per
# table" precedent LEAVE_REQUESTED/LEAVE_APPROVED/LEAVE_REJECTED already
# set (see UnlockRequest's own docstring for that precedent).
LEAD_ACCEPTED = "accepted"
LEAD_DENIED = "denied"
LEAD_DECISIONS = (LEAD_ACCEPTED, LEAD_DENIED)


class UnlockRequest(Base):
    """Employee-initiated "please unlock this locked day" request (Ganesh,
    2026-08-27) — before this, the only way to ask for an unlock was
    outside the app entirely (Teams/email/in person), and an admin had no
    signal one was needed short of being told directly. Reuses the same
    generic LEAVE_REQUESTED/LEAVE_APPROVED/LEAVE_REJECTED status strings
    every other employee-request-then-admin-decides flow in this app
    already does (see CompensationLink for the same reuse-not-a-new-enum
    precedent, despite the "LEAVE_" name) rather than inventing a fourth
    parallel status enum.

    This is deliberately a queue/notification layer in front of the
    EXISTING unlock mechanism, not a second way to unlock a day —
    approving one doesn't happen here. `unlock_day()` (app/routes/
    admin.py, unchanged in its own logic) auto-resolves any pending
    request for that employee+date to LEAVE_APPROVED the moment an admin
    actually unlocks the day, whether they arrived via this queue or just
    unlocked directly from Person Detail without noticing a request
    existed — one code path decides "unlocked or not", this table never
    gets to disagree with it. A super admin can also explicitly reject a
    request without unlocking (reject_unlock_request()) — e.g. the day's
    fine as-is, or the correction should happen a different way."""

    __tablename__ = "unlock_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    note: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default=LEAVE_REQUESTED)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    reviewed_by: Mapped[str] = mapped_column(String(120), default="")
    reviewed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    review_note: Mapped[str] = mapped_column(Text, default="")

    employee = relationship("Employee")


class LeaveRecord(Base):
    __tablename__ = "leave_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    start_date: Mapped[dt.date] = mapped_column(Date, index=True)
    end_date: Mapped[dt.date] = mapped_column(Date)  # inclusive; == start_date for one day
    type: Mapped[str] = mapped_column(String(40), default="Other")
    # None => full day (defaults to the person's daily target that day)
    minutes_per_day: Mapped[int] = mapped_column(Integer, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    entered_by: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    imported: Mapped[bool] = mapped_column(Boolean, default=False)
    # default 'approved' is deliberate: every pre-existing admin-entered and
    # imported leave row was already an approved fact (PRD open question 5's
    # original "admin enters everything" default) — only self-service
    # requests start life as 'requested'. engine.leave_minutes_on() only
    # counts 'approved' rows, so this default keeps all prior behavior
    # (including the frozen imported history) unchanged.
    status: Mapped[str] = mapped_column(String(20), default=LEAVE_APPROVED)
    reviewed_by: Mapped[str] = mapped_column(String(120), default="")
    reviewed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    review_note: Mapped[str] = mapped_column(Text, default="")
    # Leave Management V2 (Ganesh, 2026-08-21), both additive/nullable:
    # relation: which family member Bereavement Time is for (requirement
    # 7) — only meaningful when type == LEAVE_BEREAVEMENT, NULL/blank for
    # every other type and every pre-existing row.
    relation: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    # approved_minutes_per_day: NULL until a decision is made; set at
    # approve time from the admin's editable "Hours approved" field
    # (requirement 6 — partial approval). May be LESS than
    # minutes_per_day — that's the whole point of a partial approval —
    # but is never used to widen a request past what was asked for.
    # engine.leave_balance_v2()'s "used" sums THIS column, not
    # minutes_per_day, so a partial approval is reflected correctly;
    # review_note carries the "why partial" explanation and is already a
    # plain-text field above, no new column needed for that half of it.
    approved_minutes_per_day: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Multilevel approval (Ganesh, 2026-09-01) — see LEAD_ACCEPTED/
    # LEAD_DENIED's own comment above for the shared design. requires_lead_
    # review defaults True at the ORM level (every NEW request from here on
    # needs a Team Lead's say before Super Admin's final decision), but a
    # brand-new column added via SQLite/Postgres ALTER TABLE never backfills
    # existing rows to that default (see feedback_timekeeping_sqlite_add_
    # column_gap) — util.ensure_lead_review_backfill() sets it False on
    # every row that existed before this feature shipped, which is exactly
    # the grandfathering Ganesh asked for: old pending requests keep the old
    # single-step flow, only new ones go through Team Lead review first.
    # leave_add() (admin-direct entry) also explicitly sets this False —
    # nobody requested it, so there's nothing for a lead to review.
    requires_lead_review: Mapped[bool] = mapped_column(Boolean, default=True)
    lead_decision: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    lead_reason: Mapped[str] = mapped_column(Text, default="")
    lead_reviewed_by: Mapped[str] = mapped_column(String(120), default="")
    lead_reviewed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)

    employee = relationship("Employee")

    def covers(self, d: dt.date) -> bool:
        return self.start_date <= d <= self.end_date


SUPPORT_OPEN = "open"
SUPPORT_RESOLVED = "resolved"
SUPPORT_STATUSES = (SUPPORT_OPEN, SUPPORT_RESOLVED)


class SupportQuery(Base):
    """An employee's question/issue submitted from the Support page and an
    admin's reply. Brand-new table, same shape as the leave request flow
    (submit -> admin queue -> admin acts on it) — no historical data to
    worry about, so this is purely additive."""

    __tablename__ = "support_queries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    status: Mapped[str] = mapped_column(String(20), default=SUPPORT_OPEN)
    admin_reply: Mapped[str] = mapped_column(Text, default="")
    resolved_by: Mapped[str] = mapped_column(String(120), default="")
    resolved_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)

    employee = relationship("Employee")


OT_REQUESTED = "requested"
OT_APPROVED = "approved"
OT_REJECTED = "rejected"


class OvertimeApproval(Base):
    """Pre-approval for working overtime on a specific date range (Ganesh's
    manager, 2026-08-03) — same submit -> lead/admin queue -> lead/admin acts
    shape as LeaveRecord and SupportQuery above, deliberately, so it needs no
    new patterns anywhere else in the app.

    Three ways a row here ends up 'approved':
      1. Employee requests a range themselves (status starts 'requested',
         awaits review) — app/routes/employee.py's /overtime routes.
      2. A Lead/Admin grants a range proactively, before it's worked (e.g. a
         known busy season) — created already 'approved', no employee
         request needed.
      3. A Lead/Admin grants a range retroactively, after it's worked (e.g.
         reviewing at month-end who should get paid overtime) — same as #2,
         start/end are just in the past. Nothing here cares which direction
         time runs; a date range is a date range.
    In cases #2/#3 reviewed_by/reviewed_at are set immediately, same as
    LeaveRecord's admin-direct-entry (leave_add) does.

    Deliberately does NOT block or gate anything: employees can log time and
    use Punch In/Out regardless of approval status (Ganesh: "I still want to
    use overtime, even unapproved, to compensate for missed time" — approval
    here is a payroll-visibility label, not a permission system baked into
    engine.py/validation.py). See app/reports.py's attendance_report() for
    where 'approved overtime' is surfaced next to raw overtime.

    Who can act on a given employee's requests: whoever that employee's
    Employee.reports_to is, IF that person is_admin (see app/auth.py
    led_by()) — same per-person "Team Lead" concept reports_to_id already
    carries, now with actual teeth for this one purpose. An employee with no
    admin reports_to (nobody assigned, or their reports_to isn't an admin)
    simply doesn't show up in any Lead's led_by() scope, so only Super
    Admins (unscoped, see everything) can act on it — the "unassigned
    employees route to Super Admin" fallback the manager asked for falls out
    of the existing scoping pattern for free, no special-casing needed."""

    __tablename__ = "overtime_approvals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    start_date: Mapped[dt.date] = mapped_column(Date, index=True)
    end_date: Mapped[dt.date] = mapped_column(Date)  # inclusive; == start_date for one day
    note: Mapped[str] = mapped_column(Text, default="")  # employee's reason, or lead's note
    requested_by: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    status: Mapped[str] = mapped_column(String(20), default=OT_REQUESTED)
    reviewed_by: Mapped[str] = mapped_column(String(120), default="")
    reviewed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    review_note: Mapped[str] = mapped_column(Text, default="")
    # Multilevel approval (Ganesh, 2026-09-01) — same shape/reasoning as
    # LeaveRecord's own columns above; see LEAD_ACCEPTED's comment. Also
    # marks a real scoping change for this table specifically: the
    # department-scoped Team Lead who reviews this stage is determined by
    # admin_department_scope() (department string match), NOT led_by()
    # (per-person reports_to) the way this table's admin-facing scoping
    # worked before — Ganesh confirmed "department-based, for both" so
    # Leave and Overtime use one consistent reviewer-scoping rule. led_by()
    # itself is untouched/still defined (app/auth.py) in case something
    # else wants per-person reporting-line scope later, it's just no longer
    # what decides who reviews an Overtime request. overtime_grant()
    # (admin-direct entry) explicitly sets this False, same as leave_add().
    requires_lead_review: Mapped[bool] = mapped_column(Boolean, default=True)
    lead_decision: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    lead_reason: Mapped[str] = mapped_column(Text, default="")
    lead_reviewed_by: Mapped[str] = mapped_column(String(120), default="")
    lead_reviewed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)

    employee = relationship("Employee")

    def covers(self, d: dt.date) -> bool:
        return self.start_date <= d <= self.end_date


# ---- Ticketing System (Ganesh, 2026-08-06) ----------------------------------
# Internal bug/enhancement tracker, reached from the Support page (not a new
# top-level nav item — two links added to support.html/admin/support.html).
# Visibility, per Ganesh's answer to the clarifying question: everyone who
# can log in (Employee/Admin/Super Admin alike) can raise a ticket, view the
# full org-wide list, and comment. Only Developers (Employee.is_developer,
# a role independent of is_admin — see above) can change a ticket's status.
# That means every route below only needs `current_user`, except the one
# status-change route, which needs `require_developer` (app/auth.py).
TICKET_BUG = "bug"
TICKET_ENHANCEMENT = "enhancement"
TICKET_NEW_FEATURE = "new_feature"
TICKET_TYPES = (TICKET_BUG, TICKET_ENHANCEMENT, TICKET_NEW_FEATURE)
TICKET_TYPE_LABELS = {
    TICKET_BUG: "Bug", TICKET_ENHANCEMENT: "Enhancement", TICKET_NEW_FEATURE: "New Feature",
}

TICKET_LOW = "low"
TICKET_MEDIUM = "medium"
TICKET_HIGH = "high"
TICKET_URGENT = "urgent"
TICKET_PRIORITIES = (TICKET_LOW, TICKET_MEDIUM, TICKET_HIGH, TICKET_URGENT)
TICKET_PRIORITY_LABELS = {
    TICKET_LOW: "Low", TICKET_MEDIUM: "Medium", TICKET_HIGH: "High", TICKET_URGENT: "Urgent",
}
# Sort key so ticket lists can show Urgent first without a raw string sort
# putting "high" before "low" before "urgent" alphabetically.
TICKET_PRIORITY_RANK = {TICKET_URGENT: 0, TICKET_HIGH: 1, TICKET_MEDIUM: 2, TICKET_LOW: 3}

TICKET_OPEN = "open"
TICKET_IN_PROGRESS = "in_progress"
TICKET_RESOLVED = "resolved"
TICKET_CLOSED = "closed"
TICKET_STATUSES = (TICKET_OPEN, TICKET_IN_PROGRESS, TICKET_RESOLVED, TICKET_CLOSED)
TICKET_STATUS_LABELS = {
    TICKET_OPEN: "Open", TICKET_IN_PROGRESS: "In Progress",
    TICKET_RESOLVED: "Resolved", TICKET_CLOSED: "Closed",
}


class Ticket(Base):
    """A bug/enhancement/new-feature ticket raised from the Support page.
    Deliberately separate from SupportQuery above — that's a free-text
    question-and-reply to an admin; this is a structured, developer-worked
    item with type/priority/status and a comment thread."""

    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    subject: Mapped[str] = mapped_column(String(200), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    ticket_type: Mapped[str] = mapped_column(String(20), default=TICKET_BUG)
    priority: Mapped[str] = mapped_column(String(20), default=TICKET_MEDIUM)
    status: Mapped[str] = mapped_column(String(20), default=TICKET_OPEN, index=True)
    # filename only (e.g. "14.jpg"), not a full path — stored under
    # TICKET_ATTACHMENT_DIR (app/routes/tickets.py), same env-overridable
    # on-disk pattern as Employee.photo_path/AVATAR_DIR. One attachment per
    # ticket; NULL if none was attached. Named after the ticket's own id
    # (not the employee's, since one employee can raise several tickets),
    # so the row must be flushed for its id before the file is written.
    attachment_path: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    resolved_by: Mapped[str] = mapped_column(String(120), default="")
    resolved_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)

    employee = relationship("Employee", foreign_keys=[employee_id])
    comments = relationship(
        "TicketComment", back_populates="ticket", cascade="all, delete-orphan",
        order_by="TicketComment.created_at",
    )


class TicketComment(Base):
    __tablename__ = "ticket_comments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), index=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"))
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)

    ticket = relationship("Ticket", back_populates="comments")
    employee = relationship("Employee")


class Holiday(Base):
    """Company holiday calendar — one shared list, common to every employee
    regardless of location (Ganesh, 2026-08-14). Briefly split per-country
    on 2026-08-12 (US and India each with their own calendar), reverted the
    same week: the team decided holidays should just be common to everyone,
    so there's no separate US/India view anywhere in the UI or bulk upload
    anymore — see engine.holidays_set(), which now always returns every row
    unscoped.

    The `location` column and its (date, location) unique constraint are
    still here even though nothing reads `location` anymore — dropping a
    column/constraint isn't an additive change, and this app has no
    alembic-style migration path once real employees have real data (see
    CLAUDE.md's schema-change rule), so removing them isn't safe to do
    casually. New rows just get DEFAULT_LOCATION ("India") written
    invisibly by whatever creates them; since every write now uses the same
    value, (date, location) behaves exactly like a plain unique-by-date
    constraint in practice. Safe to actually drop both in a future
    migration window if this table's shape ever needs cleaning up."""

    __tablename__ = "holidays"
    __table_args__ = (UniqueConstraint("date", "location"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date)
    name: Mapped[str] = mapped_column(String(200), default="")
    location: Mapped[str] = mapped_column(String(20), default=DEFAULT_LOCATION)


# TaskDaySummary (the LLM-generated day-summary cache) was removed
# 2026-08-29 — the Task Logs report's daily summary is now
# reports.rule_based_day_summary(), a pure/deterministic function computed
# fresh on every view (see app/reports.py's daily_task_log_report()), so
# there is nothing left to cache and no error state to store. A pre-existing
# `task_day_summaries` table on an already-deployed host is simply orphaned
# — harmless, never read, safe to drop by hand or leave alone.


class DayStatus(Base):
    __tablename__ = "day_statuses"
    __table_args__ = (UniqueConstraint("employee_id", "date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(20))
    actual_minutes: Mapped[int] = mapped_column(Integer, nullable=True)
    target_minutes: Mapped[int] = mapped_column(Integer, nullable=True)
    # None => unknown (imported rows without legacy extra/short data)
    variance_minutes: Mapped[int] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(12), default="computed")  # computed|imported
    imported_token: Mapped[str] = mapped_column(String(200), default="")  # raw legacy cell
    # legacy pre-policy days (before the sheet's own COUNTIF range start,
    # e.g. before 15-Apr-2026): true status kept, but never counted as a strike
    strike_exempt: Mapped[bool] = mapped_column(Boolean, default=False)
    compensated: Mapped[bool] = mapped_column(Boolean, default=False)
    override_status: Mapped[str] = mapped_column(String(20), nullable=True)
    override_reason: Mapped[str] = mapped_column(Text, default="")
    override_by: Mapped[str] = mapped_column(String(120), default="")
    override_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=True)
    computed_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)

    employee = relationship("Employee")

    def effective_status(self, comp_erases_strike: bool = True) -> str:
        """Override wins; else a fully compensated shortfall reads Complete
        (PRD §6, open question 3 default). Base status stays for the audit."""
        if self.override_status:
            return self.override_status
        if self.compensated and comp_erases_strike and self.status in STRIKE_STATUSES:
            return COMPLETE
        return self.status


class CompensationLink(Base):
    __tablename__ = "compensation_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    shortfall_date: Mapped[dt.date] = mapped_column(Date)
    surplus_dates: Mapped[str] = mapped_column(Text, default="[]")  # JSON list of ISO dates
    # Partial allocation (Ganesh, 2026-08-25) — how many minutes of EACH
    # surplus day in surplus_dates this particular link actually consumed,
    # as a JSON {"YYYY-MM-DD": minutes} dict. Before this, a link always
    # consumed a surplus day's *entire* variance and blocked it from ever
    # being reused, even if the shortfall only needed part of it (see
    # engine.evaluate_link()/surplus_minutes_used_by_date()). A brand-new
    # nullable column with a default is picked up automatically by the
    # additive-migration guard in app/db.py — no rm tms.db needed. NOT
    # backfilled on purpose: an existing link created before this change has
    # "{}" here, and every read site (evaluate_link, surplus_minutes_used_by_
    # date, shortfall_total_allocated_minutes) falls back to the old
    # whole-day-sum behavior whenever this is empty but surplus_dates isn't
    # — that fallback IS the correct, frozen historical reading for those
    # rows, not a gap to fill in; don't add an ensure_* backfill for it.
    surplus_minutes: Mapped[str] = mapped_column(Text, default="{}")
    note: Mapped[str] = mapped_column(Text, default="")
    linked_by: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    fully_compensated: Mapped[bool] = mapped_column(Boolean, default=False)
    # Overtime-for-Missed-Hours, employee-requested (Ganesh, 2026-08-21,
    # requirement 9) — extends this existing feature rather than building a
    # second one, since "match a shortfall day against surplus days" is
    # exactly what a link already is; see
    # docs/LEAVE_MANAGEMENT_PLAN.md §3. status default is LEAVE_APPROVED,
    # matching LeaveRecord's own precedent: every pre-existing row here was
    # created directly by a SuperAdmin (app/routes/admin.py's
    # add_complink()), which is already-approved by definition — only a
    # NEW employee-submitted match request starts life as LEAVE_REQUESTED.
    # requested_by_employee distinguishes the two cases (True only for a
    # self-service request) so the admin queue knows which rows actually
    # need a decision instead of showing every historical admin-made link.
    status: Mapped[str] = mapped_column(String(20), default=LEAVE_APPROVED)
    requested_by_employee: Mapped[bool] = mapped_column(Boolean, default=False)
    reviewed_by: Mapped[str] = mapped_column(String(120), default="")
    reviewed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    review_note: Mapped[str] = mapped_column(Text, default="")
    # Multilevel approval (Ganesh, 2026-09-01) — same shape as LeaveRecord/
    # OvertimeApproval above; only meaningful for a requested_by_employee=
    # True row (an admin-direct add_complink() link is already-approved,
    # same "nobody requested it" reasoning as leave_add()/overtime_grant(),
    # so add_complink() sets this False). Department scope for the Team
    # Lead stage is the REQUESTING employee's department (admin_department_
    # scope()), matching Leave/Overtime's own reviewer-scoping rule — the
    # final approve/reject decision stays Super-Admin-only end to end,
    # unchanged (see approve_complink/reject_complink's own docstring).
    requires_lead_review: Mapped[bool] = mapped_column(Boolean, default=True)
    lead_decision: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    lead_reason: Mapped[str] = mapped_column(Text, default="")
    lead_reviewed_by: Mapped[str] = mapped_column(String(120), default="")
    lead_reviewed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)

    employee = relationship("Employee")


class SpecialPaidGrant(Base):
    """Special Paid Time, granted by management (requirement 8) — a ledger,
    not an entitlement column, since these hours are handed out one grant
    at a time (a reward, a one-off exception) rather than accrued like
    Planned Time. No separate approval step: a SuperAdmin granting the
    hours *is* the approval, same one-action-reason-required shape
    Compensation Links already use above (see docs/LEAVE_MANAGEMENT_PLAN.md
    §3 — "Recommended: no separate step"). engine.leave_balance_v2() sums
    this table's minutes as the Special Paid Time entitlement for that
    employee."""
    __tablename__ = "special_paid_grants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    minutes: Mapped[int] = mapped_column(Integer)  # integer minutes, CLAUDE.md hard rule
    reason: Mapped[str] = mapped_column(Text, default="")
    granted_by: Mapped[str] = mapped_column(String(120), default="")
    granted_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)

    employee = relationship("Employee")


class Config(Base):
    __tablename__ = "config"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(String(200), default="")


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(120), default="")
    action: Mapped[str] = mapped_column(String(60), index=True)
    entity: Mapped[str] = mapped_column(String(60), default="")
    entity_id: Mapped[str] = mapped_column(String(60), default="")
    detail: Mapped[str] = mapped_column(Text, default="")  # JSON: before/after/reason


# PRD §10 defaults — every open question is a config value
CONFIG_DEFAULTS = {
    "tolerance_minutes": "60",        # open question 2
    "strike_threshold": "5",          # open question 1
    "max_row_minutes": "240",         # §4 max single-row duration
    "backdate_working_days": "1",     # open question 7
    "gap_flag_minutes": "15",         # §4 gap flag
    "min_details_chars": "5",         # §4 details rule
    "comp_erases_strike": "1",        # open question 3
    "live_start_date": "",            # set by importer; engine computes from here on
    "max_break_minutes": "30",        # break time beyond this extends that day's target
    # Leave Management V2 (Ganesh, 2026-08-21) — thresholds instead of
    # hardcoded numbers, per CLAUDE.md's existing rule ("read via
    # engine.get_config(db), never hardcode thresholds"). Days/year, not
    # minutes/month — engine.py converts using each employee's own
    # daily_target_minutes (see docs/LEAVE_MANAGEMENT_PLAN.md §2's table).
    "probation_days_default": "180",  # 6 months (Norine, 2026-08-29) — was 90
    "planned_days_year_0_2": "9",      # 0-2 years' experience
    "planned_days_year_2_5": "11",     # 2-5 years' experience
    "planned_days_year_5_plus": "13",  # 5+ years' experience
    # Unplanned Time annual cap (Ganesh, 2026-08-27 — policy clarification
    # from management: unlike Unpaid/Bereavement Time, which stay
    # uncapped/available-on-request, Unplanned Time IS a real pool that
    # resets every calendar year, not accrued over tenure like Planned
    # Time). Hours/year, not minutes — see leave_balance_v2()'s docstring
    # in engine.py for how this is applied and why it resets on a
    # calendar-year boundary instead of running for an employee's whole
    # tenure the way Planned Time's entitlement does.
    "unplanned_hours_year_cap": "40",
    # Dashboard's "Compliance Trend" card (Ganesh, 2026-08-30, from a pasted
    # mockup showing a weekly line chart with a dashed "Target 90%" line) —
    # there was no existing notion of a compliance *rate* target anywhere in
    # this app before this (strike_threshold above is a per-person strike
    # COUNT, not an org-wide percentage), so per the hard rule above
    # ("never hardcode thresholds"), this is a new Config key rather than a
    # literal 90 baked into the template/JS. DB-default-only for now, same
    # as probation_days_default/planned_days_year_*/unplanned_hours_year_cap
    # above — not exposed on /admin/config; see
    # reports.compliance_trend_report()'s docstring for how it's used.
    "compliance_target_pct": "90",
    # Bank & statutory details toggle (Ganesh, 2026-09-03: "hide employements
    # details section as of now for everyone and just give an option in
    # settings where super admin can enable or disable... make it by default
    # disabled") — unlike every other on/off flag in this app (TICKETING_
    # ENABLED, HOLIDAY_MANAGEMENT_ENABLED, etc., all read from an env var in
    # app/templating.py and frozen at process start), this one is deliberately
    # a Config-table value, exposed as a real checkbox on /admin/config, since
    # Ganesh explicitly asked for a Super Admin to flip it at runtime without
    # a redeploy. Gates: profile.html's "Employment Details" card,
    # employment_details_page()/employment_details_save() in
    # app/routes/employee.py (404 while off, same convention
    # HOLIDAY_MANAGEMENT_ENABLED's routes already use), and admin/person.html's
    # read-only "Bank & statutory details" card — "for everyone" per Ganesh's
    # own wording, so this hides it from admins' read-only view too, not just
    # employee self-service. Existing EmployeeBankDetails rows already saved
    # are untouched either way — this only controls whether the section is
    # shown, never deletes or blocks what's already on file.
    "employment_details_enabled": "0",
}
