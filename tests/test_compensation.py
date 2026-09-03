"""app/compensation.py — the automatic compensation balance (Ganesh,
2026-07-31: "Punch Clock hours, fully automatic deduction"; source
switched from Punch In/Out to logged Task Log time, Ganesh, 2026-09-03 —
see the module's own docstring for why). `balance` is signed — overtime_
total - shortfall_total — so it can go positive (banked credit) as well as
negative (still owed); Ganesh confirmed this explicitly (short 1h + over
3h in the same month -> +2h, not capped at 0h).

Deliberately mirrors test_reports.py's pattern: an in-memory sqlite db,
DayStatus rows seeded directly (never recomputed here — compensation.py
only reads DayStatus.target_minutes, it doesn't call engine.recompute_*),
plus TaskEntry rows to drive the logged-minutes side (same fixture
convention test_reports.py's own _entry() helper already uses — arbitrary
project_id/task_type_id, no real Project/TaskType rows needed since
sqlite here doesn't enforce the FK). `today` is always passed explicitly
so these tests don't depend on the real wall-clock date.
"""
import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models as m
from app.compensation import monthly_summary
from app.db import Base

YEAR, MONTH = 2026, 7
TODAY = dt.date(YEAR, MONTH, 31)  # last day of the month — full month in scope


@pytest.fixture()
def db():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


@pytest.fixture()
def emp(db):
    e = m.Employee(name="Asha", department="Frontdesk", daily_target_minutes=480,
                    work_days="0,1,2,3,4")
    db.add(e)
    db.commit()
    return e


def _status(db, emp_id, date, target):
    db.add(m.DayStatus(employee_id=emp_id, date=date, status=m.COMPLETE,
                        target_minutes=target, source="computed"))


def _entry(db, emp_id, date, start_minute, end_minute):
    """One logged TaskEntry row — start/end are minutes-since-midnight.
    Multiple calls on the same date are additive (mirrors a real day with
    more than one row logged)."""
    db.add(m.TaskEntry(
        employee_id=emp_id, date=date, project_id=1, task_type_id=1,
        start_minute=start_minute, end_minute=end_minute,
    ))


