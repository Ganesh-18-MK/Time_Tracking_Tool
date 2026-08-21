"""Task Planning — "Plan for the Day" + Start/Pause/Resume/Stop (Ganesh,
2026-08-21, see docs/TASK_PLANNING_TIMER_PLAN.md). Matches this suite's
existing convention (see test_overtime.py's module docstring): route
handlers (app/routes/employee.py's /plan/* endpoints) aren't covered here
directly, since there's no FastAPI TestClient anywhere in this suite —
what's tested is the one pure function this feature introduced
(util.overtime_row_flags) and that the additive schema (PlannedTask,
ActiveTaskTimer.planned_task_id) round-trips correctly through an
in-memory sqlite db, same pattern test_compensation.py/test_overtime.py
already use.

Every Start-to-Pause/Stop segment becomes an ordinary TaskEntry through
the SAME _finish_task_timer()/validate_entry() path an ad-hoc Auto time
capture timer already goes through (see app/routes/employee.py) — this is
exactly why app/engine.py and app/validation.py needed zero changes for
this feature, and why there's no new engine/validation test class here:
every existing test in test_engine.py/test_validation.py already covers
the code path a plan-linked segment runs through."""
import datetime as dt

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import models as m
from app.db import Base
from app.util import overtime_row_flags


@pytest.fixture()
def db():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


def _emp(db, id=1, name="Asha"):
    e = m.Employee(id=id, name=name, department="Ops", active=True,
                    daily_target_minutes=480, work_days="0,1,2,3,4")
    db.add(e)
    return e


def _project(db, id=1, name="Acme Corp"):
    p = m.Project(id=id, name=name, active=True, status=m.LIST_APPROVED)
    db.add(p)
    return p


def _task(db, id=1, name="Development"):
    t = m.TaskType(id=id, name=name, active=True, status=m.LIST_APPROVED)
    db.add(t)
    return t


class TestOvertimeRowFlags:
    """Pure cumulative-sum helper backing today.html's overtime-row
    styling — see app/routes/employee.py's _day_context()."""

    def test_no_target_flags_nothing(self):
        assert overtime_row_flags([60, 60, 60], 0) == [False, False, False]

    def test_everything_under_target_is_unflagged(self):
        assert overtime_row_flags([120, 120, 120], 480) == [False, False, False]

    def test_row_that_starts_exactly_at_target_is_flagged(self):
        # three 160-minute rows: running-before is 0, 160, 320 — target 480
        # is only reached (not yet passed) after the 3rd row finishes, so
        # nothing here should flag; a 4th row would.
        assert overtime_row_flags([160, 160, 160], 480) == [False, False, False]
        assert overtime_row_flags([160, 160, 160, 30], 480) == [False, False, False, True]

    def test_row_that_starts_after_target_already_passed_is_flagged(self):
        # running-before the 2nd row is 500 (already past the 480 target)
        assert overtime_row_flags([500, 100], 480) == [False, True]

    def test_empty_list_returns_empty(self):
        assert overtime_row_flags([], 480) == []

    def test_running_total_accumulates_across_many_rows(self):
        durations = [100, 100, 100, 100, 100, 100]  # 600 total, target 480
        flags = overtime_row_flags(durations, 480)
        # running-before: 0,100,200,300,400,500 -> only the last (500>=480) flags
        assert flags == [False, False, False, False, False, True]


class TestPlannedTaskSchema:
    """Additive-only schema round-trip (Ganesh, 2026-08-21) — new
    `planned_tasks` table plus a nullable `active_task_timers.
    planned_task_id` FK. Confirms Base.metadata.create_all() picks up both
    without any migration script (see app/db.py's _add_missing_columns —
    exercised for real only against a live sqlite file, not this
    in-memory fixture, but the ORM-level shape is what matters here)."""

    def test_planned_task_defaults_to_planned_status(self, db):
        _emp(db)
        _project(db)
        _task(db)
        db.commit()
        plan = m.PlannedTask(
            employee_id=1, date=dt.date(2026, 8, 21), project_id=1, task_type_id=1,
            details="Work on the new leave system", created_by_employee_id=1,
        )
        db.add(plan)
        db.commit()
        fetched = db.execute(select(m.PlannedTask)).scalar_one()
        assert fetched.status == m.PLAN_PLANNED
        assert fetched.details == "Work on the new leave system"

    def test_active_task_timer_planned_task_id_defaults_null(self, db):
        _emp(db)
        _project(db)
        _task(db)
        db.commit()
        timer = m.ActiveTaskTimer(
            employee_id=1, date=dt.date(2026, 8, 21), project_id=1, task_type_id=1,
            details="ad-hoc, no plan", start_minute=540,
        )
        db.add(timer)
        db.commit()
        fetched = db.execute(select(m.ActiveTaskTimer)).scalar_one()
        assert fetched.planned_task_id is None

    def test_active_task_timer_can_link_to_a_planned_task(self, db):
        _emp(db)
        _project(db)
        _task(db)
        db.commit()
        plan = m.PlannedTask(
            employee_id=1, date=dt.date(2026, 8, 21), project_id=1, task_type_id=1,
            details="Plan-linked work", status=m.PLAN_RUNNING,
        )
        db.add(plan)
        db.commit()
        timer = m.ActiveTaskTimer(
            employee_id=1, date=dt.date(2026, 8, 21), project_id=1, task_type_id=1,
            details="Plan-linked work", start_minute=540, planned_task_id=plan.id,
        )
        db.add(timer)
        db.commit()
        fetched = db.execute(select(m.ActiveTaskTimer)).scalar_one()
        assert fetched.planned_task_id == plan.id
        assert fetched.planned_task.details == "Plan-linked work"

    def test_plan_statuses_tuple_has_all_four_states(self):
        assert m.PLAN_STATUSES == (m.PLAN_PLANNED, m.PLAN_RUNNING, m.PLAN_PAUSED, m.PLAN_DONE)
