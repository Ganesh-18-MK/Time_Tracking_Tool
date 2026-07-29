"""Admin -> Reports (app/reports.py): cascading Department -> Employee ->
Date range filters, shared by the Attendance and Strike report pages.

attendance_report()/strikes_report() take explicit start/end dates rather
than a range key, so these tests seed DayStatus rows in a month safely in
the FUTURE relative to today and pass those dates straight in. That sidesteps
_ensure_fresh(), which only calls engine.recompute_all() when start <= today
-- a recompute would otherwise rebuild DayStatus from TaskEntry/LeaveRecord
data these tests never create, wiping the rows seeded here. resolve_date_range()
itself is pure (today -> window) and is tested separately with an explicit
`today` override, no db involved.
"""
import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models as m
from app.db import Base
from app.reports import (
    DEFAULT_RANGE,
    RANGE_PRESETS,
    attendance_report,
    departments_list,
    employees_list,
    resolve_date_range,
    strikes_report,
)

FUTURE_YEAR = dt.date.today().year + 1
BASE_DATE = dt.date(FUTURE_YEAR, 6, 1)


@pytest.fixture()
def db():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


def _emp(db, id, name, department="Ops", tracked=True, active=True):
    e = m.Employee(id=id, name=name, department=department, tracked=tracked, active=active,
                    daily_target_minutes=480, work_days="0,1,2,3,4")
    db.add(e)
    return e


def _status(db, emp_id, date, status, override=None, strike_exempt=False):
    db.add(m.DayStatus(employee_id=emp_id, date=date, status=status,
                        override_status=override, strike_exempt=strike_exempt, source="computed"))


class TestResolveDateRange:
    TODAY = dt.date(2026, 7, 29)

    def test_7d_preset(self):
        start, end = resolve_date_range("7d", today=self.TODAY)
        assert end == self.TODAY
        assert (end - start).days == 6  # inclusive of both ends

    def test_30d_preset(self):
        start, end = resolve_date_range("30d", today=self.TODAY)
        assert (end - start).days == 29

    def test_90d_preset(self):
        start, end = resolve_date_range("90d", today=self.TODAY)
        assert (end - start).days == 89

    def test_custom_with_both_dates(self):
        s = dt.date(2026, 1, 1)
        e = dt.date(2026, 1, 10)
        assert resolve_date_range("custom", start=s, end=e, today=self.TODAY) == (s, e)

    def test_custom_swaps_reversed_dates(self):
        s = dt.date(2026, 1, 10)
        e = dt.date(2026, 1, 1)
        assert resolve_date_range("custom", start=s, end=e, today=self.TODAY) == (e, s)

    def test_custom_without_dates_falls_back_to_default(self):
        start, end = resolve_date_range("custom", today=self.TODAY)
        default_days = next(d for k, _l, d in RANGE_PRESETS if k == DEFAULT_RANGE)
        assert (end - start).days == default_days - 1

    def test_unrecognized_key_falls_back_to_default(self):
        start, end = resolve_date_range("bogus", today=self.TODAY)
        default_days = next(d for k, _l, d in RANGE_PRESETS if k == DEFAULT_RANGE)
        assert (end - start).days == default_days - 1
        assert end == self.TODAY


class TestDepartmentsList:
    def test_sorted_distinct_departments(self, db):
        _emp(db, 1, "A", department="Tech")
        _emp(db, 2, "B", department="Accounts")
        _emp(db, 3, "C", department="Tech")
        db.commit()
        assert departments_list(db) == ["Accounts", "Tech"]

    def test_untracked_or_inactive_excluded(self, db):
        _emp(db, 1, "Tracked", department="Tech", tracked=True)
        _emp(db, 2, "Untracked", department="Ops", tracked=False)
        _emp(db, 3, "Inactive", department="HR", active=False)
        db.commit()
        assert departments_list(db) == ["Tech"]

    def test_blank_department_becomes_dash(self, db):
        _emp(db, 1, "A", department=None)
        db.commit()
        assert departments_list(db) == ["—"]


class TestEmployeesList:
    def test_no_filter_returns_all_tracked(self, db):
        _emp(db, 1, "A", department="Tech")
        _emp(db, 2, "B", department="Accounts")
        db.commit()
        assert {e.name for e in employees_list(db)} == {"A", "B"}

    def test_filter_by_department(self, db):
        _emp(db, 1, "A", department="Tech")
        _emp(db, 2, "B", department="Accounts")
        db.commit()
        rows = employees_list(db, department="Tech")
        assert [e.name for e in rows] == ["A"]

    def test_untracked_excluded(self, db):
        _emp(db, 1, "A", department="Tech", tracked=False)
        db.commit()
        assert employees_list(db, department="Tech") == []


