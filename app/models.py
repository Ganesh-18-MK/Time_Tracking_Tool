"""Data model per PRD §8.

Conventions:
  * All durations/targets/variances are integer MINUTES (no float-hour drift,
    matches the minute-level logging the whole system is built on).
  * Times of day are integer minutes since midnight (0..1440). No timezones —
    every value is in the employee's local working time, same as the sheets.
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
    phone: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    # Human-facing, stable ID ("LOMK001", ...) — never reused, never edited
    # by hand. Assigned at creation (see app/util.py next_employee_code) and
    # is the match key for bulk *updating* existing employees; existing rows
    # from before this column existed are backfilled once at startup (see
    # app/util.py ensure_employee_codes, called from app/main.py).
    employee_code: Mapped[Optional[str]] = mapped_column(String(20), unique=True, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    # tracked=False => excluded from compliance runs (e.g. admin accounts)
    tracked: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    # filename only (e.g. "14.jpg"), not a full path — stored under
    # app/static/uploads/avatars/. NULL until the employee uploads one via
    # /profile. Local disk for now; see note on SupportQuery/deployment re:
    # Render's ephemeral filesystem before this goes live long-term.
    photo_path: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    entries = relationship("TaskEntry", back_populates="employee")

    @property
    def work_day_set(self):
        return {int(x) for x in self.work_days.split(",") if x.strip() != ""}


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class TaskType(Base):
    __tablename__ = "task_types"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


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


class Holiday(Base):
    __tablename__ = "holidays"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date, unique=True)
    name: Mapped[str] = mapped_column(String(200), default="")


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
