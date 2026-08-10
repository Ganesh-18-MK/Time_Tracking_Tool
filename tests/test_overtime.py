"""Overtime pre-approval (Ganesh's manager, 2026-08-03): Team Lead routing
(app/auth.py led_by()) and the Attendance Report's "approved overtime"
figure (app/reports.py). Mirrors test_reports.py/test_compensation.py's
pattern — an in-memory sqlite db, rows seeded directly, PunchSession rows
to drive the overtime side. Route handlers (request/approve/reject/grant)
aren't covered here, matching this suite's existing convention of testing
the logic layer (app/auth.py, app/reports.py) rather than app/routes/*.py
directly — there's no FastAPI TestClient anywhere else in this suite either.
"""
import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models as m
from app.auth import led_by
from app.db import Base
from app.reports import attendance_report
from app.util import today_local

YEAR, MONTH = 2026, 8
FUTURE_YEAR = today_local().year + 1  # keeps attendance_report tests out of
# _ensure_fresh()'s recompute path, same reasoning as test_reports.py's
# module docstring — these tests seed DayStatus rows directly and a
# recompute would wipe them.


@pytest.fixture()
def db():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


def _emp(db, id, name, department="Ops", is_admin=False, is_super_admin=False, reports_to_id=None):
    e = m.Employee(
        id=id, name=name, department=department, active=True,
        daily_target_minutes=480, work_days="0,1,2,3,4",
        is_admin=is_admin, is_super_admin=is_super_admin, reports_to_id=reports_to_id,
    )
    db.add(e)
    return e


def _status(db, emp_id, date, target=480):
    db.add(m.DayStatus(employee_id=emp_id, date=date, status=m.COMPLETE,
                        target_minutes=target, source="computed"))


def _punch(db, emp_id, date, in_minute, out_minute):
    base = dt.datetime.combine(date, dt.time())
    db.add(m.PunchSession(
        employee_id=emp_id, date=date,
        punched_in_at=base + dt.timedelta(minutes=in_minute),
        punched_out_at=base + dt.timedelta(minutes=out_minute),
    ))


def _ot(db, emp_id, start, end, status=m.OT_APPROVED):
    db.add(m.OvertimeApproval(employee_id=emp_id, start_date=start, end_date=end, status=status))


class TestLedBy:
    """Team Lead scoping — per-person via reports_to_id, restricted (at the
    roster-form level, not here) to reports_to values that are admins."""

    def test_super_admin_is_unscoped(self, db):
        lead = _emp(db, 1, "Norine", is_super_admin=True, is_admin=True)
        db.commit()
        assert led_by(lead, db) is None

    def test_dept_admin_sees_only_their_direct_reports(self, db):
        lead = _emp(db, 1, "Deepthi", is_admin=True)
        _emp(db, 2, "Asha", reports_to_id=1)
        _emp(db, 3, "Priya", reports_to_id=1)
        _emp(db, 4, "Rahul", reports_to_id=None)  # unassigned — not in scope
        db.commit()
        assert led_by(lead, db) == {2, 3}

    def test_dept_admin_with_no_reports_gets_empty_set_not_none(self, db):
        # empty set (not None) matters: None means "unscoped" everywhere
        # this is used (nav badge counts, admin/overtime.html's employee
        # picker) — an empty set correctly means "scoped, but to nobody".
        lead = _emp(db, 1, "Steve", is_admin=True)
        _emp(db, 2, "Asha", reports_to_id=None)
        db.commit()
        assert led_by(lead, db) == set()

    def test_being_led_by_someone_else_does_not_leak_into_scope(self, db):
        lead_a = _emp(db, 1, "Deepthi", is_admin=True)
        _emp(db, 2, "Lead B", is_admin=True, reports_to_id=None)
        _emp(db, 3, "Asha", reports_to_id=2)  # reports to Lead B, not A
        db.commit()
        assert led_by(lead_a, db) == set()


