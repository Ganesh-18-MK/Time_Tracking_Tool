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
    earliest_gap_window,
    entry_details_edit_error,
    gap_flags,
    suggest_non_overlapping_start,
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


class TestProjectScopedTasks:
    """ProjectTask (Ganesh, 2026-08-27, see that model's docstring in
    app/models.py) — a task with NO links is unrestricted (every existing
    fixture row here, e.g. TaskType id=1 "Check emails", has none, which
    is exactly what keeps every other test class in this file passing
    unmodified). These tests add a second Project + a linked TaskType to
    exercise the actual restriction."""

    @pytest.fixture()
    def db(self, db):
        s, emp = db
        s.add(m.Project(id=5, name="Second Client, Inc."))
        s.add(m.TaskType(id=3, name="Client-Specific Task"))
        s.commit()
        s.add(m.ProjectTask(project_id=1, task_type_id=3, created_by="test"))
        s.commit()
        return s, emp

    def test_unrestricted_task_usable_under_any_project(self, db):
        s, emp = db
        v(s, emp, project_id=1, task_type_id=1)  # TaskType id=1 has no links
        v(s, emp, project_id=5, task_type_id=1)  # still fine under the other project too

    def test_restricted_task_usable_under_its_linked_project(self, db):
        s, emp = db
        v(s, emp, project_id=1, task_type_id=3)  # linked to project 1 above

    def test_restricted_task_rejected_under_an_unlinked_project(self, db):
        s, emp = db
        assert "isn't set up for this Project" in errs(s, emp, project_id=5, task_type_id=3)


class TestDepartmentScopedProjects:
    """ProjectDepartment (Ganesh, 2026-08-28, see that model's docstring in
    app/models.py) — a project with NO department links is unrestricted
    (every existing fixture Project here has none, which is exactly what
    keeps every other test class in this file passing unmodified). No
    pytest coverage existed for this feature before now (see CLAUDE.md's
    own 2026-08-28 bullet — it shipped verified only by hand-trace); added
    here alongside the 2026-09-03 closing_existing bugfix below since both
    touch the exact same check."""

    @pytest.fixture()
    def db(self, db):
        s, emp = db
        emp.department = "Ops"
        s.add(m.Project(id=6, name="Ops-Only Client"))
        s.commit()
        s.add(m.ProjectDepartment(project_id=6, department="Ops", created_by="test"))
        s.commit()
        return s, emp

    def test_unrestricted_project_usable_by_any_department(self, db):
        s, emp = db
        v(s, emp, project_id=1, task_type_id=1)  # Project id=1 has no department links

    def test_restricted_project_usable_by_its_linked_department(self, db):
        s, emp = db
        v(s, emp, project_id=6, task_type_id=1)  # emp.department == "Ops", linked above

    def test_restricted_project_rejected_for_a_different_department(self, db):
        s, emp = db
        emp.department = "Front Desk"
        assert "isn't available to your department" in errs(s, emp, project_id=6, task_type_id=1)


