"""Leave Management V2 (Ganesh, 2026-08-21 — see docs/LEAVE_MANAGEMENT_PLAN.md).

Pure date/number math (full_months_elapsed, years_of_service_on,
planned_time_accrued_minutes_pure, required_notice_working_days) is tested
without a database at all — these were also verified standalone against
the exact minutes table in the plan's §2 in the sandbox that wrote this
file (no fastapi/sqlalchemy available there), and this file is the real
pytest version of those same checks, so `pytest tests/test_leave_v2.py -q`
is the thing that actually needs to go green before
LEAVE_MANAGEMENT_V2_ENABLED flips on.

DB-backed pieces (leave_balance_v2, is_probation_active,
notice_period_satisfied, effective_leave_type) use the same in-memory
sqlite fixture pattern as test_compensation.py/test_reports.py.
"""
import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import engine, models as m
from app.db import Base

CFG = {
    "planned_days_year_0_2": "9",
    "planned_days_year_2_5": "11",
    "planned_days_year_5_plus": "13",
    "probation_days_default": "90",
}


@pytest.fixture()
def db():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


@pytest.fixture()
def emp(db):
    e = m.Employee(
        name="Priya", department="Ops", daily_target_minutes=480,
        work_days="0,1,2,3,4", start_date=dt.date(2020, 1, 1),
    )
    db.add(e)
    db.commit()
    return e


# ---- full_months_elapsed ----------------------------------------------------
class TestFullMonthsElapsed:
    def test_zero_before_start(self):
        assert engine.full_months_elapsed(dt.date(2026, 3, 1), dt.date(2026, 2, 1)) == 0

    def test_month_starting_on_the_1st_counts_once_it_ends(self):
        start = dt.date(2026, 4, 1)
        assert engine.full_months_elapsed(start, dt.date(2026, 4, 29)) == 0
        assert engine.full_months_elapsed(start, dt.date(2026, 4, 30)) == 1

    def test_mid_month_start_skips_the_partial_first_month(self):
        start = dt.date(2026, 1, 15)
        # January is never fully in [start, ...) since it began before start
        assert engine.full_months_elapsed(start, dt.date(2026, 1, 31)) == 0
        assert engine.full_months_elapsed(start, dt.date(2026, 2, 15)) == 0
        assert engine.full_months_elapsed(start, dt.date(2026, 2, 28)) == 1

    def test_multiple_full_months(self):
        assert engine.full_months_elapsed(dt.date(2026, 1, 1), dt.date(2026, 6, 30)) == 6


# ---- accrual band math, matching docs/LEAVE_MANAGEMENT_PLAN.md §2 ----------
class TestPlannedTimeAccrual:
    def test_0_2_year_band_is_360_minutes_per_month_at_8h_day(self):
        start = dt.date(2025, 4, 1)
        total = engine.planned_time_accrued_minutes_pure(start, 0, 480, CFG, dt.date(2025, 4, 30))
        assert total == 360

    def test_2_5_year_band_is_440_minutes_per_month_at_8h_day(self):
        start = dt.date(2020, 1, 1)  # 2-year mark is 2022-01-01
        one_month = (
            engine.planned_time_accrued_minutes_pure(start, 0, 480, CFG, dt.date(2022, 2, 28))
            - engine.planned_time_accrued_minutes_pure(start, 0, 480, CFG, dt.date(2022, 1, 31))
        )
        assert one_month == 440

    def test_5_plus_year_band_is_520_minutes_per_month_at_8h_day(self):
        start = dt.date(2015, 1, 1)
        one_month = (
            engine.planned_time_accrued_minutes_pure(start, 0, 480, CFG, dt.date(2022, 2, 28))
            - engine.planned_time_accrued_minutes_pure(start, 0, 480, CFG, dt.date(2022, 1, 31))
        )
        assert one_month == 520

    def test_no_accrual_during_probation(self):
        start = dt.date(2026, 1, 1)
        assert engine.planned_time_accrued_minutes_pure(start, 90, 480, CFG, dt.date(2026, 3, 15)) == 0

    def test_accrual_not_backdated_into_probation_window(self):
        # probation ends 2026-03-31 (day=31, not the 1st) -> April is the
        # first candidate month, not March
        start = dt.date(2026, 1, 1)
        total = engine.planned_time_accrued_minutes_pure(start, 90, 480, CFG, dt.date(2026, 4, 30))
        assert total == 360  # exactly one month (April), not April+partial-March

    def test_running_total_does_not_reset_on_january_1st(self):
        start = dt.date(2024, 1, 1)
        before = engine.planned_time_accrued_minutes_pure(start, 0, 480, CFG, dt.date(2024, 12, 31))
        after = engine.planned_time_accrued_minutes_pure(start, 0, 480, CFG, dt.date(2025, 1, 31))
        assert after == before + 360  # one more month credited, no reset dip

    def test_wrapper_reads_probation_default_from_config_when_unset(self, db, emp):
        # emp.probation_days is None -> falls back to cfg's probation_days_default
        emp.start_date = dt.date(2026, 1, 1)
        as_of = dt.date(2026, 3, 15)  # within the 90-day default probation
        assert engine.planned_time_accrued_minutes(db, emp, CFG, as_of) == 0

    def test_wrapper_respects_per_employee_probation_override(self, db, emp):
        emp.start_date = dt.date(2026, 1, 1)
        emp.probation_days = 10  # much shorter than the 90-day default
        as_of = dt.date(2026, 3, 15)
        assert engine.planned_time_accrued_minutes(db, emp, CFG, as_of) > 0


