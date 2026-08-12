"""Status/variance/strike engine tests (PRD §5–§6 rules)."""
import datetime as dt

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import models as m
from app.db import Base
from app.engine import compute_day, leave_balance, leave_minutes_on, strikes_in, today_attendance

MON = dt.date(2026, 7, 20)  # Monday
SAT = dt.date(2026, 7, 25)
TODAY = dt.date(2026, 7, 27)
TOL = 60


def emp(target=480, days="0,1,2,3,4"):
    return m.Employee(name="T", daily_target_minutes=target, work_days=days)


def day(sub, leave=0, e=None, d=MON, working=True, holiday=False, today=TODAY, break_excess=0):
    return compute_day(e or emp(), d, sub, leave, working, holiday, TOL, today, break_excess)


class TestStatuses:
    def test_complete_at_target(self):
        assert day(480)["status"] == m.COMPLETE

    def test_complete_exactly_at_tolerance_boundary(self):
        # hours >= target - tolerance  =>  420 on 480 target with 60 tol is Complete
        assert day(420)["status"] == m.COMPLETE

    def test_partial_one_minute_below_tolerance(self):
        assert day(419)["status"] == m.PARTIAL

    def test_missing_when_never_submitted_past_day(self):
        r = day(None)
        assert r["status"] == m.MISSING
        assert r["variance"] == -480

    def test_today_unsubmitted_is_pending_not_missing(self):
        assert day(None, d=TODAY) is None

    def test_weekend_no_work_records_zero(self):
        r = day(None, d=SAT, working=False)
        assert r["status"] == m.WEEKEND and r["variance"] == 0

    def test_weekend_work_is_pure_surplus(self):
        r = day(180, d=SAT, working=False)
        assert r["status"] == m.WEEKEND and r["variance"] == 180 and r["target"] == 0

    def test_holiday_flagged(self):
        r = day(None, working=False, holiday=True)
        assert r["status"] == m.HOLIDAY

    def test_part_time_target(self):
        # part-timer (the sheets show '4 Hours 19 min' days): 259-minute target
        r = day(259, e=emp(target=259))
        assert r["status"] == m.COMPLETE and r["variance"] == 0


class TestLeave:
    def test_full_day_leave_zero_target_no_variance(self):
        r = day(None, leave=480)
        assert r["status"] == m.LEAVE and r["target"] == 0 and r["variance"] == 0

    def test_partial_leave_reduces_target(self):
        # 2h approved leave -> target 360; 360 submitted = Complete, variance 0
        r = day(360, leave=120)
        assert r["status"] == m.COMPLETE and r["target"] == 360 and r["variance"] == 0

    def test_partial_leave_still_partial_if_short(self):
        r = day(240, leave=120)  # target 360, tol 60 -> 240 < 300 => Partial
        assert r["status"] == m.PARTIAL and r["variance"] == -120

    def test_partial_leave_unsubmitted_past_day_missing(self):
        r = day(None, leave=120)
        assert r["status"] == m.MISSING and r["variance"] == -360


def _row(status, exempt=False, compensated=False, override=None):
    r = m.DayStatus(employee_id=1, date=MON, status=status)
    r.strike_exempt = exempt
    r.compensated = compensated
    r.override_status = override
    return r


class TestStrikes:
    def test_missing_plus_partial_each_one_strike(self):
        rows = [_row(m.MISSING), _row(m.PARTIAL), _row(m.COMPLETE), _row(m.LEAVE)]
        assert strikes_in(rows, comp_erases=True) == 2

    def test_compensated_shortfall_erases_strike(self):
        # open question 3 default: fully compensated day recomputes as Complete
        assert strikes_in([_row(m.PARTIAL, compensated=True)], comp_erases=True) == 0

    def test_compensated_still_counts_when_config_off(self):
        assert strikes_in([_row(m.PARTIAL, compensated=True)], comp_erases=False) == 1

    def test_override_wins_over_everything(self):
        rows = [_row(m.COMPLETE, override=m.MISSING), _row(m.MISSING, override=m.LEAVE)]
        assert strikes_in(rows, comp_erases=True) == 1

    def test_pre_policy_days_never_strike(self):
        # April 1-14 2026: true status kept, sheet's own formula excluded them
        assert strikes_in([_row(m.MISSING, exempt=True)], comp_erases=True) == 0


