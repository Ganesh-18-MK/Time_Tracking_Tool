"""Small pure-function helpers in app/util.py."""
from app.util import clamp_break_end


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