class TestAttendanceReportSummary:
    def test_counts_and_percentage_per_employee(self, db):
        _emp(db, 1, "Jane", department="Accounts")
        db.commit()
        for i, status in enumerate([m.COMPLETE, m.COMPLETE, m.COMPLETE, m.PARTIAL, m.MISSING]):
            _status(db, 1, BASE_DATE + dt.timedelta(days=i), status)
        db.commit()

        result = attendance_report(db, BASE_DATE, BASE_DATE + dt.timedelta(days=4))
        assert result["mode"] == "summary"
        row = result["rows"][0]
        assert row["employee"].name == "Jane"
        assert row["counts"][m.COMPLETE] == 3
        assert row["counts"][m.PARTIAL] == 1
        assert row["counts"][m.MISSING] == 1
        assert row["attendance_pct"] == 60.0  # 3 of 5 expected (complete+partial+missing)

    def test_percentage_none_when_nothing_expected(self, db):
        _emp(db, 1, "OnlyLeave")
        db.commit()
        _status(db, 1, BASE_DATE, m.LEAVE)
        db.commit()
        result = attendance_report(db, BASE_DATE, BASE_DATE)
        assert result["rows"][0]["attendance_pct"] is None

    def test_department_filter_scopes_rows(self, db):
        _emp(db, 1, "A", department="Tech")
        _emp(db, 2, "B", department="Accounts")
        db.commit()
        _status(db, 1, BASE_DATE, m.COMPLETE)
        _status(db, 2, BASE_DATE, m.COMPLETE)
        db.commit()
        result = attendance_report(db, BASE_DATE, BASE_DATE, department="Tech")
        assert [r["employee"].name for r in result["rows"]] == ["A"]

    def test_override_status_wins(self, db):
        _emp(db, 1, "A")
        db.commit()
        _status(db, 1, BASE_DATE, m.MISSING, override=m.LEAVE)
        db.commit()
        result = attendance_report(db, BASE_DATE, BASE_DATE)
        assert result["rows"][0]["counts"][m.LEAVE] == 1
        assert result["rows"][0]["counts"][m.MISSING] == 0


class TestAttendanceReportDaily:
    def test_single_employee_selected_returns_daily_rows_sorted_by_date(self, db):
        _emp(db, 1, "Jane")
        db.commit()
        _status(db, 1, BASE_DATE + dt.timedelta(days=1), m.COMPLETE)
        _status(db, 1, BASE_DATE, m.PARTIAL)
        db.commit()

        result = attendance_report(db, BASE_DATE, BASE_DATE + dt.timedelta(days=1), employee_id=1)
        assert result["mode"] == "daily"
        assert result["employee"].name == "Jane"
        assert [r["date"] for r in result["rows"]] == [BASE_DATE, BASE_DATE + dt.timedelta(days=1)]
        assert [r["status"] for r in result["rows"]] == [m.PARTIAL, m.COMPLETE]

    def test_unknown_employee_id_falls_back_to_summary(self, db):
        _emp(db, 1, "Jane")
        db.commit()
        result = attendance_report(db, BASE_DATE, BASE_DATE, employee_id=999)
        assert result["mode"] == "summary"
        assert result["rows"] == []


class TestStrikesReportSummary:
    def test_strikes_counted_and_sorted_worst_first(self, db):
        _emp(db, 1, "Worst")
        _emp(db, 2, "Best")
        db.commit()
        for i in range(3):
            _status(db, 1, BASE_DATE + dt.timedelta(days=i), m.MISSING)
        _status(db, 2, BASE_DATE, m.COMPLETE)
        db.commit()

        result = strikes_report(db, BASE_DATE, BASE_DATE + dt.timedelta(days=2))
        assert result["rows"][0]["employee"].name == "Worst"
        assert result["rows"][0]["strikes"] == 3
        assert result["rows"][1]["employee"].name == "Best"
        assert result["rows"][1]["strikes"] == 0

    def test_strike_exempt_days_never_count(self, db):
        _emp(db, 1, "A")
        db.commit()
        _status(db, 1, BASE_DATE, m.MISSING, strike_exempt=True)
        db.commit()
        result = strikes_report(db, BASE_DATE, BASE_DATE)
        assert result["rows"][0]["strikes"] == 0


class TestStrikesReportDaily:
    def test_single_employee_returns_only_strike_days_with_total(self, db):
        _emp(db, 1, "A")
        db.commit()
        _status(db, 1, BASE_DATE, m.MISSING)
        _status(db, 1, BASE_DATE + dt.timedelta(days=1), m.COMPLETE)
        db.commit()

        result = strikes_report(db, BASE_DATE, BASE_DATE + dt.timedelta(days=1), employee_id=1)
        assert result["mode"] == "daily"
        assert result["total"] == 1
        assert [r["date"] for r in result["rows"]] == [BASE_DATE]

    def test_strike_exempt_excluded_from_daily_rows_and_total(self, db):
        _emp(db, 1, "A")
        db.commit()
        _status(db, 1, BASE_DATE, m.MISSING, strike_exempt=True)
        db.commit()
        result = strikes_report(db, BASE_DATE, BASE_DATE, employee_id=1)
        assert result["rows"] == []
        assert result["total"] == 0