class TestEffectiveStatus:
    def test_original_status_retained_under_compensation(self):
        r = _row(m.PARTIAL, compensated=True)
        assert r.status == m.PARTIAL  # audit keeps the base fact
        assert r.effective_status(True) == m.COMPLETE
        assert r.effective_status(False) == m.PARTIAL


def _leave(status, minutes=120, d=MON):
    return m.LeaveRecord(
        employee_id=1, start_date=d, end_date=d, minutes_per_day=minutes, status=status
    )


class TestLeaveApprovalFilter:
    """Self-service leave requests must not affect compliance until an
    admin approves them (see LeaveRecord.status / engine.leave_minutes_on)."""

    def test_approved_leave_counts(self):
        assert leave_minutes_on([_leave(m.LEAVE_APPROVED)], emp(), MON) == 120

    def test_requested_leave_does_not_count(self):
        assert leave_minutes_on([_leave(m.LEAVE_REQUESTED)], emp(), MON) == 0

    def test_rejected_leave_does_not_count(self):
        assert leave_minutes_on([_leave(m.LEAVE_REJECTED)], emp(), MON) == 0

    def test_column_default_is_approved(self):
        # every pre-existing admin-entered/imported row relies on this
        # default staying 'approved' — changing it silently breaks history.
        default = m.LeaveRecord.__table__.columns["status"].default.arg
        assert default == m.LEAVE_APPROVED

    def test_mixed_statuses_only_approved_sums(self):
        leaves = [_leave(m.LEAVE_APPROVED, minutes=60), _leave(m.LEAVE_REQUESTED, minutes=200)]
        assert leave_minutes_on(leaves, emp(), MON) == 60


class TestBreakExcess:
    """Break time within the configured allowance is free; time over it
    extends the target, same shape as leave reducing it (engine.compute_day
    break_excess_min param, fed by recompute_employee from BreakEntry)."""

    def test_within_allowance_target_unchanged(self):
        r = day(480, break_excess=0)
        assert r["target"] == 480 and r["status"] == m.COMPLETE

    def test_excess_extends_target(self):
        # 15 min over allowance -> target 495; 480 logged is now short
        r = day(480, break_excess=15)
        assert r["target"] == 495
        assert r["variance"] == 480 - 495

    def test_excess_can_flip_complete_to_partial(self):
        # exactly at the old target, but 60 over allowance pushes target to
        # 540; with 60 tolerance, 480 >= 540-60=480 is still Complete...
        r = day(480, break_excess=60)
        assert r["target"] == 540
        assert r["status"] == m.COMPLETE  # 480 >= 540 - 60 tolerance
        # one more minute of excess tips it over
        r2 = day(480, break_excess=61)
        assert r2["status"] == m.PARTIAL

    def test_excess_ignored_on_full_day_leave(self):
        # on approved full-day leave, no work is expected — break policy
        # doesn't apply regardless of how much break time was logged
        r = day(None, leave=480, break_excess=45)
        assert r["status"] == m.LEAVE and r["target"] == 0

    def test_excess_ignored_on_non_working_day(self):
        r = day(None, d=SAT, working=False, break_excess=45)
        assert r["status"] == m.WEEKEND and r["target"] == 0

    def test_negative_excess_never_reduces_target(self):
        # defensive: compute_day itself floors at 0, even if a caller ever
        # passes a negative number
        r = day(480, break_excess=-10)
        assert r["target"] == 480


@pytest.fixture()
def attendance_db():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    s.add_all([
        m.Project(id=1, name="P"),
        m.TaskType(id=1, name="T"),
    ])
    s.commit()
    yield s
    s.close()


def _mkemp(s, id, work_days="0,1,2,3,4,5,6"):
    e = m.Employee(id=id, name=f"E{id}", daily_target_minutes=480, work_days=work_days)
    s.add(e)
    s.commit()
    return e


