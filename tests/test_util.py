"""Small pure-function helpers in app/util.py."""
import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import models as m
from app.db import Base
from app.util import (
    BUSINESS_TZ,
    clamp_break_end,
    ensure_bootstrap_admins,
    ensure_list_status_backfill,
    flags_to_role,
    mask_tail,
    now_local,
    overtime_minutes,
    punch_out_error,
    punch_remaining_minutes,
    role_to_flags,
    today_local,
)


class TestBusinessTimezone:
    """now_local()/today_local() (manager request, 2026-08-10): every
    clock-face capture is expressed in one fixed reference timezone
    (BUSINESS_TZ = America/Chicago), independent of the server container's
    own OS clock (Cloud Run defaults to UTC) or wherever an employee
    physically is. See CLAUDE.md's "no timezones" hard rule and
    app/util.py's BUSINESS_TZ docstring."""

    def test_now_local_is_timezone_aware_in_business_tz(self):
        now = now_local()
        assert now.tzinfo is not None
        assert now.utcoffset() is not None
        # CST is UTC-6, CDT is UTC-5 — never naive, never any other offset
        assert now.utcoffset() in (dt.timedelta(hours=-6), dt.timedelta(hours=-5))

    def test_today_local_matches_now_local_date(self):
        # both read the same instant through the same BUSINESS_TZ; the tiny
        # gap between the two calls can only matter within a few ms of
        # midnight, which this test isn't trying to pin down
        assert today_local() == now_local().date()

    def test_summer_date_is_cdt_utc_minus_5(self):
        # August: US Central is on daylight time
        instant = dt.datetime(2026, 8, 10, 12, 0, tzinfo=BUSINESS_TZ)
        assert instant.utcoffset() == dt.timedelta(hours=-5)
        assert instant.tzname() == "CDT"

    def test_winter_date_is_cst_utc_minus_6(self):
        # January: US Central is on standard time
        instant = dt.datetime(2026, 1, 10, 12, 0, tzinfo=BUSINESS_TZ)
        assert instant.utcoffset() == dt.timedelta(hours=-6)
        assert instant.tzname() == "CST"

    def test_ist_evening_converts_to_business_tz_morning(self):
        # The exact worked example from the manager conversation, 2026-08-10:
        # an employee in IST clicking Start at 8:00 PM local time must be
        # captured as ~9:30 AM Central the same instant — not 8:00 PM.
        ist_instant = dt.datetime(2026, 8, 10, 20, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        converted = ist_instant.astimezone(BUSINESS_TZ)
        assert (converted.hour, converted.minute) == (9, 30)
        assert converted.tzname() == "CDT"


class TestClampBreakEnd:
    def test_normal_break_same_day(self):
        # 9:43 PM (1303) -> 9:48 PM (1308): a real 5-minute break
        assert clamp_break_end(1303, 1308) == 1308

    def test_same_minute_break_is_not_a_wraparound(self):
        # regression: started and ended within the same clock minute used
        # to get clamped to end-of-day, fabricating a multi-hour duration
        assert clamp_break_end(1303, 1303) == 1303

    def test_genuine_midnight_wraparound_clamps_to_end_of_day(self):
        # started 23:58 (1438), ended 00:02 next calendar day (2)
        assert clamp_break_end(1438, 2) == 1440


class TestMaskTail:
    def test_blank_reads_as_not_added_yet(self):
        assert mask_tail("") == "Not added yet"
        assert mask_tail(None) == "Not added yet"

    def test_long_value_shows_last_four(self):
        assert mask_tail("1234567890123") == "•••••••••0123"

    def test_full_value_never_appears_in_output(self):
        assert "1234567890123" not in mask_tail("1234567890123")

    def test_value_shorter_than_keep_is_fully_masked(self):
        # nothing safe to reveal if the whole value is <= the keep length
        assert mask_tail("123") == "•••"

    def test_value_exactly_keep_length_is_fully_masked(self):
        assert mask_tail("1234") == "••••"

    def test_custom_keep_length(self):
        assert mask_tail("ABCDE1234F", keep=2) == "••••••••4F"

    def test_whitespace_only_reads_as_not_added_yet(self):
        assert mask_tail("   ") == "Not added yet"


class TestPunchRemainingMinutes:
    def test_nothing_punched_yet_shows_full_target(self):
        assert punch_remaining_minutes(480, 0) == 480

    def test_partial_progress_reduces_remaining(self):
        assert punch_remaining_minutes(480, 60) == 420

    def test_exactly_on_target_is_zero(self):
        assert punch_remaining_minutes(480, 480) == 0

    def test_overtime_goes_negative_not_clamped(self):
        # must stay visible as overtime, not silently read as "done"
        assert punch_remaining_minutes(480, 500) == -20

    def test_break_excess_already_folded_into_target(self):
        # the caller passes an already-adjusted target (base + break
        # excess) — this function does no break math of its own
        assert punch_remaining_minutes(480 + 10, 60) == 430


class TestOvertimeMinutes:
    def test_under_target_is_zero_not_negative(self):
        assert overtime_minutes(400, 480) == 0

    def test_exactly_on_target_is_zero(self):
        assert overtime_minutes(480, 480) == 0

    def test_over_target_is_the_excess(self):
        assert overtime_minutes(500, 480) == 20

    def test_unknown_target_reads_as_no_overtime_not_all_overtime(self):
        # a legacy-imported day with no computed target — must not count
        # the entire punched duration as overtime just because target is
        # unknown
        assert overtime_minutes(500, None) == 0

    def test_zero_target_full_day_leave_counts_any_punched_time_as_overtime(self):
        # a full-day-leave day has target 0 (see app/engine.py compute_day)
        # — any punched time that day is, by definition, all overtime
        assert overtime_minutes(60, 0) == 60


class TestPunchOutError:
    """Punch Out guard (Ganesh, 2026-08-11) — an employee was punching out
    with the day's task rows never Submit Day'd, so the punched duration
    had nothing backing it in the task log. Punch In/Out is always keyed to
    "today" (see the punch_in/punch_out routes), so this only ever needs
    today's own DaySubmission row."""

    def test_no_submission_yet_blocks_punch_out(self):
        assert punch_out_error(None) is not None

    def test_unsubmitted_day_blocks_punch_out(self):
        sub = m.DaySubmission(employee_id=1, date=dt.date(2026, 8, 10), total_minutes=0, locked=False)
        assert punch_out_error(sub) is not None

    def test_admin_reopened_day_blocks_punch_out_until_resubmitted(self):
        # an admin unlocking a day for corrections sets locked=False again —
        # punching out mid-correction shouldn't be allowed either
        sub = m.DaySubmission(employee_id=1, date=dt.date(2026, 8, 10), total_minutes=60, locked=False)
        assert punch_out_error(sub) is not None

    def test_submitted_and_locked_day_allows_punch_out(self):
        sub = m.DaySubmission(employee_id=1, date=dt.date(2026, 8, 10), total_minutes=480, locked=True)
        assert punch_out_error(sub) is None


class TestRoleToFlags:
    """Three-tier admin role (Ganesh, 2026-07-31) — see Employee.is_super_admin
    docstring. 'admin' is department-scoped, 'super_admin' is org-wide."""

    def test_employee_has_neither_flag(self):
        assert role_to_flags("employee") == (False, False)

    def test_admin_is_admin_but_not_super(self):
        assert role_to_flags("admin") == (True, False)

    def test_super_admin_has_both_flags(self):
        assert role_to_flags("super_admin") == (True, True)

    def test_unrecognized_value_defaults_to_employee(self):
        # never silently grants admin access on a typo'd/garbage role string
        assert role_to_flags("garbage") == (False, False)


class TestFlagsToRole:
    def test_neither_flag_is_employee(self):
        assert flags_to_role(False, False) == "employee"

    def test_admin_only_is_admin(self):
        assert flags_to_role(True, False) == "admin"

    def test_both_flags_is_super_admin(self):
        assert flags_to_role(True, True) == "super_admin"

    def test_super_admin_flag_without_admin_flag_reads_as_employee(self):
        # shouldn't happen in practice (is_super_admin implies is_admin
        # everywhere it's set), but must not silently grant admin display
        assert flags_to_role(False, True) == "employee"

    def test_round_trips_through_role_to_flags(self):
        for role in ("employee", "admin", "super_admin"):
            assert flags_to_role(*role_to_flags(role)) == role


class TestEnsureBootstrapAdmins:
    """The BOOTSTRAP_ADMINS startup step (Ganesh, 2026-07-31) — solves a
    fresh-Postgres-deploy chicken-and-egg problem where /signup can't work
    because nobody is on the roster yet. Must stay a no-op the instant any
    employee exists, so it's safe to leave set in Azure App Settings
    forever without ever clobbering real onboarded data."""

    @pytest.fixture()
    def db(self):
        eng = create_engine("sqlite://")
        Base.metadata.create_all(eng)
        s = sessionmaker(bind=eng)()
        yield s
        s.close()

    def test_noop_when_env_var_unset(self, db, monkeypatch):
        monkeypatch.delenv("BOOTSTRAP_ADMINS", raising=False)
        ensure_bootstrap_admins(db)
        assert db.execute(select(m.Employee)).first() is None

    def test_noop_when_employees_already_exist(self, db, monkeypatch):
        db.add(m.Employee(name="Existing Person", email="existing@mkimmigrationlaw.com"))
        db.commit()
        monkeypatch.setenv("BOOTSTRAP_ADMINS", "New Leader:new@mkimmigrationlaw.com")
        ensure_bootstrap_admins(db)
        # only the pre-existing row — nothing added on top of real data
        names = [e.name for e in db.execute(select(m.Employee)).scalars()]
        assert names == ["Existing Person"]

    def test_creates_three_super_admins_from_env_var(self, db, monkeypatch):
        monkeypatch.setenv(
            "BOOTSTRAP_ADMINS",
            "Deepthi Divakaran:Deepthi@mkimmigrationlaw.com,"
            "Steve Kennedy:Steve@mkimmigrationlaw.com,"
            "Norine:Norine@mkimmigrationlaw.com",
        )
        ensure_bootstrap_admins(db)
        emps = list(db.execute(select(m.Employee)).scalars())
        assert len(emps) == 3
        for e in emps:
            assert e.is_admin is True
            assert e.is_super_admin is True
            assert e.active is True
            assert e.tracked is False  # excluded from compliance runs, same as any admin
        emails = {e.email for e in emps}
        assert emails == {
            "Deepthi@mkimmigrationlaw.com",
            "Steve@mkimmigrationlaw.com",
            "Norine@mkimmigrationlaw.com",
        }
        codes = {e.employee_code for e in emps}
        assert codes == {"LOMK001", "LOMK002", "LOMK003"}

    def test_malformed_entries_are_skipped_not_fatal(self, db, monkeypatch):
        monkeypatch.setenv("BOOTSTRAP_ADMINS", "no-colon-here,Valid Leader:valid@mkimmigrationlaw.com")
        ensure_bootstrap_admins(db)
        emps = list(db.execute(select(m.Employee)).scalars())
        assert len(emps) == 1
        assert emps[0].email == "valid@mkimmigrationlaw.com"


class TestEnsureListStatusBackfill:
    """The Project/TaskType `status` column (Ganesh, 2026-08-01 suggestions
    feature) — regression coverage for a real bug: SQLite's ADD COLUMN
    leaves every pre-existing row's status NULL, not the ORM default of
    'approved'. NULL fails both the Today picker filter and
    validate_entry's status check, so a project/task created before this
    feature shipped would silently become unpickable and unloggable until
    backfilled. See ensure_super_admin_backfill for the identical
    prior-art pattern (same SQLite ADD COLUMN gap)."""

    @pytest.fixture()
    def db(self):
        eng = create_engine("sqlite://")
        Base.metadata.create_all(eng)
        s = sessionmaker(bind=eng)()
        yield s
        s.close()

    def test_backfills_null_status_to_approved(self, db):
        # simulates a row created before the status column existed. Leaving
        # status unset would NOT reproduce this — the ORM applies the
        # Python-side default('approved') to any new insert. An explicit
        # status=None bypasses that default (only an *unset* attribute gets
        # the default; an explicitly-assigned None is inserted as NULL),
        # which is what SQLite's own ALTER TABLE ADD COLUMN does to every
        # row that existed before the column did.
        p = m.Project(name="Legacy Project", active=True, status=None)
        t = m.TaskType(name="Legacy Task", active=True, status=None)
        db.add_all([p, t])
        db.commit()
        assert p.status is None and t.status is None  # sanity: nothing set it yet

        ensure_list_status_backfill(db)

        db.refresh(p)
        db.refresh(t)
        assert p.status == m.LIST_APPROVED
        assert t.status == m.LIST_APPROVED

    def test_noop_when_status_already_set(self, db):
        # a pending suggestion must NOT get silently promoted to approved
        pending = m.Project(name="Suggested Project", active=True, status=m.LIST_PENDING)
        approved = m.Project(name="Approved Project", active=True, status=m.LIST_APPROVED)
        db.add_all([pending, approved])
        db.commit()

        ensure_list_status_backfill(db)

        db.refresh(pending)
        db.refresh(approved)
        assert pending.status == m.LIST_PENDING
        assert approved.status == m.LIST_APPROVED

    def test_noop_when_nothing_to_backfill(self, db):
        # safe to call on every startup even with zero projects/tasks
        ensure_list_status_backfill(db)
        assert list(db.execute(select(m.Project)).scalars()) == []