class TestApprovedOvertimeInAttendanceReport:
    """attendance_report()'s new 'approved_overtime'/'approved_overtime_minutes'
    figure — same overtime minutes as the existing 'overtime' figure, but
    only counting days inside an OT_APPROVED range. Never changes the raw
    'overtime' figure itself."""

    def test_day_inside_approved_range_counts_as_approved(self, db):
        emp = _emp(db, 1, "Asha")
        d = dt.date(FUTURE_YEAR, 6, 10)
        _status(db, 1, d, target=480)
        _punch(db, 1, d, 0, 600)  # 10h punched, 2h over 8h target
        _ot(db, 1, d, d, status=m.OT_APPROVED)
        db.commit()
        result = attendance_report(db, d, d, employee_id=1)
        row = result["rows"][0]
        assert row["overtime"] == 120
        assert row["approved_overtime"] == 120

    def test_day_outside_any_approved_range_is_unapproved(self, db):
        emp = _emp(db, 1, "Asha")
        d = dt.date(FUTURE_YEAR, 6, 10)
        _status(db, 1, d, target=480)
        _punch(db, 1, d, 0, 600)
        # approval covers a different day entirely
        _ot(db, 1, dt.date(FUTURE_YEAR, 6, 1), dt.date(FUTURE_YEAR, 6, 2), status=m.OT_APPROVED)
        db.commit()
        result = attendance_report(db, d, d, employee_id=1)
        row = result["rows"][0]
        assert row["overtime"] == 120  # raw overtime unaffected either way
        assert row["approved_overtime"] == 0

    def test_no_approval_at_all_is_unapproved(self, db):
        emp = _emp(db, 1, "Asha")
        d = dt.date(FUTURE_YEAR, 6, 10)
        _status(db, 1, d, target=480)
        _punch(db, 1, d, 0, 600)
        db.commit()
        result = attendance_report(db, d, d, employee_id=1)
        assert result["rows"][0]["approved_overtime"] == 0

    def test_pending_request_does_not_count_as_approved(self, db):
        emp = _emp(db, 1, "Asha")
        d = dt.date(FUTURE_YEAR, 6, 10)
        _status(db, 1, d, target=480)
        _punch(db, 1, d, 0, 600)
        _ot(db, 1, d, d, status=m.OT_REQUESTED)  # not yet approved
        db.commit()
        result = attendance_report(db, d, d, employee_id=1)
        assert result["rows"][0]["overtime"] == 120
        assert result["rows"][0]["approved_overtime"] == 0

    def test_rejected_request_does_not_count_as_approved(self, db):
        emp = _emp(db, 1, "Asha")
        d = dt.date(FUTURE_YEAR, 6, 10)
        _status(db, 1, d, target=480)
        _punch(db, 1, d, 0, 600)
        _ot(db, 1, d, d, status=m.OT_REJECTED)
        db.commit()
        result = attendance_report(db, d, d, employee_id=1)
        assert result["rows"][0]["approved_overtime"] == 0

    def test_multi_day_range_covers_every_day_in_it(self, db):
        emp = _emp(db, 1, "Asha")
        d1 = dt.date(FUTURE_YEAR, 6, 10)
        d2 = dt.date(FUTURE_YEAR, 6, 11)
        _status(db, 1, d1, target=480)
        _status(db, 1, d2, target=480)
        _punch(db, 1, d1, 0, 600)
        _punch(db, 1, d2, 0, 540)  # 1h over
        _ot(db, 1, d1, d2, status=m.OT_APPROVED)
        db.commit()
        result = attendance_report(db, d1, d2, employee_id=1)
        by_date = {r["date"]: r for r in result["rows"]}
        assert by_date[d1]["approved_overtime"] == 120
        assert by_date[d2]["approved_overtime"] == 60

    def test_summary_mode_aggregates_approved_overtime_per_employee(self, db):
        _emp(db, 1, "Asha")
        _emp(db, 2, "Priya")
        d = dt.date(FUTURE_YEAR, 6, 10)
        _status(db, 1, d, target=480)
        _status(db, 2, d, target=480)
        _punch(db, 1, d, 0, 600)  # 2h overtime, approved
        _punch(db, 2, d, 0, 600)  # 2h overtime, NOT approved
        _ot(db, 1, d, d, status=m.OT_APPROVED)
        db.commit()
        result = attendance_report(db, d, d)  # no employee_id -> summary mode
        by_id = {r["employee"].id: r for r in result["rows"]}
        assert by_id[1]["overtime_minutes"] == 120
        assert by_id[1]["approved_overtime_minutes"] == 120
        assert by_id[2]["overtime_minutes"] == 120
        assert by_id[2]["approved_overtime_minutes"] == 0

    def test_unapproved_overtime_still_compensable(self, db):
        """The whole point of keeping approval non-blocking (Ganesh: "I
        still want to use overtime, even unapproved, to compensate for
        missed time") — this test exists to document that nothing about
        OvertimeApproval touches DayStatus/CompensationLink at all; the
        raw 'overtime' figure (what a compensation link's surplus-day
        picker ultimately traces back to via variance_minutes) is
        identical whether or not the day was pre-approved."""
        emp = _emp(db, 1, "Asha")
        d = dt.date(FUTURE_YEAR, 6, 10)
        _status(db, 1, d, target=480)
        _punch(db, 1, d, 0, 600)
        db.commit()  # no OvertimeApproval row at all
        result = attendance_report(db, d, d, employee_id=1)
        assert result["rows"][0]["overtime"] == 120  # fully intact, unapproved or not


class TestOvertimeApprovalCovers:
    def test_covers_is_inclusive_of_both_ends(self):
        ot = m.OvertimeApproval(
            employee_id=1, start_date=dt.date(2026, 6, 5), end_date=dt.date(2026, 6, 7),
        )
        assert ot.covers(dt.date(2026, 6, 5)) is True
        assert ot.covers(dt.date(2026, 6, 6)) is True
        assert ot.covers(dt.date(2026, 6, 7)) is True
        assert ot.covers(dt.date(2026, 6, 4)) is False
        assert ot.covers(dt.date(2026, 6, 8)) is False
