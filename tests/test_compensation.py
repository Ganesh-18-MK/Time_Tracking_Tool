"""app/compensation.py — the automatic Punch Clock compensation balance
(Ganesh, 2026-07-31: "Punch Clock hours, fully automatic deduction").
`balance` is signed — overtime_total - shortfall_total — so it can go
positive (banked credit) as well as negative (still owed); Ganesh
confirmed this explicitly (short 1h + over 3h in the same month -> +2h,
not capped at 0h).

Deliberately mirrors test_reports.py's pattern: an in-memory sqlite db,
DayStatus rows seeded directly (never recomputed here — compensation.py
only reads DayStatus.target_minutes, it doesn't call engine.recompute_*),
plus PunchSession rows to drive the punched-minutes side. `today` is
always passed explicitly so these tests don't depend on the real
wall-clock date.
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


def _punch(db, emp_id, date, in_minute, out_minute):
    """in_minute/out_minute are minutes-since-midnight on `date`, purely to
    make test setup readable — PunchSession itself stores full datetimes."""
    base = dt.datetime.combine(date, dt.time())
    db.add(m.PunchSession(
        employee_id=emp_id, date=date,
        punched_in_at=base + dt.timedelta(minutes=in_minute),
        punched_out_at=base + dt.timedelta(minutes=out_minute),
    ))


def _break(db, emp_id, date, start_minute, end_minute, break_type=m.BREAK_PERSONAL):
    db.add(m.BreakEntry(
        employee_id=emp_id, date=date, break_type=break_type,
        start_minute=start_minute, end_minute=end_minute,
    ))


class TestMonthlySummary:
    def test_shortfall_day_adds_to_balance(self, db, emp):
        d = dt.date(YEAR, MONTH, 1)
        _status(db, emp.id, d, 480)
        _punch(db, emp.id, d, 0, 420)  # punched 420 of 480 -> 60 short
        db.commit()
        result = monthly_summary(db, emp, YEAR, MONTH, today=TODAY)
        assert result["shortfall_total"] == 60
        assert result["overtime_total"] == 0
        assert result["balance"] == -60  # negative = still owed

    def test_overtime_exactly_matching_shortfall_zeroes_the_balance(self, db, emp):
        d1, d2 = dt.date(YEAR, MONTH, 1), dt.date(YEAR, MONTH, 2)
        _status(db, emp.id, d1, 480)
        _status(db, emp.id, d2, 480)
        _punch(db, emp.id, d1, 0, 420)   # 60 short
        _punch(db, emp.id, d2, 0, 540)   # exactly 60 over
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
        _punch(db, emp.id, d1, 0, 420)   # 60 short
        _punch(db, emp.id, d2, 0, 660)   # 180 over
        db.commit()
        result = monthly_summary(db, emp, YEAR, MONTH, today=TODAY)
        assert result["shortfall_total"] == 60
        assert result["overtime_total"] == 180
        assert result["balance"] == 120  # +2h banked as credit

    def test_large_overtime_with_no_shortfall_is_pure_credit(self, db, emp):
        d = dt.date(YEAR, MONTH, 1)
        _status(db, emp.id, d, 480)
        _punch(db, emp.id, d, 0, 1000)
        db.commit()
        result = monthly_summary(db, emp, YEAR, MONTH, today=TODAY)
        assert result["shortfall_total"] == 0
        assert result["overtime_total"] == 520
        assert result["balance"] == 520

    def test_no_punch_at_all_is_a_full_day_shortfall(self, db, emp):
        d = dt.date(YEAR, MONTH, 1)
        _status(db, emp.id, d, 480)
        db.commit()  # no PunchSession row for this day at all
        result = monthly_summary(db, emp, YEAR, MONTH, today=TODAY)
        assert result["shortfall_total"] == 480
        assert result["balance"] == -480

    def test_balance_can_go_arbitrarily_negative_when_owed(self, db, emp):
        # no floor on the owed side either — a bad week should show its
        # true size, not get silently capped
        d1, d2, d3 = (dt.date(YEAR, MONTH, i) for i in (1, 2, 3))
        for d in (d1, d2, d3):
            _status(db, emp.id, d, 480)
            _punch(db, emp.id, d, 0, 240)  # 240 short each day
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

    def test_still_running_punch_session_does_not_count_yet(self, db, emp):
        # only *completed* Punch In/Out minutes count — an open session
        # (punched_out_at is None) isn't a finished fact yet, same
        # convention as app/reports.py's overtime column
        d = dt.date(YEAR, MONTH, 1)
        _status(db, emp.id, d, 480)
        db.add(m.PunchSession(
            employee_id=emp.id, date=d,
            punched_in_at=dt.datetime.combine(d, dt.time()),
            punched_out_at=None,
        ))
        db.commit()
        result = monthly_summary(db, emp, YEAR, MONTH, today=TODAY)
        # treated as 0 punched minutes -> full shortfall, not partial credit
        assert result["shortfall_total"] == 480

    def test_only_scoped_to_the_requested_month(self, db, emp):
        # a shortfall in June must not bleed into July's balance — the
        # feature resets every calendar month (Ganesh: "by end of the month")
        june_day = dt.date(YEAR, 6, 15)
        _status(db, emp.id, june_day, 480)
        _punch(db, emp.id, june_day, 0, 300)  # 180 short in June
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
        _punch(db, emp.id, d_later, 0, 420)    # 60 short -> included
        _punch(db, emp.id, d_earlier, 0, 400)  # 80 short -> included
        _punch(db, emp.id, d_exact, 0, 480)    # exactly on target -> excluded
        db.commit()
        result = monthly_summary(db, emp, YEAR, MONTH, today=TODAY)
        # chronological order, regardless of DB insertion order, and the
        # exact-target day never shows up in the breakdown at all
        assert [row["date"] for row in result["days"]] == [d_earlier, d_later]

    def test_empty_month_returns_zeroed_summary(self, db, emp):
        result = monthly_summary(db, emp, YEAR, MONTH, today=TODAY)
        assert result == {"balance": 0, "shortfall_total": 0, "overtime_total": 0, "days": []}


class TestBreakTimeExcludedFromPunchedMinutes:
    """Ganesh, 2026-08-01: "whenever we are taking a break the punch in
    timer should stop... then again punch timer should continue" — a
    PunchSession's raw duration includes any breaks taken while punched
    in, so a completed break must be netted out before comparing punched
    minutes to target, or break time would silently count as productive
    work in the automatic balance."""

    def test_break_taken_during_punch_session_reduces_punched_minutes(self, db, emp):
        d = dt.date(YEAR, MONTH, 1)
        _status(db, emp.id, d, 480)
        _punch(db, emp.id, d, 0, 480)          # punched a full 480 raw
        _break(db, emp.id, d, 100, 130)        # but 30 of those were a break
        db.commit()
        result = monthly_summary(db, emp, YEAR, MONTH, today=TODAY)
        # 480 raw - 30 break = 450 actual work -> 30 short, not exactly on target
        assert result["shortfall_total"] == 30
        assert result["overtime_total"] == 0
        assert result["balance"] == -30

    def test_still_running_break_is_not_netted_out_yet(self, db, emp):
        # only a *completed* break counts, same "not a finished fact yet"
        # convention as an open punch session
        d = dt.date(YEAR, MONTH, 1)
        _status(db, emp.id, d, 480)
        _punch(db, emp.id, d, 0, 480)
        db.add(m.BreakEntry(
            employee_id=emp.id, date=d, break_type=m.BREAK_PERSONAL,
            start_minute=100, end_minute=None,
        ))
        db.commit()
        result = monthly_summary(db, emp, YEAR, MONTH, today=TODAY)
        assert result["shortfall_total"] == 0  # full 480 still counts, break not closed

    def test_multiple_breaks_same_day_all_netted_out(self, db, emp):
        d = dt.date(YEAR, MONTH, 1)
        _status(db, emp.id, d, 480)
        _punch(db, emp.id, d, 0, 500)
        _break(db, emp.id, d, 100, 115)   # 15
        _break(db, emp.id, d, 300, 320)   # 20
        db.commit()
        result = monthly_summary(db, emp, YEAR, MONTH, today=TODAY)
        # 500 raw - 35 break = 465 actual -> 15 short
        assert result["shortfall_total"] == 15

    def test_break_time_never_pushes_punched_minutes_negative(self, db, emp):
        # a pathological/edge case (break minutes exceeding a short punch
        # session) must floor at 0, not go negative and flip into overtime
        d = dt.date(YEAR, MONTH, 1)
        _status(db, emp.id, d, 480)
        _punch(db, emp.id, d, 0, 20)
        _break(db, emp.id, d, 0, 20)
        db.commit()
        result = monthly_summary(db, emp, YEAR, MONTH, today=TODAY)
        assert result["shortfall_total"] == 480
        assert result["overtime_total"] == 0

    def test_break_alone_does_not_affect_target(self, db, emp):
        # the >30 min break-allowance-extends-target rule is engine.py's
        # job (already baked into DayStatus.target_minutes before this
        # module ever sees it) — this module must not double-apply it by
        # also touching target, only punched minutes
        d = dt.date(YEAR, MONTH, 1)
        _status(db, emp.id, d, 480)  # target as computed/stored — unchanged by this module
        _punch(db, emp.id, d, 0, 510)
        _break(db, emp.id, d, 100, 140)  # 40 min break -> 510 - 40 = 470 punched, 10 short
        db.commit()
        result = monthly_summary(db, emp, YEAR, MONTH, today=TODAY)
        assert result["days"][0]["target"] == 480
        assert result["days"][0]["punched"] == 470
        assert result["shortfall_total"] == 10
