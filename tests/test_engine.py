"""Status/variance/strike engine tests (PRD §5–§6 rules)."""
import datetime as dt

import pytest

from app import models as m
from app.engine import compute_day, strikes_in

MON = dt.date(2026, 7, 20)  # Monday
SAT = dt.date(2026, 7, 25)
TODAY = dt.date(2026, 7, 27)
TOL = 60


def emp(target=480, days="0,1,2,3,4"):
    return m.Employee(name="T", daily_target_minutes=target, work_days=days)


def day(sub, leave=0, e=None, d=MON, working=True, holiday=False, today=TODAY):
    return compute_day(e or emp(), d, sub, leave, working, holiday, TOL, today)


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
