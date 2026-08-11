"""Entry validation tests — the strict guidelines of PRD §4."""
import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app import models as m
from app.util import today_local
from app.validation import (
    EntryError,
    earliest_allowed_date,
    entry_details_edit_error,
    gap_flags,
    validate_entry,
)

CFG = dict(m.CONFIG_DEFAULTS)
# validate_entry() itself now computes "today" via today_local() (BUSINESS_TZ,
# not the test runner's own OS clock) — this must match, or these tests would
# be flaky specifically in the UTC-vs-Central gap around midnight.
TODAY = today_local()


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
    other = m.Employee(id=2, name="Someone Else", daily_target_minutes=480)
    s.add_all([emp, other])
    s.commit()
    # employee/lead-suggested Project/Task (Ganesh, 2026-08-01) — usable by
    # the submitter only while pending
    s.add_all([
        m.Project(id=3, name="Suggested By Emp1", status=m.LIST_PENDING, created_by_employee_id=1),
        m.TaskType(id=2, name="Suggested Task By Emp1", status=m.LIST_PENDING, created_by_employee_id=1),
        m.Project(id=4, name="Rejected Suggestion", status=m.LIST_REJECTED, active=False, created_by_employee_id=1),
    ])
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


class TestSuggestionStatus:
    """Employee/lead-suggested Project/Task (Ganesh, 2026-08-01), tightened
    2026-08-11: a pending suggestion is unusable by ANYONE — including
    whoever suggested it — until a team lead/admin approves it. (Previously
    the submitter could use their own pending suggestion immediately; that
    carve-out let unreviewed suggestions end up on real logged time before
    review, so it's gone.) Enforced here server-side, not just hidden
    client-side — see app/routes/employee.py _visible_projects_and_tasks
    for the matching dropdown filter."""

    def test_submitter_cannot_use_their_own_pending_project_yet(self, db):
        s, emp = db
        assert "awaiting admin approval" in errs(s, emp, project_id=3)  # emp (id=1) suggested project id=3

    def test_submitter_cannot_use_their_own_pending_task_yet(self, db):
        s, emp = db
        assert "awaiting admin approval" in errs(s, emp, task_type_id=2)  # emp (id=1) suggested task id=2

    def test_someone_else_also_cannot_use_a_pending_project(self, db):
        s, _emp = db
        other = s.get(m.Employee, 2)
        assert "awaiting admin approval" in errs(s, other, project_id=3)

    def test_someone_else_also_cannot_use_a_pending_task(self, db):
        s, _emp = db
        other = s.get(m.Employee, 2)
        assert "awaiting admin approval" in errs(s, other, task_type_id=2)

    def test_approved_suggestion_becomes_usable_by_anyone(self, db):
        s, emp = db
        proj = s.get(m.Project, 3)
        proj.status = m.LIST_APPROVED
        s.commit()
        v(s, emp, project_id=3)  # no longer pending — fine now

    def test_rejected_suggestion_unusable_even_by_the_submitter(self, db):
        # rejected also sets active=False (see app/routes/admin.py
        # suggestion_reject)
        assert "Project/Employer" in errs(*db, project_id=4)


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