# ---- probation gate ----------------------------------------------------------
class TestProbation:
    def test_active_during_window(self, emp):
        emp.start_date = dt.date(2026, 1, 1)
        emp.probation_days = 90
        assert engine.is_probation_active(emp, dt.date(2026, 3, 15), CFG) is True

    def test_inactive_after_window(self, emp):
        emp.start_date = dt.date(2026, 1, 1)
        emp.probation_days = 90
        assert engine.is_probation_active(emp, dt.date(2026, 4, 15), CFG) is False

    def test_falls_back_to_config_default_when_unset(self, emp):
        emp.start_date = dt.date(2026, 1, 1)
        emp.probation_days = None
        assert engine.is_probation_active(emp, dt.date(2026, 3, 15), CFG) is True
        assert engine.is_probation_active(emp, dt.date(2026, 5, 1), CFG) is False


# ---- notice period ------------------------------------------------------------
class TestNoticePeriod:
    def test_tiers_match_the_plan(self):
        assert engine.required_notice_working_days(1) == 2
        assert engine.required_notice_working_days(2) == 7
        assert engine.required_notice_working_days(3) == 7
        assert engine.required_notice_working_days(4) == 15
        assert engine.required_notice_working_days(30) == 15

    def test_satisfied_when_enough_working_days_precede_the_request(self, emp):
        holidays = set()
        # Monday 2026-08-24 submitted for a 1-day leave starting Thursday
        # 2026-08-27 -> Tue+Wed = 2 working days' notice, exactly enough
        submitted = dt.date(2026, 8, 24)
        leave_start = dt.date(2026, 8, 27)
        assert engine.notice_period_satisfied(submitted, leave_start, 1, emp, holidays) is True

    def test_not_satisfied_with_too_little_notice(self, emp):
        submitted = dt.date(2026, 8, 26)
        leave_start = dt.date(2026, 8, 27)  # next day, 0 working days' notice
        assert engine.notice_period_satisfied(submitted, leave_start, 1, emp, holidays=set()) is False

    def test_holidays_and_weekends_dont_count_toward_notice(self, emp):
        # Fri 2026-08-21 submitted for Mon 2026-08-24 -> only Sat/Sun
        # between them, both non-working -> 0 working days, fails a 2-day
        # requirement even though 3 calendar days passed
        submitted = dt.date(2026, 8, 21)
        leave_start = dt.date(2026, 8, 24)
        assert engine.notice_period_satisfied(submitted, leave_start, 1, emp, holidays=set()) is False


# ---- PIP forces Unpaid --------------------------------------------------------
class TestPipForcesUnpaid:
    def test_normal_employee_keeps_requested_type(self, emp):
        emp.is_on_pip = False
        assert engine.effective_leave_type(emp, m.LEAVE_PLANNED) == m.LEAVE_PLANNED

    def test_pip_employee_forced_to_unpaid(self, emp):
        emp.is_on_pip = True
        assert engine.effective_leave_type(emp, m.LEAVE_PLANNED) == m.LEAVE_UNPAID
        assert engine.effective_leave_type(emp, m.LEAVE_BEREAVEMENT) == m.LEAVE_UNPAID

    def test_pip_does_not_override_special_paid_time(self, emp):
        # Special Paid Time is a management grant, not a request PIP should
        # be able to veto
        emp.is_on_pip = True
        assert engine.effective_leave_type(emp, m.LEAVE_SPECIAL_PAID) == m.LEAVE_SPECIAL_PAID