class TestClosingExistingBypass:
    """Bug fix (Ganesh, 2026-09-03) — an employee had a timer already
    running against a project; an admin then restricted that project to a
    different department (or unlinked its task) while the timer was still
    open. Stop/Pause on that timer — and the auto-close that happens
    before starting anything else — used to fail outright, since they all
    route through this same validate_entry() check with no way to say
    "this pairing isn't a new pick, it was already running." permanently
    stranding the employee: nothing could close the timer, and nothing
    else could start until it was closed. closing_existing=True is the
    fix — see _log_timer_as_entry()'s docstring in app/routes/employee.py
    for the real caller."""

    def test_department_restriction_added_after_the_fact_still_blocks_a_fresh_pick(self, db):
        s, emp = db
        emp.department = "Ops"
        s.add(m.Project(id=6, name="Ops-Only Client"))
        s.commit()
        s.add(m.ProjectDepartment(project_id=6, department="Ops", created_by="test"))
        s.commit()
        emp.department = "Front Desk"  # admin moved her, or restricted the project after she started
        assert "isn't available to your department" in errs(s, emp, project_id=6, task_type_id=1)

    def test_closing_existing_bypasses_the_department_restriction(self, db):
        s, emp = db
        emp.department = "Ops"
        s.add(m.Project(id=6, name="Ops-Only Client"))
        s.commit()
        s.add(m.ProjectDepartment(project_id=6, department="Ops", created_by="test"))
        s.commit()
        emp.department = "Front Desk"
        v(s, emp, project_id=6, task_type_id=1, closing_existing=True)  # no error raised

    def test_closing_existing_bypasses_the_task_project_restriction(self, db):
        s, emp = db
        s.add(m.Project(id=5, name="Second Client, Inc."))
        s.add(m.TaskType(id=3, name="Client-Specific Task"))
        s.commit()
        s.add(m.ProjectTask(project_id=1, task_type_id=3, created_by="test"))
        s.commit()
        # task_type_id=3 is only linked to project 1, not project 5 — a
        # fresh pick of this pair is rejected...
        assert "isn't set up for this Project" in errs(s, emp, project_id=5, task_type_id=3)
        # ...but closing out an already-running one against it succeeds.
        v(s, emp, project_id=5, task_type_id=3, closing_existing=True)

    def test_closing_existing_does_not_bypass_unrelated_checks(self, db):
        s, emp = db
        # locked day, overlap, cap, backdate — none of those are about
        # "is this a fresh pick," so closing_existing must never soften them.
        assert "after Start" in errs(
            s, emp, start_minute=600, end_minute=540, closing_existing=True
        )

    def test_closing_existing_bypasses_the_new_active_timer_check(self, db):
        # 2026-09-03, same day as the Add-Task-vs-running-timer bugfix
        # (TestActiveTimerOverlap below) — _log_timer_as_entry() always
        # passes closing_existing=True to close out THE one active timer a
        # given employee can have; that call's own final segment trivially
        # "overlaps" itself (same start_minute), so this check must not
        # fire for it, or Stop/Pause could never succeed at all.
        s, emp = db
        s.add(m.ActiveTaskTimer(employee_id=1, date=TODAY, project_id=1, task_type_id=1,
                                 start_minute=540, started_at=dt.datetime.utcnow()))
        s.commit()
        v(s, emp, start_minute=540, end_minute=600, closing_existing=True)  # no error raised


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


class TestActiveTimerOverlap:
    """Bug fix (Ganesh, 2026-09-03, from a screenshot): Auto time capture
    started 11:50 AM; the employee manually Added Task 11:50 AM-12:20 PM
    while it was still running. Nothing checked that manual row against the
    live timer, so it saved fine — then Stopping the timer failed with
    "Overlaps existing row 11:50 AM-12:20 PM" once the timer's own real
    segment tried to log across that same window, leaving the employee
    stuck (Pause/Stop both fail the same way; Cancel is the only way out,
    and it discards the timer's real elapsed time). Fixed by treating a
    currently-running ActiveTaskTimer exactly like an open/still-running
    BreakEntry above: it blocks from its start to end of day, since its
    real end isn't known yet either."""

    def test_entry_overlapping_a_running_timer_is_blocked(self, db):
        s, emp = db
        s.add(m.ActiveTaskTimer(employee_id=1, date=TODAY, project_id=1, task_type_id=1,
                                 start_minute=710, started_at=dt.datetime.utcnow()))  # 11:50 AM
        s.commit()
        msg = errs(s, emp, start_minute=710, end_minute=750)  # 11:50 AM-12:30 PM
        assert "Auto time capture is currently running" in msg

    def test_entry_starting_before_and_running_into_the_timer_is_blocked(self, db):
        s, emp = db
        s.add(m.ActiveTaskTimer(employee_id=1, date=TODAY, project_id=1, task_type_id=1,
                                 start_minute=710, started_at=dt.datetime.utcnow()))
        s.commit()
        assert "Auto time capture is currently running" in errs(s, emp, start_minute=690, end_minute=720)

    def test_entry_entirely_before_the_timer_starts_is_allowed(self, db):
        s, emp = db
        s.add(m.ActiveTaskTimer(employee_id=1, date=TODAY, project_id=1, task_type_id=1,
                                 start_minute=710, started_at=dt.datetime.utcnow()))
        s.commit()
        v(s, emp, start_minute=600, end_minute=710)  # ends exactly when the timer starts — fine

    def test_entry_on_a_different_day_than_the_timer_is_unaffected(self, db):
        s, emp = db
        s.add(m.ActiveTaskTimer(employee_id=1, date=TODAY, project_id=1, task_type_id=1,
                                 start_minute=710, started_at=dt.datetime.utcnow()))
        s.commit()
        yesterday = TODAY - dt.timedelta(days=1)
        v(s, emp, date=yesterday, start_minute=600, end_minute=660)

    def test_no_timer_running_is_unaffected(self, db):
        s, emp = db
        v(*db)  # no ActiveTaskTimer row at all — the whole check is a no-op


