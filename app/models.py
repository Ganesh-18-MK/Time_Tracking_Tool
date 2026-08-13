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
    # Two-tier admin (Ganesh, 2026-07-31): is_admin alone now means
    # "department-scoped admin / team lead" — sees Dashboard, Leave
    # Requests, and Reports, filtered to their own Employee.department
    # only. is_super_admin=True is the org-wide tier that sees every
    # department and every other admin screen (Roster, Settings, Audit
    # Log, Support Inbox, Projects & Tasks, bulk uploads, person-detail
    # overrides/unlocks/compensation links) — see app/auth.py
    # require_super_admin and app/util.py ensure_super_admin_backfill
    # (existing admins are auto-promoted to super_admin on upgrade so
    # nobody who could see everything before this column existed loses
    # access silently).
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


class TaskEntry(Base):
    __tablename__ = "task_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    task_type_id: Mapped[int] = mapped_column(ForeignKey("task_types.id"))
    details: Mapped[str] = mapped_column(Text, default="")
    start_minute: Mapped[int] = mapped_column(Integer)  # minutes since midnight
    end_minute: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    # marks rows migrated from legacy Task Summary files (not validated to new rules)
    imported: Mapped[bool] = mapped_column(Boolean, default=False)

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

    employee = relationship("Employee")
    project = relationship("Project")
    task_type = relationship("TaskType")

    @property
    def elapsed_minutes(self) -> int:
        return int((dt.datetime.utcnow() - self.started_at).total_seconds() // 60)


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


LEAVE_REQUESTED = "requested"
LEAVE_APPROVED = "approved"
LEAVE_REJECTED = "rejected"


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
    note: Mapped[str] = mapped_column(Text, default="")
    linked_by: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    fully_compensated: Mapped[bool] = mapped_column(Boolean, default=False)

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
}