# ---- used / pending / remaining split -----------------------------------------
class TestLeaveBalanceV2:
    def test_pending_is_held_out_of_remaining_without_counting_as_used(self, db, emp):
        emp.start_date = dt.date(2020, 1, 1)
        db.add(m.LeaveRecord(
            employee_id=emp.id, start_date=dt.date(2026, 6, 1), end_date=dt.date(2026, 6, 1),
            type=m.LEAVE_PLANNED, minutes_per_day=240, status=m.LEAVE_REQUESTED,
        ))
        db.commit()
        bal = engine.leave_balance_v2(db, emp, as_of=dt.date(2026, 6, 15), cfg=CFG)
        planned = bal[m.LEAVE_PLANNED]
        assert planned["used"] == 0
        assert planned["pending"] == 240
        assert planned["remaining"] == planned["entitlement"] - 240

    def test_partial_approval_reduces_used_not_the_original_request(self, db, emp):
        emp.start_date = dt.date(2020, 1, 1)
        lv = m.LeaveRecord(
            employee_id=emp.id, start_date=dt.date(2026, 6, 1), end_date=dt.date(2026, 6, 1),
            type=m.LEAVE_PLANNED, minutes_per_day=480, status=m.LEAVE_APPROVED,
            approved_minutes_per_day=240,  # admin approved half of what was asked
        )
        db.add(lv)
        db.commit()
        bal = engine.leave_balance_v2(db, emp, as_of=dt.date(2026, 6, 15), cfg=CFG)
        assert bal[m.LEAVE_PLANNED]["used"] == 240  # not 480
        assert bal[m.LEAVE_PLANNED]["pending"] == 0

    def test_unplanned_type_has_no_capped_entitlement(self, db, emp):
        bal = engine.leave_balance_v2(db, emp, as_of=dt.date(2026, 6, 15), cfg=CFG)
        assert bal[m.LEAVE_UNPLANNED]["entitlement"] is None
        assert bal[m.LEAVE_UNPLANNED]["remaining"] is None

    def test_special_paid_entitlement_sums_grants(self, db, emp):
        db.add(m.SpecialPaidGrant(employee_id=emp.id, minutes=240, reason="Recognition award", granted_by="Admin"))
        db.add(m.SpecialPaidGrant(employee_id=emp.id, minutes=120, reason="Extra", granted_by="Admin"))
        db.commit()
        bal = engine.leave_balance_v2(db, emp, as_of=dt.date(2026, 6, 15), cfg=CFG)
        assert bal[m.LEAVE_SPECIAL_PAID]["entitlement"] == 360


# ---- Overtime-for-Missed-Hours match window (requirement 9) -----------------
class TestCompensationWindow:
    def test_within_21_days_is_ok(self):
        assert engine.compensation_window_ok(dt.date(2026, 6, 1), dt.date(2026, 6, 20)) is True
        assert engine.compensation_window_ok(dt.date(2026, 6, 1), dt.date(2026, 5, 15)) is True

    def test_same_calendar_month_is_ok_even_if_far_apart_in_days(self):
        # 30 days apart but both in March -> still allowed via the
        # same-calendar-month half of the OR
        assert engine.compensation_window_ok(dt.date(2026, 3, 1), dt.date(2026, 3, 30)) is True

    def test_outside_both_conditions_fails(self):
        assert engine.compensation_window_ok(dt.date(2026, 1, 1), dt.date(2026, 3, 1)) is False

    def test_exact_21_day_boundary_is_inclusive(self):
        # cross-month pair so the same-calendar-month half of the OR can't
        # rescue a boundary case and mask what's actually being tested
        assert engine.compensation_window_ok(dt.date(2026, 1, 25), dt.date(2026, 2, 15)) is True   # 21 days
        assert engine.compensation_window_ok(dt.date(2026, 1, 25), dt.date(2026, 2, 16)) is False  # 22 days