class TestTodayAttendance:
    """Live, right-now attendance view for the dashboard KPI cards — distinct
    from DayStatus, which never marks today Missing because the day isn't
    over yet (see engine.today_attendance docstring)."""

    def test_logged_via_task_entry(self, attendance_db):
        s = attendance_db
        e = _mkemp(s, 1)
        s.add(m.TaskEntry(employee_id=e.id, date=TODAY, project_id=1, task_type_id=1,
                          details="x", start_minute=540, end_minute=600))
        s.commit()
        out = today_attendance(s, m.CONFIG_DEFAULTS, TODAY)
        assert [x.id for x in out["logged"]] == [1]
        assert out["on_leave"] == [] and out["not_yet"] == []

    def test_logged_via_active_break_with_no_time_entry(self, attendance_db):
        s = attendance_db
        e = _mkemp(s, 1)
        s.add(m.BreakEntry(employee_id=e.id, date=TODAY, start_minute=600, end_minute=None))
        s.commit()
        out = today_attendance(s, m.CONFIG_DEFAULTS, TODAY)
        assert [x.id for x in out["logged"]] == [1]

    def test_full_day_approved_leave_is_on_leave(self, attendance_db):
        s = attendance_db
        e = _mkemp(s, 1)
        s.add(m.LeaveRecord(employee_id=e.id, start_date=TODAY, end_date=TODAY,
                            minutes_per_day=None, status=m.LEAVE_APPROVED))
        s.commit()
        out = today_attendance(s, m.CONFIG_DEFAULTS, TODAY)
        assert [x.id for x in out["on_leave"]] == [1]
        assert out["logged"] == [] and out["not_yet"] == []

    def test_pending_leave_request_does_not_count_as_on_leave(self, attendance_db):
        # matches leave_minutes_on: only approved leave affects anything
        s = attendance_db
        e = _mkemp(s, 1)
        s.add(m.LeaveRecord(employee_id=e.id, start_date=TODAY, end_date=TODAY,
                            minutes_per_day=None, status=m.LEAVE_REQUESTED))
        s.commit()
        out = today_attendance(s, m.CONFIG_DEFAULTS, TODAY)
        assert [x.id for x in out["not_yet"]] == [1]
        assert out["on_leave"] == []

    def test_partial_leave_with_nothing_logged_is_not_yet(self, attendance_db):
        # a half-day leave doesn't cover the whole day — still expected to
        # log some work, so it isn't "on leave" for the day
        s = attendance_db
        e = _mkemp(s, 1)
        s.add(m.LeaveRecord(employee_id=e.id, start_date=TODAY, end_date=TODAY,
                            minutes_per_day=120, status=m.LEAVE_APPROVED))
        s.commit()
        out = today_attendance(s, m.CONFIG_DEFAULTS, TODAY)
        assert [x.id for x in out["not_yet"]] == [1]

    def test_nothing_logged_is_not_yet(self, attendance_db):
        s = attendance_db
        _mkemp(s, 1)
        out = today_attendance(s, m.CONFIG_DEFAULTS, TODAY)
        assert [x.id for x in out["not_yet"]] == [1]

    def test_non_working_day_reported_separately(self, attendance_db):
        s = attendance_db
        _mkemp(s, 1, work_days="0,1,2,3,4")  # Mon-Fri only
        out = today_attendance(s, m.CONFIG_DEFAULTS, SAT)
        assert [x.id for x in out["off_today"]] == [1]
        assert out["logged"] == [] and out["not_yet"] == [] and out["on_leave"] == []

    def test_inactive_employee_excluded_entirely(self, attendance_db):
        s = attendance_db
        e = _mkemp(s, 1)
        e.active = False
        s.commit()
        out = today_attendance(s, m.CONFIG_DEFAULTS, TODAY)
        assert out["logged"] == out["on_leave"] == out["not_yet"] == out["off_today"] == []