class TestMonthlySummary:
    def test_shortfall_day_adds_to_balance(self, db, emp):
        d = dt.date(YEAR, MONTH, 1)
        _status(db, emp.id, d, 480)
        _entry(db, emp.id, d, 0, 420)  # logged 420 of 480 -> 60 short
        db.commit()
        result = monthly_summary(db, emp, YEAR, MONTH, today=TODAY)
        assert result["shortfall_total"] == 60
        assert result["overtime_total"] == 0
        assert result["balance"] == -60  # negative = still owed

    def test_overtime_exactly_matching_shortfall_zeroes_the_balance(self, db, emp):
        d1, d2 = dt.date(YEAR, MONTH, 1), dt.date(YEAR, MONTH, 2)
        _status(db, emp.id, d1, 480)
        _status(db, emp.id, d2, 480)
        _entry(db, emp.id, d1, 0, 420)   # 60 short
        _entry(db, emp.id, d2, 0, 540)   # exactly 60 over
        db.commit()
        result = monthly_summary(db, emp, YEAR, MONTH, today=TODAY)
        assert result["shortfall_total"] == 60
        assert result["overtime_total"] == 60
        assert result["balance"] == 0

    def test_overtime_beyond_shortfall_banks_as_a_credit(self, db, emp):
        # Ganesh's exact scenario: short 1h Monday, over 3h Tuesday -> +2h,
        # not capped at 0h
        d1, d2 = dt.date(YEAR, MONTH, 1), dt.date(YEAR, MONTH, 2)
        _status(db, emp.id, d1, 480)
        _status(db, emp.id, d2, 480)
        _entry(db, emp.id, d1, 0, 420)   # 60 short
        _entry(db, emp.id, d2, 0, 660)   # 180 over
        db.commit()
        result = monthly_summary(db, emp, YEAR, MONTH, today=TODAY)
        assert result["shortfall_total"] == 60
        assert result["overtime_total"] == 180
        assert result["balance"] == 120  # +2h banked as credit

    def test_large_overtime_with_no_shortfall_is_pure_credit(self, db, emp):
        d = dt.date(YEAR, MONTH, 1)
        _status(db, emp.id, d, 480)
        _entry(db, emp.id, d, 0, 1000)
        db.commit()
        result = monthly_summary(db, emp, YEAR, MONTH, today=TODAY)
        assert result["shortfall_total"] == 0
        assert result["overtime_total"] == 520
        assert result["balance"] == 520

    def test_no_entries_at_all_is_a_full_day_shortfall(self, db, emp):
        d = dt.date(YEAR, MONTH, 1)
        _status(db, emp.id, d, 480)
        db.commit()  # no TaskEntry row for this day at all
        result = monthly_summary(db, emp, YEAR, MONTH, today=TODAY)
        assert result["shortfall_total"] == 480
        assert result["balance"] == -480

    def test_balance_can_go_arbitrarily_negative_when_owed(self, db, emp):
        # no floor on the owed side either — a bad week should show its
        # true size, not get silently capped
        d1, d2, d3 = (dt.date(YEAR, MONTH, i) for i in (1, 2, 3))
        for d in (d1, d2, d3):
            _status(db, emp.id, d, 480)
            _entry(db, emp.id, d, 0, 240)  # 240 short each day
        db.commit()
        result = monthly_summary(db, emp, YEAR, MONTH, today=TODAY)
        assert result["shortfall_total"] == 720
        assert result["balance"] == -720

    def test_leave_holiday_weekend_days_are_excluded(self, db, emp):
        # DayStatus.target_minutes is 0 (or None) on leave/holiday/weekend —
        # never a shortfall, and must not even show up in the day breakdown
        d = dt.date(YEAR, MONTH, 1)
        _status(db, emp.id, d, 0)
        db.commit()
        result = monthly_summary(db, emp, YEAR, MONTH, today=TODAY)
        assert result == {"balance": 0, "shortfall_total": 0, "overtime_total": 0, "days": []}

    def test_multiple_entries_same_day_are_summed(self, db, emp):
        # a real day is usually more than one logged row -- the switch to
        # TaskEntry (2026-09-03) sums every row that date, same grouped-sum
        # query engine.recompute_employee's own task_totals already uses
        d = dt.date(YEAR, MONTH, 1)
        _status(db, emp.id, d, 480)
        _entry(db, emp.id, d, 0, 200)     # 200
        _entry(db, emp.id, d, 260, 460)   # 200
        db.commit()
        result = monthly_summary(db, emp, YEAR, MONTH, today=TODAY)
        # 400 logged of 480 -> 80 short
        assert result["days"][0]["logged"] == 400
        assert result["shortfall_total"] == 80

    def test_only_scoped_to_the_requested_month(self, db, emp):
        # a shortfall in June must not bleed into July's balance — the
        # feature resets every calendar month (Ganesh: "by end of the month")
        june_day = dt.date(YEAR, 6, 15)
        _status(db, emp.id, june_day, 480)
        _entry(db, emp.id, june_day, 0, 300)  # 180 short in June
        db.commit()
        result = monthly_summary(db, emp, YEAR, MONTH, today=TODAY)  # asking for July
        assert result == {"balance": 0, "shortfall_total": 0, "overtime_total": 0, "days": []}

    def test_days_list_is_sorted_and_omits_exact_target_days(self, db, emp):
        d_later, d_earlier, d_exact = (
            dt.date(YEAR, MONTH, 3), dt.date(YEAR, MONTH, 1), dt.date(YEAR, MONTH, 2),
        )
        _status(db, emp.id, d_later, 480)
        _status(db, emp.id, d_earlier, 480)
        _status(db, emp.id, d_exact, 480)
        _entry(db, emp.id, d_later, 0, 420)    # 60 short -> included
        _entry(db, emp.id, d_earlier, 0, 400)  # 80 short -> included
        _entry(db, emp.id, d_exact, 0, 480)    # exactly on target -> excluded
        db.commit()
        result = monthly_summary(db, emp, YEAR, MONTH, today=TODAY)
        # chronological order, regardless of DB insertion order, and the
        # exact-target day never shows up in the breakdown at all
        assert [row["date"] for row in result["days"]] == [d_earlier, d_later]

    def test_empty_month_returns_zeroed_summary(self, db, emp):
        result = monthly_summary(db, emp, YEAR, MONTH, today=TODAY)
        assert result == {"balance": 0, "shortfall_total": 0, "overtime_total": 0, "days": []}


class TestSourceSwitchMatchesDayStatusActual:
    """Ganesh, 2026-09-03: the whole point of the switch was for this
    feature to read the same "actual hours worked" signal as everywhere
    else in the app (DayStatus.actual_minutes, strikes, My Month's own
    Running hours balance) instead of a separate Punch In/Out figure. A
    day with logged TaskEntry rows that would sum to a given
    DayStatus.actual_minutes must produce that exact same number as
    "logged" here — this module doesn't read actual_minutes directly (it
    only reads target_minutes off DayStatus, see the module docstring),
    but the two must agree because they're now built from the same
    TaskEntry rows via the same kind of grouped-sum query."""

    def test_logged_matches_sum_of_task_entries_independent_of_daystatus_actual(self, db, emp):
        d = dt.date(YEAR, MONTH, 1)
        # DayStatus.actual_minutes deliberately left stale/wrong here to
        # prove this module computes "logged" itself from TaskEntry rather
        # than trusting whatever's cached on DayStatus.actual_minutes
        db.add(m.DayStatus(employee_id=emp.id, date=d, status=m.COMPLETE,
                            target_minutes=480, actual_minutes=0, source="computed"))
        _entry(db, emp.id, d, 0, 300)
        db.commit()
        result = monthly_summary(db, emp, YEAR, MONTH, today=TODAY)
        assert result["days"][0]["logged"] == 300
        assert result["shortfall_total"] == 180
