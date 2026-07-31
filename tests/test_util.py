"""Small pure-function helpers in app/util.py."""
from app.util import (
    clamp_break_end,
    flags_to_role,
    mask_tail,
    overtime_minutes,
    punch_remaining_minutes,
    role_to_flags,
)


class TestClampBreakEnd:
    def test_normal_break_same_day(self):
        # 9:43 PM (1303) -> 9:48 PM (1308): a real 5-minute break
        assert clamp_break_end(1303, 1308) == 1308

    def test_same_minute_break_is_not_a_wraparound(self):
        # regression: started and ended within the same clock minute used
        # to get clamped to end-of-day, fabricating a multi-hour duration
        assert clamp_break_end(1303, 1303) == 1303

    def test_genuine_midnight_wraparound_clamps_to_end_of_day(self):
        # started 23:58 (1438), ended 00:02 next calendar day (2)
        assert clamp_break_end(1438, 2) == 1440


class TestMaskTail:
    def test_blank_reads_as_not_added_yet(self):
        assert mask_tail("") == "Not added yet"
        assert mask_tail(None) == "Not added yet"

    def test_long_value_shows_last_four(self):
        assert mask_tail("1234567890123") == "•••••••••0123"

    def test_full_value_never_appears_in_output(self):
        assert "1234567890123" not in mask_tail("1234567890123")

    def test_value_shorter_than_keep_is_fully_masked(self):
        # nothing safe to reveal if the whole value is <= the keep length
        assert mask_tail("123") == "•••"

    def test_value_exactly_keep_length_is_fully_masked(self):
        assert mask_tail("1234") == "••••"

    def test_custom_keep_length(self):
        assert mask_tail("ABCDE1234F", keep=2) == "••••••••4F"

    def test_whitespace_only_reads_as_not_added_yet(self):
        assert mask_tail("   ") == "Not added yet"


class TestPunchRemainingMinutes:
    def test_nothing_punched_yet_shows_full_target(self):
        assert punch_remaining_minutes(480, 0) == 480

    def test_partial_progress_reduces_remaining(self):
        assert punch_remaining_minutes(480, 60) == 420

    def test_exactly_on_target_is_zero(self):
        assert punch_remaining_minutes(480, 480) == 0

    def test_overtime_goes_negative_not_clamped(self):
        # must stay visible as overtime, not silently read as "done"
        assert punch_remaining_minutes(480, 500) == -20

    def test_break_excess_already_folded_into_target(self):
        # the caller passes an already-adjusted target (base + break
        # excess) — this function does no break math of its own
        assert punch_remaining_minutes(480 + 10, 60) == 430


class TestOvertimeMinutes:
    def test_under_target_is_zero_not_negative(self):
        assert overtime_minutes(400, 480) == 0

    def test_exactly_on_target_is_zero(self):
        assert overtime_minutes(480, 480) == 0

    def test_over_target_is_the_excess(self):
        assert overtime_minutes(500, 480) == 20

    def test_unknown_target_reads_as_no_overtime_not_all_overtime(self):
        # a legacy-imported day with no computed target — must not count
        # the entire punched duration as overtime just because target is
        # unknown
        assert overtime_minutes(500, None) == 0

    def test_zero_target_full_day_leave_counts_any_punched_time_as_overtime(self):
        # a full-day-leave day has target 0 (see app/engine.py compute_day)
        # — any punched time that day is, by definition, all overtime
        assert overtime_minutes(60, 0) == 60


class TestRoleToFlags:
    """Three-tier admin role (Ganesh, 2026-07-31) — see Employee.is_super_admin
    docstring. 'admin' is department-scoped, 'super_admin' is org-wide."""

    def test_employee_has_neither_flag(self):
        assert role_to_flags("employee") == (False, False)

    def test_admin_is_admin_but_not_super(self):
        assert role_to_flags("admin") == (True, False)

    def test_super_admin_has_both_flags(self):
        assert role_to_flags("super_admin") == (True, True)

    def test_unrecognized_value_defaults_to_employee(self):
        # never silently grants admin access on a typo'd/garbage role string
        assert role_to_flags("garbage") == (False, False)


class TestFlagsToRole:
    def test_neither_flag_is_employee(self):
        assert flags_to_role(False, False) == "employee"

    def test_admin_only_is_admin(self):
        assert flags_to_role(True, False) == "admin"

    def test_both_flags_is_super_admin(self):
        assert flags_to_role(True, True) == "super_admin"

    def test_super_admin_flag_without_admin_flag_reads_as_employee(self):
        # shouldn't happen in practice (is_super_admin implies is_admin
        # everywhere it's set), but must not silently grant admin display
        assert flags_to_role(False, True) == "employee"

    def test_round_trips_through_role_to_flags(self):
        for role in ("employee", "admin", "super_admin"):
            assert flags_to_role(*role_to_flags(role)) == role