class TestLocationAwareHolidays:
    """Holiday management (Ganesh, 2026-08-12) — the team now has both US
    and India staff, each with their own holiday calendar (see Holiday's
    docstring in app/models.py). holidays_set()/holidays_by_location() must
    scope to one country at a time; is_working_day() must never fold in a
    holiday from the *other* country."""

    def _seed(self, s):
        s.add_all([
            m.Holiday(date=dt.date(2026, 7, 4), name="Independence Day", location=m.LOCATION_US),
            m.Holiday(date=dt.date(2026, 11, 8), name="Diwali", location=m.LOCATION_INDIA),
            # same calendar date, holiday in one country only in each row —
            # exercises the (date, location) unique pair, not date alone
            m.Holiday(date=dt.date(2026, 1, 26), name="Republic Day", location=m.LOCATION_INDIA),
        ])
        s.commit()

    def test_holidays_set_scoped_to_india_excludes_us_only_dates(self, attendance_db):
        s = attendance_db
        self._seed(s)
        from app.engine import holidays_set
        india = holidays_set(s, m.LOCATION_INDIA)
        assert dt.date(2026, 11, 8) in india
        assert dt.date(2026, 1, 26) in india
        assert dt.date(2026, 7, 4) not in india

    def test_holidays_set_scoped_to_us_excludes_india_only_dates(self, attendance_db):
        s = attendance_db
        self._seed(s)
        from app.engine import holidays_set
        us = holidays_set(s, m.LOCATION_US)
        assert dt.date(2026, 7, 4) in us
        assert dt.date(2026, 11, 8) not in us
        assert dt.date(2026, 1, 26) not in us

    def test_holidays_set_unscoped_returns_every_country(self, attendance_db):
        s = attendance_db
        self._seed(s)
        from app.engine import holidays_set
        everyone = holidays_set(s)
        assert everyone == {dt.date(2026, 7, 4), dt.date(2026, 11, 8), dt.date(2026, 1, 26)}

    def test_holidays_by_location_buckets_correctly(self, attendance_db):
        s = attendance_db
        self._seed(s)
        from app.engine import holidays_by_location
        by_loc = holidays_by_location(s)
        assert by_loc[m.LOCATION_US] == {dt.date(2026, 7, 4)}
        assert by_loc[m.LOCATION_INDIA] == {dt.date(2026, 11, 8), dt.date(2026, 1, 26)}

    def test_india_employee_still_works_on_a_us_only_holiday(self, attendance_db):
        s = attendance_db
        self._seed(s)
        from app.engine import holidays_set, is_working_day
        india_emp = m.Employee(name="I", daily_target_minutes=480, work_days="0,1,2,3,4,5,6",
                               location=m.LOCATION_INDIA)
        holidays = holidays_set(s, india_emp.location)
        # July 4 is a Saturday in 2026, but work_days includes Sat/Sun here
        # specifically so the assertion is about the holiday check, not the
        # weekday check
        assert is_working_day(india_emp, dt.date(2026, 7, 4), holidays) is True

    def test_india_employee_does_not_work_on_an_india_holiday(self, attendance_db):
        s = attendance_db
        self._seed(s)
        from app.engine import holidays_set, is_working_day
        india_emp = m.Employee(name="I", daily_target_minutes=480, work_days="0,1,2,3,4,5,6",
                               location=m.LOCATION_INDIA)
        holidays = holidays_set(s, india_emp.location)
        assert is_working_day(india_emp, dt.date(2026, 11, 8), holidays) is False

    def test_us_employee_does_not_work_on_a_us_holiday_but_does_on_diwali(self, attendance_db):
        s = attendance_db
        self._seed(s)
        from app.engine import holidays_set, is_working_day
        us_emp = m.Employee(name="U", daily_target_minutes=480, work_days="0,1,2,3,4,5,6",
                            location=m.LOCATION_US)
        holidays = holidays_set(s, us_emp.location)
        assert is_working_day(us_emp, dt.date(2026, 7, 4), holidays) is False
        assert is_working_day(us_emp, dt.date(2026, 11, 8), holidays) is True

    def test_recompute_employee_marks_holiday_only_for_matching_location(self, attendance_db):
        # end-to-end: recompute_employee must pull the employee's OWN
        # location's holiday set, not a flat company-wide one
        s = attendance_db
        self._seed(s)
        from app.engine import recompute_employee
        india_emp = m.Employee(id=10, name="I", daily_target_minutes=480,
                               work_days="0,1,2,3,4,5,6", location=m.LOCATION_INDIA)
        us_emp = m.Employee(id=11, name="U", daily_target_minutes=480,
                            work_days="0,1,2,3,4,5,6", location=m.LOCATION_US)
        s.add_all([india_emp, us_emp])
        s.commit()
        d = dt.date(2026, 11, 8)  # Diwali — India only
        recompute_employee(s, india_emp, d, d, m.CONFIG_DEFAULTS, today=dt.date(2026, 11, 9))
        recompute_employee(s, us_emp, d, d, m.CONFIG_DEFAULTS, today=dt.date(2026, 11, 9))
        india_status = s.execute(
            select(m.DayStatus).where(m.DayStatus.employee_id == 10, m.DayStatus.date == d)
        ).scalar_one()
        us_status = s.execute(
            select(m.DayStatus).where(m.DayStatus.employee_id == 11, m.DayStatus.date == d)
        ).scalar_one()
        assert india_status.status == m.HOLIDAY
        assert us_status.status != m.HOLIDAY


