"""Entry validation tests — the strict guidelines of PRD §4."""
import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app import models as m
from app.validation import EntryError, earliest_allowed_date, gap_flags, validate_entry

CFG = dict(m.CONFIG_DEFAULTS)
TODAY = dt.date.today()


@pytest.fixture()
def db():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    s.add_all([
        m.Project(id=1, name="AB2 Consulting, Inc."),
        m.Project(id=2, name="Old Client", active=False),
        m.TaskType(id=1, name="Check emails"),
    ])
    emp = m.Employee(id=1, name="Test", daily_target_minutes=480)
    s.add(emp)
    s.commit()
    yield s, emp
    s.close()


def v(s, emp, **kw):
    args = dict(date=TODAY, project_id=1, task_type_id=1,
                details="valid details here", start_minute=540, end_minute=600)
    args.update(kw)
    validate_entry(s, emp, cfg=CFG, **args)


def errs(s, emp, **kw):
    with pytest.raises(EntryError) as ei:
        v(s, emp, **kw)
    return " | ".join(ei.value.errors)


class TestRowRules:
    def test_valid_row_passes(self, db):
        v(*db)

    def test_end_before_start_rejected(self, db):
        assert "after Start" in errs(*db, start_minute=600, end_minute=540)

    def test_zero_length_rejected(self, db):
        assert "after Start" in errs(*db, start_minute=600, end_minute=600)

    def test_max_row_duration_4h_default(self, db):
        assert "break the work down" in errs(*db, start_minute=540, end_minute=540 + 241)
        v(*db, start_minute=540, end_minute=540 + 240)  # exactly 4h is fine

    def test_details_min_5_chars(self, db):
        assert "at least 5 characters" in errs(*db, details="abc")

    def test_free_text_project_not_allowed(self, db):
        assert "Project/Employer" in errs(*db, project_id=0)

    def test_deactivated_project_hidden_from_new_rows(self, db):
        assert "Project/Employer" in errs(*db, project_id=2)

    def test_future_date_rejected(self, db):
        assert "future" in errs(*db, date=TODAY + dt.timedelta(days=1))


class TestOverlaps:
    def test_overlap_rejected(self, db):
        s, emp = db
        s.add(m.TaskEntry(employee_id=1, date=TODAY, project_id=1, task_type_id=1,
                          details="existing row", start_minute=570, end_minute=630))
        s.commit()
        assert "Overlaps" in errs(s, emp, start_minute=600, end_minute=660)

    def test_touching_rows_allowed(self, db):
        s, emp = db
        s.add(m.TaskEntry(employee_id=1, date=TODAY, project_id=1, task_type_id=1,
                          details="existing row", start_minute=570, end_minute=630))
        s.commit()
        v(s, emp, start_minute=630, end_minute=690)  # back-to-back is fine


class TestLockAndBackdate:
    def test_locked_day_rejects_entries(self, db):
        s, emp = db
        s.add(m.DaySubmission(employee_id=1, date=TODAY, total_minutes=60, locked=True))
        s.commit()
        assert "locked" in errs(s, emp)

    def test_unlocked_day_accepts_entries(self, db):
        s, emp = db
        s.add(m.DaySubmission(employee_id=1, date=TODAY, total_minutes=60, locked=False))
        s.commit()
        v(s, emp)

    def test_earliest_allowed_skips_weekend(self):
        # Monday with 1-working-day window => Friday allowed
        emp = m.Employee(name="t", daily_target_minutes=480, work_days="0,1,2,3,4")
        monday = dt.date(2026, 7, 27)
        assert earliest_allowed_date(emp, monday, 1, set()) == dt.date(2026, 7, 24)

    def test_earliest_allowed_skips_holiday(self):
        emp = m.Employee(name="t", daily_target_minutes=480, work_days="0,1,2,3,4")
        monday = dt.date(2026, 7, 27)
        assert earliest_allowed_date(
            emp, monday, 1, {dt.date(2026, 7, 24)}
        ) == dt.date(2026, 7, 23)


class TestGapFlags:
    def test_gap_over_threshold_flagged_not_blocked(self, db):
        s, _ = db
        a = m.TaskEntry(id=1, employee_id=1, date=TODAY, project_id=1, task_type_id=1,
                        details="a", start_minute=540, end_minute=600)
        b = m.TaskEntry(id=2, employee_id=1, date=TODAY, project_id=1, task_type_id=1,
                        details="b", start_minute=620, end_minute=680)
        flags = gap_flags([a, b], 15)
        assert flags == {2: 20}

    def test_gap_within_threshold_not_flagged(self):
        a = m.TaskEntry(id=1, employee_id=1, date=TODAY, project_id=1, task_type_id=1,
                        details="a", start_minute=540, end_minute=600)
        b = m.TaskEntry(id=2, employee_id=1, date=TODAY, project_id=1, task_type_id=1,
                        details="b", start_minute=615, end_minute=680)
        assert gap_flags([a, b], 15) == {}