class TestBreakOverlap:
    """An employee can't log task time over a break they also logged
    (Ganesh, 2026-08-11 — employee entered 1:15 PM start when their actual
    break ran 12:57-1:18 PM; the row saved and the resulting gap got
    flagged as unexplained even though a break covered nearly all of it).
    Blocked in validate_entry rather than left to gap_flags's netting so
    the employee gets a clear message and a valid time to pick instead."""

    def test_entry_starting_inside_a_break_is_blocked(self, db):
        s, emp = db
        s.add(m.BreakEntry(employee_id=1, date=TODAY, break_type=m.BREAK_LUNCH_DINNER,
                            start_minute=777, end_minute=798))  # 12:57-1:18 PM
        s.commit()
        msg = errs(s, emp, start_minute=780, end_minute=840)  # 1:00-2:00 PM
        assert "during your" in msg and "break" in msg

    def test_entry_partially_overlapping_a_break_is_blocked(self, db):
        s, emp = db
        s.add(m.BreakEntry(employee_id=1, date=TODAY, break_type=m.BREAK_LUNCH_DINNER,
                            start_minute=777, end_minute=798))
        s.commit()
        # starts before the break and runs into it
        assert "break" in errs(s, emp, start_minute=770, end_minute=780)

    def test_entry_right_after_break_ends_is_allowed(self, db):
        s, emp = db
        s.add(m.BreakEntry(employee_id=1, date=TODAY, break_type=m.BREAK_LUNCH_DINNER,
                            start_minute=777, end_minute=798))
        s.commit()
        v(s, emp, start_minute=798, end_minute=860)  # 1:18 PM onward — touching is fine

    def test_entry_before_break_starts_is_allowed(self, db):
        s, emp = db
        s.add(m.BreakEntry(employee_id=1, date=TODAY, break_type=m.BREAK_LUNCH_DINNER,
                            start_minute=777, end_minute=798))
        s.commit()
        v(s, emp, start_minute=690, end_minute=777)  # ends exactly when break starts

    def test_entry_during_an_open_ongoing_break_is_blocked(self, db):
        s, emp = db
        s.add(m.BreakEntry(employee_id=1, date=TODAY, break_type=m.BREAK_PERSONAL,
                            start_minute=777, end_minute=None))  # still running
        s.commit()
        assert "break" in errs(s, emp, start_minute=780, end_minute=840)


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

    def test_break_nets_out_of_gap_even_when_not_exactly_aligned(self):
        # Exact screenshot scenario (Ganesh, 2026-08-11): prev row ends
        # 12:55 PM (775), break runs 12:57-1:18 PM (777-798), next row
        # starts 1:15 PM (795). Raw gap is 20min; the break covers 18 of
        # those 20 (777-795 overlap), leaving only 2min unexplained — well
        # under the 15min threshold, so this should NOT be flagged, even
        # though the break doesn't start/end exactly on the adjacent rows.
        prev = m.TaskEntry(id=1, employee_id=1, date=TODAY, project_id=1, task_type_id=1,
                           details="a", start_minute=690, end_minute=775)  # 11:30-12:55
        cur = m.TaskEntry(id=2, employee_id=1, date=TODAY, project_id=1, task_type_id=1,
                          details="b", start_minute=795, end_minute=870)  # 1:15-2:30
        brk = m.BreakEntry(employee_id=1, date=TODAY, break_type=m.BREAK_LUNCH_DINNER,
                           start_minute=777, end_minute=798)
        assert gap_flags([prev, cur], 15, [brk]) == {}

    def test_gap_beyond_break_still_flagged_with_remaining_minutes_only(self):
        # Same prev row + break as above, but the employee doesn't log the
        # next row until 1:40 PM (820) — 45min raw gap, break covers 21min
        # (777-798, entirely inside the gap this time), leaving 24min
        # genuinely unexplained. Should flag 24, not the raw 45.
        prev = m.TaskEntry(id=1, employee_id=1, date=TODAY, project_id=1, task_type_id=1,
                           details="a", start_minute=690, end_minute=775)
        cur = m.TaskEntry(id=2, employee_id=1, date=TODAY, project_id=1, task_type_id=1,
                          details="b", start_minute=820, end_minute=870)
        brk = m.BreakEntry(employee_id=1, date=TODAY, break_type=m.BREAK_LUNCH_DINNER,
                           start_minute=777, end_minute=798)
        assert gap_flags([prev, cur], 15, [brk]) == {2: 24}

    def test_no_breaks_passed_flags_full_raw_gap(self):
        # breaks=None (default) behaves exactly like before this feature —
        # nothing nets out, full raw gap is flagged.
        a = m.TaskEntry(id=1, employee_id=1, date=TODAY, project_id=1, task_type_id=1,
                        details="a", start_minute=540, end_minute=600)
        b = m.TaskEntry(id=2, employee_id=1, date=TODAY, project_id=1, task_type_id=1,
                        details="b", start_minute=620, end_minute=680)
        assert gap_flags([a, b], 15) == {2: 20}
        assert gap_flags([a, b], 15, []) == {2: 20}


class TestEntryDetailsEditGuard:
    """validation.entry_details_edit_error() — Ganesh, 2026-08-10: rows were
    delete-and-re-add only before; this is the guard behind the new
    in-place Details edit (routes/employee.py's edit_entry_details).
    Ownership itself is checked by the route before this is ever called,
    same as delete_entry's existing pattern — not this function's job."""

    def _entry(self, date):
        return m.TaskEntry(employee_id=1, date=date, project_id=1, task_type_id=1,
                           details="x", start_minute=540, end_minute=600)

    def test_own_entry_today_unlocked_is_allowed(self, db):
        s, emp = db
        assert entry_details_edit_error(self._entry(TODAY), emp, TODAY, None) is None

    def test_own_entry_yesterday_is_blocked(self, db):
        s, emp = db
        yesterday = TODAY - dt.timedelta(days=1)
        err = entry_details_edit_error(self._entry(yesterday), emp, TODAY, None)
        assert err is not None and "today" in err.lower()

    def test_own_entry_future_backdated_view_is_blocked_too(self, db):
        # today-only means exactly today, not "today or anything else
        # currently on screen" — a day in the future (shouldn't normally
        # happen, but the guard doesn't special-case it) is also not today
        s, emp = db
        tomorrow = TODAY + dt.timedelta(days=1)
        err = entry_details_edit_error(self._entry(tomorrow), emp, TODAY, None)
        assert err is not None

    def test_locked_day_is_blocked_even_if_today(self, db):
        s, emp = db
        sub = m.DaySubmission(employee_id=1, date=TODAY, locked=True)
        err = entry_details_edit_error(self._entry(TODAY), emp, TODAY, sub)
        assert err is not None and "locked" in err.lower()

    def test_unlocked_submission_today_is_allowed(self, db):
        # a DaySubmission row exists (day was submitted then unlocked by an
        # admin) but locked=False -> editing is allowed again
        s, emp = db
        sub = m.DaySubmission(employee_id=1, date=TODAY, locked=False)
        assert entry_details_edit_error(self._entry(TODAY), emp, TODAY, sub) is None

    def test_admin_bypasses_today_only_rule(self, db):
        s, emp = db
        emp.is_admin = True
        yesterday = TODAY - dt.timedelta(days=1)
        assert entry_details_edit_error(self._entry(yesterday), emp, TODAY, None) is None

    def test_admin_bypasses_lock_rule(self, db):
        s, emp = db
        emp.is_admin = True
        sub = m.DaySubmission(employee_id=1, date=TODAY, locked=True)
        assert entry_details_edit_error(self._entry(TODAY), emp, TODAY, sub) is None
