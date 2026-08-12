"""Regression coverage for the HOLIDAY_MANAGEMENT_ENABLED guard added
2026-08-13 around _emp_from_form's location handling.

Holiday Management (Country field on Roster) is held back from this deploy
behind the flag (see app/templating.py). Both roster forms hide the Country
<select> while the flag is off, so app/routes/admin.py's roster_add/
roster_edit now pass `None` instead of the submitted value to
_emp_from_form — this test exists specifically to catch the regression
that motivated that change: without the None-when-off guard, Form()'s
unsubmitted-field default (m.DEFAULT_LOCATION, i.e. "India") would silently
overwrite every employee's location back to India on *any* roster edit,
including edits that have nothing to do with location at all.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models as m
from app.db import Base
from app.routes.admin import _emp_from_form
from app.util import ROLE_EMPLOYEE


def make_db():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)()


def edit(db, emp, location):
    """Mimics the fixed roster_edit() call: unrelated fields change, and
    `location` is whatever roster_edit() decided to pass through (None when
    HOLIDAY_MANAGEMENT_ENABLED is off, the submitted value when it's on)."""
    _emp_from_form(
        db, emp, emp.name, emp.email or "", "New Dept", emp.designation or "",
        "8", ["0", "1", "2", "3", "4"], "", True, True, ROLE_EMPLOYEE,
        location=location,
    )


class TestLocationFlagGuard:
    def test_flag_off_preserves_existing_location_on_unrelated_edit(self):
        db = make_db()
        emp = m.Employee(name="Priya", location=m.LOCATION_US)
        db.add(emp)
        db.commit()

        edit(db, emp, location=None)  # HOLIDAY_MANAGEMENT_ENABLED off

        assert emp.location == m.LOCATION_US, (
            "editing an employee for an unrelated reason must not reset "
            "their country back to the default while the flag is off"
        )

    def test_flag_on_applies_admin_selected_location(self):
        db = make_db()
        emp = m.Employee(name="Arun", location=m.LOCATION_INDIA)
        db.add(emp)
        db.commit()

        edit(db, emp, location=m.LOCATION_US)  # HOLIDAY_MANAGEMENT_ENABLED on

        assert emp.location == m.LOCATION_US

    def test_invalid_location_value_is_ignored_either_way(self):
        db = make_db()
        emp = m.Employee(name="Divya", location=m.LOCATION_INDIA)
        db.add(emp)
        db.commit()

        edit(db, emp, location="Mars")

        assert emp.location == m.LOCATION_INDIA

    def test_new_employee_defaults_to_india_when_flag_off(self):
        db = make_db()
        emp = m.Employee(name="New Hire")
        db.add(emp)
        db.commit()

        edit(db, emp, location=None)  # roster_add() with the flag off

        assert emp.location == m.DEFAULT_LOCATION == m.LOCATION_INDIA