class TestLeaveBalance:
    """engine.leave_balance() — manager request, 2026-08-10: approving leave
    should count against the employee's annual entitlement, not just
    display it (see the function's own docstring for the full rationale:
    computed live from approved LeaveRecords, nothing stored/decremented,
    so a calendar-year boundary resets it automatically)."""

    def _emp(self, s, **entitlements):
        e = m.Employee(
            id=1, name="E1", daily_target_minutes=480, work_days="0,1,2,3,4",
            casual_leave_days=entitlements.get("casual"),
            sick_leave_days=entitlements.get("sick"),
            vacation_leave_days=entitlements.get("vacation"),
        )
        s.add(e)
        s.commit()
        return e

    def test_no_leave_taken_remaining_equals_entitlement(self, attendance_db):
        s = attendance_db
        e = self._emp(s, casual=10, sick=8, vacation=12)
        bal = leave_balance(s, e, year=2026)
        assert bal["Casual"] == {"entitlement": 10, "used": 0, "remaining": 10}
        assert bal["Sick"] == {"entitlement": 8, "used": 0, "remaining": 8}
        assert bal["Vacation"] == {"entitlement": 12, "used": 0, "remaining": 12}

    def test_unset_entitlement_reads_as_zero(self, attendance_db):
        # NULL columns (never bulk-assigned) -> 0, same convention as the
        # Employee model docstring / the old static balances table
        s = attendance_db
        e = self._emp(s)
        bal = leave_balance(s, e, year=2026)
        assert bal["Casual"]["entitlement"] == 0
        assert bal["Casual"]["remaining"] == 0

    def test_approved_full_day_leave_decrements_by_one(self, attendance_db):
        s = attendance_db
        e = self._emp(s, casual=10)
        s.add(m.LeaveRecord(
            employee_id=e.id, start_date=dt.date(2026, 3, 10), end_date=dt.date(2026, 3, 10),
            type="Casual", minutes_per_day=None, status=m.LEAVE_APPROVED,
        ))
        s.commit()
        bal = leave_balance(s, e, year=2026)
        assert bal["Casual"] == {"entitlement": 10, "used": 1, "remaining": 9}

    def test_matches_manager_worked_example_10_minus_2_is_8(self, attendance_db):
        # the exact scenario from the conversation: 10 assigned, 2 approved
        # full days taken -> 8 remaining
        s = attendance_db
        e = self._emp(s, casual=10)
        for d in (dt.date(2026, 3, 10), dt.date(2026, 3, 11)):
            s.add(m.LeaveRecord(
                employee_id=e.id, start_date=d, end_date=d,
                type="Casual", minutes_per_day=None, status=m.LEAVE_APPROVED,
            ))
        s.commit()
        assert leave_balance(s, e, year=2026)["Casual"]["remaining"] == 8

    def test_pending_request_does_not_decrement(self, attendance_db):
        s = attendance_db
        e = self._emp(s, casual=10)
        s.add(m.LeaveRecord(
            employee_id=e.id, start_date=dt.date(2026, 3, 10), end_date=dt.date(2026, 3, 10),
            type="Casual", minutes_per_day=None, status=m.LEAVE_REQUESTED,
        ))
        s.commit()
        assert leave_balance(s, e, year=2026)["Casual"]["remaining"] == 10

    def test_rejected_request_does_not_decrement(self, attendance_db):
        s = attendance_db
        e = self._emp(s, casual=10)
        s.add(m.LeaveRecord(
            employee_id=e.id, start_date=dt.date(2026, 3, 10), end_date=dt.date(2026, 3, 10),
            type="Casual", minutes_per_day=None, status=m.LEAVE_REJECTED,
        ))
        s.commit()
        assert leave_balance(s, e, year=2026)["Casual"]["remaining"] == 10

    def test_half_day_leave_decrements_by_half(self, attendance_db):
        s = attendance_db
        e = self._emp(s, sick=8)
        s.add(m.LeaveRecord(
            employee_id=e.id, start_date=dt.date(2026, 3, 10), end_date=dt.date(2026, 3, 10),
            type="Sick", minutes_per_day=240, status=m.LEAVE_APPROVED,  # half of 480
        ))
        s.commit()
        assert leave_balance(s, e, year=2026)["Sick"]["remaining"] == 7.5

    def test_multi_day_leave_decrements_once_per_day(self, attendance_db):
        s = attendance_db
        e = self._emp(s, vacation=12)
        s.add(m.LeaveRecord(
            employee_id=e.id, start_date=dt.date(2026, 3, 10), end_date=dt.date(2026, 3, 14),
            type="Vacation", minutes_per_day=None, status=m.LEAVE_APPROVED,
        ))
        s.commit()
        assert leave_balance(s, e, year=2026)["Vacation"]["remaining"] == 7  # 12 - 5

    def test_other_type_does_not_affect_any_bucket(self, attendance_db):
        # "Other" has no entitlement column to track against — see
        # _LEAVE_ENTITLEMENT_FIELDS
        s = attendance_db
        e = self._emp(s, casual=10, sick=8, vacation=12)
        s.add(m.LeaveRecord(
            employee_id=e.id, start_date=dt.date(2026, 3, 10), end_date=dt.date(2026, 3, 10),
            type="Other", note="jury duty", minutes_per_day=None, status=m.LEAVE_APPROVED,
        ))
        s.commit()
        bal = leave_balance(s, e, year=2026)
        assert bal["Casual"]["remaining"] == 10
        assert bal["Sick"]["remaining"] == 8
        assert bal["Vacation"]["remaining"] == 12

    def test_leave_in_a_different_year_does_not_count(self, attendance_db):
        # resets automatically each January: querying year=2026 must ignore
        # a 2025 leave record entirely
        s = attendance_db
        e = self._emp(s, casual=10)
        s.add(m.LeaveRecord(
            employee_id=e.id, start_date=dt.date(2025, 12, 15), end_date=dt.date(2025, 12, 15),
            type="Casual", minutes_per_day=None, status=m.LEAVE_APPROVED,
        ))
        s.commit()
        assert leave_balance(s, e, year=2026)["Casual"]["remaining"] == 10
        # but it does count against 2025's own balance
        assert leave_balance(s, e, year=2025)["Casual"]["remaining"] == 9

    def test_leave_spanning_year_boundary_only_counts_days_in_that_year(self, attendance_db):
        # Dec 30 2025 -> Jan 2 2026: 2 days fall in 2025, 2 days fall in 2026
        s = attendance_db
        e = self._emp(s, casual=10)
        s.add(m.LeaveRecord(
            employee_id=e.id, start_date=dt.date(2025, 12, 30), end_date=dt.date(2026, 1, 2),
            type="Casual", minutes_per_day=None, status=m.LEAVE_APPROVED,
        ))
        s.commit()
        assert leave_balance(s, e, year=2025)["Casual"]["remaining"] == 8
        assert leave_balance(s, e, year=2026)["Casual"]["remaining"] == 8

    def test_default_year_is_today_local(self, attendance_db):
        # no year kwarg -> uses today_local().year, not the raw system clock
        s = attendance_db
        e = self._emp(s, casual=10)
        assert leave_balance(s, e)["Casual"]["entitlement"] == 10