class TestSuggestNonOverlappingStart:
    """Ganesh, 2026-08-14: a failed Add Row used to reset the whole form and
    leave the employee to guess a new time by trial and error.
    suggest_non_overlapping_start() mirrors validate_entry's own overlap
    conditions (TaskEntry rows + BreakEntry rows, touching a boundary
    allowed) to hand back the earliest minute that actually clears every
    conflict — see add_entry()'s _reopen() in app/routes/employee.py."""

    def test_overlapping_task_entry_suggests_its_end(self, db):
        s, emp = db
        s.add(m.TaskEntry(employee_id=1, date=TODAY, project_id=1, task_type_id=1,
                          details="existing row", start_minute=540, end_minute=600))
        s.commit()
        assert suggest_non_overlapping_start(s, emp, TODAY, 550, 650) == 600

    def test_touching_a_row_exactly_at_its_end_is_not_a_conflict(self, db):
        s, emp = db
        s.add(m.TaskEntry(employee_id=1, date=TODAY, project_id=1, task_type_id=1,
                          details="existing row", start_minute=540, end_minute=600))
        s.commit()
        assert suggest_non_overlapping_start(s, emp, TODAY, 600, 650) is None

    def test_overlapping_a_break_suggests_its_end(self, db):
        s, emp = db
        s.add(m.BreakEntry(employee_id=1, date=TODAY, break_type=m.BREAK_PERSONAL,
                            start_minute=600, end_minute=630))
        s.commit()
        assert suggest_non_overlapping_start(s, emp, TODAY, 610, 700) == 630

    def test_conflicting_with_both_task_and_break_takes_the_later_end(self, db):
        s, emp = db
        s.add(m.TaskEntry(employee_id=1, date=TODAY, project_id=1, task_type_id=1,
                          details="existing row", start_minute=540, end_minute=600))
        s.add(m.BreakEntry(employee_id=1, date=TODAY, break_type=m.BREAK_PERSONAL,
                            start_minute=650, end_minute=700))
        s.commit()
        assert suggest_non_overlapping_start(s, emp, TODAY, 590, 750) == 700

    def test_no_conflict_returns_none(self, db):
        s, emp = db
        s.add(m.TaskEntry(employee_id=1, date=TODAY, project_id=1, task_type_id=1,
                          details="existing row", start_minute=540, end_minute=600))
        s.commit()
        assert suggest_non_overlapping_start(s, emp, TODAY, 700, 800) is None

    def test_still_open_break_extends_to_end_of_day(self, db):
        s, emp = db
        s.add(m.BreakEntry(employee_id=1, date=TODAY, break_type=m.BREAK_PERSONAL,
                            start_minute=600, end_minute=None))
        s.commit()
        assert suggest_non_overlapping_start(s, emp, TODAY, 610, 650) == 1440

    def test_running_timer_extends_to_end_of_day(self, db):
        # Ganesh, 2026-09-03 bugfix — same "unknown real end" reasoning as
        # an open break just above
        s, emp = db
        s.add(m.ActiveTaskTimer(employee_id=1, date=TODAY, project_id=1, task_type_id=1,
                                 start_minute=710, started_at=dt.datetime.utcnow()))
        s.commit()
        assert suggest_non_overlapping_start(s, emp, TODAY, 700, 750) == 1440

    def test_editing_own_row_excludes_itself_via_entry_id(self, db):
        s, emp = db
        e = m.TaskEntry(employee_id=1, date=TODAY, project_id=1, task_type_id=1,
                        details="existing row", start_minute=540, end_minute=600)
        s.add(e)
        s.commit()
        assert suggest_non_overlapping_start(s, emp, TODAY, 540, 600, entry_id=e.id) is None

    def test_invalid_time_range_returns_none(self, db):
        s, emp = db
        assert suggest_non_overlapping_start(s, emp, TODAY, 600, 600) is None
        assert suggest_non_overlapping_start(s, emp, TODAY, 700, 600) is None


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


