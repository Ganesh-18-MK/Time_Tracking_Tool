"""Auto-added 'General / Break' display rows (Ganesh, 2026-08-14).

Ending a break used to leave no trace in the task log at all — employees had
started manually adding a 'Break' TaskEntry themselves, picking whatever
real client Project they'd last worked under, which read oddly on a report
and could double-count break time as billable work (BreakEntry/target math
already treats break time as separate from logged work — see BreakEntry's
docstring). `_merge_entries_and_breaks`/`_BreakLogRow` in
app/routes/employee.py instead build a read-only, display-only row —
Project 'General', Task 'Break' — for every completed break, merged
chronologically with the real TaskEntry rows. These tests cover that merge
directly (pure function, no route/DB round trip needed for the merge logic
itself, though real ORM rows are used as input for realism)."""
import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app import models as m
from app.routes.employee import _BreakLogRow, _merge_entries_and_breaks

TODAY = dt.date(2026, 8, 13)


@pytest.fixture()
def db():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    s.add_all([
        m.Project(id=1, name="Bluepeak Analytics Inc."),
        m.TaskType(id=1, name="Development"),
    ])
    emp = m.Employee(id=1, name="Test", daily_target_minutes=480)
    s.add(emp)
    s.commit()
    yield s, emp
    s.close()


def _entry(s, emp, start, end, details="did work"):
    e = m.TaskEntry(
        employee_id=emp.id, date=TODAY, project_id=1, task_type_id=1,
        details=details, start_minute=start, end_minute=end,
    )
    s.add(e)
    s.commit()
    return e


def _break(s, emp, start, end, break_type=m.BREAK_PERSONAL):
    b = m.BreakEntry(
        employee_id=emp.id, date=TODAY, break_type=break_type,
        start_minute=start, end_minute=end,
    )
    s.add(b)
    s.commit()
    return b


class TestBreakLogRow:
    def test_labels_are_fixed_general_and_break(self, db):
        s, emp = db
        b = _break(s, emp, 600, 630)
        row = _BreakLogRow(b)
        assert row.project.name == "General"
        assert row.task_type.name == "Break"

    def test_details_is_the_break_type(self, db):
        s, emp = db
        b = _break(s, emp, 600, 630, break_type=m.BREAK_LUNCH_DINNER)
        row = _BreakLogRow(b)
        assert row.details == "Lunch/Dinner"

    def test_id_is_none_so_edit_delete_flag_controls_no_op(self, db):
        s, emp = db
        b = _break(s, emp, 600, 630)
        row = _BreakLogRow(b)
        assert row.id is None

    def test_duration_matches_the_break_span(self, db):
        s, emp = db
        b = _break(s, emp, 600, 645)
        row = _BreakLogRow(b)
        assert row.duration_minutes == 45


class TestMergeEntriesAndBreaks:
    def test_merges_and_sorts_chronologically(self, db):
        s, emp = db
        e1 = _entry(s, emp, 540, 600)
        b1 = _break(s, emp, 600, 630)
        e2 = _entry(s, emp, 630, 660)
        merged = _merge_entries_and_breaks([e2, e1], [b1])
        assert [row.start_minute for row in merged] == [540, 600, 630]
        assert merged[1].project.name == "General"

    def test_open_still_running_break_is_excluded(self, db):
        s, emp = db
        e1 = _entry(s, emp, 540, 600)
        open_break = m.BreakEntry(
            employee_id=emp.id, date=TODAY, break_type=m.BREAK_PERSONAL,
            start_minute=600, end_minute=None,
        )
        s.add(open_break)
        s.commit()
        merged = _merge_entries_and_breaks([e1], [open_break])
        assert len(merged) == 1
        assert merged[0].id == e1.id

    def test_no_entries_no_breaks_returns_empty_list(self, db):
        assert _merge_entries_and_breaks([], []) == []

    def test_real_task_entries_keep_their_own_project_and_id(self, db):
        s, emp = db
        e1 = _entry(s, emp, 540, 600)
        b1 = _break(s, emp, 600, 630)
        merged = _merge_entries_and_breaks([e1], [b1])
        real = next(r for r in merged if r.id is not None)
        assert real.id == e1.id
        assert real.project.name == "Bluepeak Analytics Inc."

    def test_does_not_mutate_or_touch_the_original_entries_list_objects(self, db):
        """Total/target/gap_flags/compensation/overtime/strikes all keep
        computing off the original TaskEntry list — the merge must not
        monkeypatch or replace those objects, only build new ones for the
        break rows and return a new combined list."""
        s, emp = db
        e1 = _entry(s, emp, 540, 600)
        b1 = _break(s, emp, 600, 630)
        original_entries = [e1]
        _merge_entries_and_breaks(original_entries, [b1])
        assert original_entries == [e1]
        assert not hasattr(e1, "is_break")