class TestEarliestGapWindow:
    """earliest_gap_window() — feeds Today's Add Row auto-prefill (Ganesh,
    2026-08-21): an unexplained gap between two logged rows now scopes the
    Add Row form to that window automatically instead of only showing the
    ⚠ warning label. Always paired with gap_flags()'s own output for the
    same entries, same as today_page() calls it."""

    def test_no_entries_no_window(self):
        assert earliest_gap_window([], {}) is None

    def test_no_flagged_gap_returns_none(self):
        a = m.TaskEntry(id=1, employee_id=1, date=TODAY, project_id=1, task_type_id=1,
                        details="a", start_minute=540, end_minute=600)
        b = m.TaskEntry(id=2, employee_id=1, date=TODAY, project_id=1, task_type_id=1,
                        details="b", start_minute=615, end_minute=680)
        assert earliest_gap_window([a, b], gap_flags([a, b], 15)) is None

    def test_flagged_gap_returns_prev_end_to_cur_start(self):
        a = m.TaskEntry(id=1, employee_id=1, date=TODAY, project_id=1, task_type_id=1,
                        details="a", start_minute=540, end_minute=600)
        b = m.TaskEntry(id=2, employee_id=1, date=TODAY, project_id=1, task_type_id=1,
                        details="b", start_minute=720, end_minute=780)
        flags = gap_flags([a, b], 15)
        assert earliest_gap_window([a, b], flags) == (600, 720)

    def test_picks_the_earliest_gap_when_several_exist(self):
        a = m.TaskEntry(id=1, employee_id=1, date=TODAY, project_id=1, task_type_id=1,
                        details="a", start_minute=540, end_minute=600)   # 9:00-10:00
        b = m.TaskEntry(id=2, employee_id=1, date=TODAY, project_id=1, task_type_id=1,
                        details="b", start_minute=660, end_minute=720)   # 11:00-12:00 (gap before: 60min)
        c = m.TaskEntry(id=3, employee_id=1, date=TODAY, project_id=1, task_type_id=1,
                        details="c", start_minute=800, end_minute=860)   # 13:20-14:20 (gap before: 80min)
        flags = gap_flags([a, b, c], 15)
        assert flags == {2: 60, 3: 80}
        # earliest by position in the day, not by size of the gap
        assert earliest_gap_window([a, b, c], flags) == (600, 660)

    def test_unordered_input_list_still_finds_the_right_window(self):
        # entries aren't guaranteed to arrive already sorted — gap_flags()
        # itself sorts internally, and so must this.
        a = m.TaskEntry(id=1, employee_id=1, date=TODAY, project_id=1, task_type_id=1,
                        details="a", start_minute=540, end_minute=600)
        b = m.TaskEntry(id=2, employee_id=1, date=TODAY, project_id=1, task_type_id=1,
                        details="b", start_minute=720, end_minute=780)
        flags = gap_flags([a, b], 15)
        assert earliest_gap_window([b, a], flags) == (600, 720)

    def test_right_open_trailing_gap_is_not_a_window(self):
        # nothing logged yet after the last row isn't "between two rows" —
        # gap_flags() itself never flags this (there's no `cur` row to
        # flag), so this is really just confirming the None default holds.
        a = m.TaskEntry(id=1, employee_id=1, date=TODAY, project_id=1, task_type_id=1,
                        details="a", start_minute=540, end_minute=600)
        assert earliest_gap_window([a], gap_flags([a], 15)) is None


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

    def test_works_unchanged_for_a_breakentry_too(self, db):
        """Ganesh, 2026-08-14: the same guard now also gates editing a
        break's optional note (app/routes/employee.py's edit_break_details)
        — both models have a plain `.date` column, so no BreakEntry-specific
        branch was needed, just a wider type hint."""
        s, emp = db
        brk_today = m.BreakEntry(employee_id=1, date=TODAY, break_type=m.BREAK_PERSONAL,
                                  start_minute=600, end_minute=630)
        assert entry_details_edit_error(brk_today, emp, TODAY, None) is None

        yesterday = TODAY - dt.timedelta(days=1)
        brk_yesterday = m.BreakEntry(employee_id=1, date=yesterday, break_type=m.BREAK_PERSONAL,
                                      start_minute=600, end_minute=630)
        err = entry_details_edit_error(brk_yesterday, emp, TODAY, None)
        assert err is not None and "today" in err.lower()

        sub = m.DaySubmission(employee_id=1, date=TODAY, locked=True)
        err = entry_details_edit_error(brk_today, emp, TODAY, sub)
        assert err is not None and "locked" in err.lower()
