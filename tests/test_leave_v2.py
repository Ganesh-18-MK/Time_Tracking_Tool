"""Leave Management V2 (Ganesh, 2026-08-21 — see docs/LEAVE_MANAGEMENT_PLAN.md).

Pure date/number math (full_months_elapsed, years_of_service_on,
planned_time_accrued_minutes_pure, required_notice_working_days) is tested
without a database at all — these were also verified standalone against
the exact minutes table in the plan's §2 in the sandbox that wrote this
file (no fastapi/sqlalchemy available there), and this file is the real
pytest version of those same checks, so `pytest tests/test_leave_v2.py -q`
is the thing that actually needs to go green before
LEAVE_MANAGEMENT_V2_ENABLED flips on.

DB-backed pieces (leave_balance_v2, is_probation_active,
notice_period_satisfied, effective_leave_type) use the same in-memory
sqlite fixture pattern as test_compensation.py/test_reports.py.
"""
import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import engine, models as m
from app.db import Base

CFG = {
    "planned_days_year_0_2": "9",
    "planned_days_year_2_5": "11",
    "planned_days_year_5_plus": "13",
    "probation_days_default": "90",
}


@pytest.fixture()
def db():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


@pytest.fixture()
def emp(db):
    e = m.Employee(
        name="Priya", department="Ops", daily_target_minutes=480,
        work_days="0,1,2,3,4", start_date=dt.date(2020, 1, 1),
    )
    db.add(e)
    db.commit()
    return e


# ---- full_months_elapsed ----------------------------------------------------
class TestFullMonthsElapsed:
    def test_zero_before_start(self):
        assert engine.full_months_elapsed(dt.date(2026, 3, 1), dt.date(2026, 2, 1)) == 0

    def test_month_starting_on_the_1st_counts_once_it_ends(self):
        start = dt.date(2026, 4, 1)
        assert engine.full_months_elapsed(start, dt.date(2026, 4, 29)) == 0
        assert engine.full_months_elapsed(start, dt.date(2026, 4, 30)) == 1

    def test_mid_month_start_skips_the_partial_first_month(self):
        start = dt.date(2026, 1, 15)
        # January is never fully in [start, ...) since it began before start
        assert engine.full_months_elapsed(start, dt.date(2026, 1, 31)) == 0
        assert engine.full_months_elapsed(start, dt.date(2026, 2, 15)) == 0
        assert engine.full_months_elapsed(start, dt.date(2026, 2, 28)) == 1

    def test_multiple_full_months(self):
        assert engine.full_months_elapsed(dt.date(2026, 1, 1), dt.date(2026, 6, 30)) == 6


# ---- accrual band math, matching docs/LEAVE_MANAGEMENT_PLAN.md §2 ----------
class TestPlannedTimeAccrual:
    def test_0_2_year_band_is_360_minutes_per_month_at_8h_day(self):
        start = dt.date(2025, 4, 1)
        total = engine.planned_time_accrued_minutes_pure(start, 0, 480, CFG, dt.date(2025, 4, 30))
        assert total == 360

    def test_2_5_year_band_is_440_minutes_per_month_at_8h_day(self):
        start = dt.date(2020, 1, 1)  # 2-year mark is 2022-01-01
        one_month = (
            engine.planned_time_accrued_minutes_pure(start, 0, 480, CFG, dt.date(2022, 2, 28))
            - engine.planned_time_accrued_minutes_pure(start, 0, 480, CFG, dt.date(2022, 1, 31))
        )
        assert one_month == 440

    def test_5_plus_year_band_is_520_minutes_per_month_at_8h_day(self):
        start = dt.date(2015, 1, 1)
        one_month = (
            engine.planned_time_accrued_minutes_pure(start, 0, 480, CFG, dt.date(2022, 2, 28))
            - engine.planned_time_accrued_minutes_pure(start, 0, 480, CFG, dt.date(2022, 1, 31))
        )
        assert one_month == 520

    def test_no_accrual_during_probation(self):
        start = dt.date(2026, 1, 1)
        assert engine.planned_time_accrued_minutes_pure(start, 90, 480, CFG, dt.date(2026, 3, 15)) == 0

    def test_accrual_not_backdated_into_probation_window(self):
        # probation ends 2026-03-31 (day=31, not the 1st) -> April is the
        # first candidate month, not March
        start = dt.date(2026, 1, 1)
        total = engine.planned_time_accrued_minutes_pure(start, 90, 480, CFG, dt.date(2026, 4, 30))
        assert total == 360  # exactly one month (April), not April+partial-March

    def test_running_total_does_not_reset_on_january_1st(self):
        start = dt.date(2024, 1, 1)
        before = engine.planned_time_accrued_minutes_pure(start, 0, 480, CFG, dt.date(2024, 12, 31))
        after = engine.planned_time_accrued_minutes_pure(start, 0, 480, CFG, dt.date(2025, 1, 31))
        assert after == before + 360  # one more month credited, no reset dip

    def test_wrapper_reads_probation_default_from_config_when_unset(self, db, emp):
        # emp.probation_days is None -> falls back to cfg's probation_days_default
        emp.start_date = dt.date(2026, 1, 1)
        as_of = dt.date(2026, 3, 15)  # within the 90-day default probation
        assert engine.planned_time_accrued_minutes(db, emp, CFG, as_of) == 0

    def test_wrapper_respects_per_employee_probation_override(self, db, emp):
        emp.start_date = dt.date(2026, 1, 1)
        emp.probation_days = 10  # much shorter than the 90-day default
        as_of = dt.date(2026, 3, 15)
        assert engine.planned_time_accrued_minutes(db, emp, CFG, as_of) > 0


# ---- Unplanned Time proration (Ganesh, 2026-09-04) ----------------------------
class TestUnplannedTimeProration:
    def test_zero_on_january_first(self):
        # No full calendar month has elapsed yet -> the known, accepted
        # 0-entitlement-in-January consequence documented on the function.
        assert engine.unplanned_time_prorated_entitlement_minutes(CFG, dt.date(2026, 1, 1)) == 0

    def test_zero_through_the_second_to_last_day_of_january(self):
        assert engine.unplanned_time_prorated_entitlement_minutes(CFG, dt.date(2026, 1, 30)) == 0

    def test_one_twelfth_the_moment_january_fully_elapses_on_its_last_day(self):
        # Same "a month counts once its last day has passed" rule
        # full_months_elapsed() already applies elsewhere (Planned Time's
        # own accrual for a hire on the 1st) -> Jan 31 itself, not Feb 1,
        # is when the jump to one full month's worth (2400/12 = 200
        # minutes) happens.
        assert engine.unplanned_time_prorated_entitlement_minutes(CFG, dt.date(2026, 1, 31)) == 200
        assert engine.unplanned_time_prorated_entitlement_minutes(CFG, dt.date(2026, 2, 1)) == 200

    def test_five_months_by_mid_june(self):
        # Matches TestLeaveBalanceV2.test_unplanned_type_entitlement_is_prorated_not_flat
        assert engine.unplanned_time_prorated_entitlement_minutes(CFG, dt.date(2026, 6, 15)) == 1000

    def test_full_cap_only_on_the_last_day_of_the_year(self):
        assert engine.unplanned_time_prorated_entitlement_minutes(CFG, dt.date(2026, 12, 30)) == 2200  # 11 months
        assert engine.unplanned_time_prorated_entitlement_minutes(CFG, dt.date(2026, 12, 31)) == 2400  # 12 months

    def test_new_hire_never_inherits_months_before_their_own_start_date(self):
        # Hired 2026-07-01 (a start-of-month hire, so July itself is
        # eligible once it ends): only July and August have fully elapsed
        # by 2026-09-04 -> 2 months, not the 8 months that have elapsed
        # since Jan 1 org-wide.
        assert engine.unplanned_time_prorated_entitlement_minutes(
            CFG, dt.date(2026, 9, 4), hire_date=dt.date(2026, 7, 1)
        ) == 400

    def test_new_hire_hired_before_january_uses_calendar_year_floor_not_hire_date(self):
        # A long-tenured employee's hire_date is irrelevant once it's
        # earlier than Jan 1 of as_of's year -> falls back to the plain
        # calendar-year calculation, same as passing hire_date=None.
        with_hire = engine.unplanned_time_prorated_entitlement_minutes(
            CFG, dt.date(2026, 6, 15), hire_date=dt.date(2020, 1, 1)
        )
        without_hire = engine.unplanned_time_prorated_entitlement_minutes(CFG, dt.date(2026, 6, 15))
        assert with_hire == without_hire == 1000

    def test_mid_month_hire_skips_the_partial_first_month(self):
        # Hired 2026-01-15: January is never a full month for this
        # employee (same "no partial first month" rule Planned Time's own
        # accrual uses) -> February is their first candidate month.
        assert engine.unplanned_time_prorated_entitlement_minutes(
            CFG, dt.date(2026, 2, 15), hire_date=dt.date(2026, 1, 15)
        ) == 0
        assert engine.unplanned_time_prorated_entitlement_minutes(
            CFG, dt.date(2026, 2, 28), hire_date=dt.date(2026, 1, 15)
        ) == 200

    def test_respects_a_non_default_configured_cap(self):
        cfg = dict(CFG, unplanned_hours_year_cap="24")
        # 24h = 1440 minutes / 12 = 120 minutes/month; 6 full months
        # (Jan-Jun) have elapsed by Jun 30 itself -> 6 * 120 = 720
        assert engine.unplanned_time_prorated_entitlement_minutes(cfg, dt.date(2026, 6, 30)) == 720


# ---- probation gate ----------------------------------------------------------
class TestProbation:
    def test_active_during_window(self, emp):
        emp.start_date = dt.date(2026, 1, 1)
        emp.probation_days = 90
        assert engine.is_probation_active(emp, dt.date(2026, 3, 15), CFG) is True

    def test_inactive_after_window(self, emp):
        emp.start_date = dt.date(2026, 1, 1)
        emp.probation_days = 90
        assert engine.is_probation_active(emp, dt.date(2026, 4, 15), CFG) is False

    def test_falls_back_to_config_default_when_unset(self, emp):
        emp.start_date = dt.date(2026, 1, 1)
        emp.probation_days = None
        assert engine.is_probation_active(emp, dt.date(2026, 3, 15), CFG) is True
        assert engine.is_probation_active(emp, dt.date(2026, 5, 1), CFG) is False


# ---- notice period ------------------------------------------------------------
class TestNoticePeriod:
    def test_tiers_match_the_plan(self):
        assert engine.required_notice_working_days(1) == 2
        assert engine.required_notice_working_days(2) == 7
        assert engine.required_notice_working_days(3) == 7
        assert engine.required_notice_working_days(4) == 15
        assert engine.required_notice_working_days(30) == 15

    def test_satisfied_when_enough_working_days_precede_the_request(self, emp):
        holidays = set()
        # Monday 2026-08-24 submitted for a 1-day leave starting Thursday
        # 2026-08-27 -> Tue+Wed = 2 working days' notice, exactly enough
        submitted = dt.date(2026, 8, 24)
        leave_start = dt.date(2026, 8, 27)
        assert engine.notice_period_satisfied(submitted, leave_start, 1, emp, holidays) is True

    def test_not_satisfied_with_too_little_notice(self, emp):
        submitted = dt.date(2026, 8, 26)
        leave_start = dt.date(2026, 8, 27)  # next day, 0 working days' notice
        assert engine.notice_period_satisfied(submitted, leave_start, 1, emp, holidays=set()) is False

    def test_holidays_and_weekends_dont_count_toward_notice(self, emp):
        # Fri 2026-08-21 submitted for Mon 2026-08-24 -> only Sat/Sun
        # between them, both non-working -> 0 working days, fails a 2-day
        # requirement even though 3 calendar days passed
        submitted = dt.date(2026, 8, 21)
        leave_start = dt.date(2026, 8, 24)
        assert engine.notice_period_satisfied(submitted, leave_start, 1, emp, holidays=set()) is False


# ---- PIP forces Unpaid --------------------------------------------------------
class TestPipForcesUnpaid:
    def test_normal_employee_keeps_requested_type(self, emp):
        emp.is_on_pip = False
        assert engine.effective_leave_type(emp, m.LEAVE_PLANNED) == m.LEAVE_PLANNED

    def test_pip_employee_forced_to_unpaid(self, emp):
        emp.is_on_pip = True
        assert engine.effective_leave_type(emp, m.LEAVE_PLANNED) == m.LEAVE_UNPAID
        assert engine.effective_leave_type(emp, m.LEAVE_BEREAVEMENT) == m.LEAVE_UNPAID

    def test_pip_does_not_override_special_paid_time(self, emp):
        # Special Paid Time is a management grant, not a request PIP should
        # be able to veto
        emp.is_on_pip = True
        assert engine.effective_leave_type(emp, m.LEAVE_SPECIAL_PAID) == m.LEAVE_SPECIAL_PAID


# ---- used / pending / remaining split -----------------------------------------
class TestLeaveBalanceV2:
    def test_pending_is_held_out_of_remaining_without_counting_as_used(self, db, emp):
        emp.start_date = dt.date(2020, 1, 1)
        db.add(m.LeaveRecord(
            employee_id=emp.id, start_date=dt.date(2026, 6, 1), end_date=dt.date(2026, 6, 1),
            type=m.LEAVE_PLANNED, minutes_per_day=240, status=m.LEAVE_REQUESTED,
        ))
        db.commit()
        bal = engine.leave_balance_v2(db, emp, as_of=dt.date(2026, 6, 15), cfg=CFG)
        planned = bal[m.LEAVE_PLANNED]
        assert planned["used"] == 0
        assert planned["pending"] == 240
        assert planned["remaining"] == planned["entitlement"] - 240

    def test_partial_approval_reduces_used_not_the_original_request(self, db, emp):
        emp.start_date = dt.date(2020, 1, 1)
        lv = m.LeaveRecord(
            employee_id=emp.id, start_date=dt.date(2026, 6, 1), end_date=dt.date(2026, 6, 1),
            type=m.LEAVE_PLANNED, minutes_per_day=480, status=m.LEAVE_APPROVED,
            approved_minutes_per_day=240,  # admin approved half of what was asked
        )
        db.add(lv)
        db.commit()
        bal = engine.leave_balance_v2(db, emp, as_of=dt.date(2026, 6, 15), cfg=CFG)
        assert bal[m.LEAVE_PLANNED]["used"] == 240  # not 480
        assert bal[m.LEAVE_PLANNED]["pending"] == 0

    def test_unplanned_type_entitlement_is_prorated_not_flat(self, db, emp):
        # emp.start_date is 2020-01-01 (well before as_of), so this is
        # purely calendar-based: 5 full months elapsed since 2026-01-01
        # by 2026-06-15 (Jan-May; June itself hasn't ended yet) -> 5/12 of
        # the default 40-hour (2400-minute) annual cap = 1000 minutes,
        # not the old flat 2400. See unplanned_time_prorated_entitlement_
        # minutes() for the exact rule (Ganesh, 2026-09-04).
        bal = engine.leave_balance_v2(db, emp, as_of=dt.date(2026, 6, 15), cfg=CFG)
        assert bal[m.LEAVE_UNPLANNED]["entitlement"] == 1000
        assert bal[m.LEAVE_UNPLANNED]["remaining"] == 1000

    def test_special_paid_entitlement_sums_grants(self, db, emp):
        db.add(m.SpecialPaidGrant(employee_id=emp.id, minutes=240, reason="Recognition award", granted_by="Admin"))
        db.add(m.SpecialPaidGrant(employee_id=emp.id, minutes=120, reason="Extra", granted_by="Admin"))
        db.commit()
        bal = engine.leave_balance_v2(db, emp, as_of=dt.date(2026, 6, 15), cfg=CFG)
        assert bal[m.LEAVE_SPECIAL_PAID]["entitlement"] == 360

    # ---- Deferred Unplanned-Time compensation (Ganesh, 2026-09-04) ---------
    def test_pending_compensation_is_excluded_from_used(self, db, emp):
        db.add(m.LeaveRecord(
            employee_id=emp.id, start_date=dt.date(2026, 6, 1), end_date=dt.date(2026, 6, 1),
            type=m.LEAVE_UNPLANNED, minutes_per_day=480, approved_minutes_per_day=480,
            status=m.LEAVE_APPROVED, wants_compensation=True,
            compensation_status=m.LEAVE_COMP_PENDING, compensation_minutes_needed=480,
            compensation_deadline=dt.date(2026, 6, 30),
        ))
        db.commit()
        bal = engine.leave_balance_v2(db, emp, as_of=dt.date(2026, 6, 15), cfg=CFG)
        assert bal[m.LEAVE_UNPLANNED]["used"] == 0

    def test_matched_compensation_is_excluded_from_used(self, db, emp):
        db.add(m.LeaveRecord(
            employee_id=emp.id, start_date=dt.date(2026, 6, 1), end_date=dt.date(2026, 6, 1),
            type=m.LEAVE_UNPLANNED, minutes_per_day=480, approved_minutes_per_day=480,
            status=m.LEAVE_APPROVED, wants_compensation=True,
            compensation_status=m.LEAVE_COMP_MATCHED, compensation_minutes_needed=480,
            compensation_deadline=dt.date(2026, 6, 30),
        ))
        db.commit()
        bal = engine.leave_balance_v2(db, emp, as_of=dt.date(2026, 6, 15), cfg=CFG)
        assert bal[m.LEAVE_UNPLANNED]["used"] == 0

    def test_resolved_unplanned_counts_normally(self, db, emp):
        db.add(m.LeaveRecord(
            employee_id=emp.id, start_date=dt.date(2026, 6, 1), end_date=dt.date(2026, 6, 1),
            type=m.LEAVE_UNPLANNED, minutes_per_day=480, approved_minutes_per_day=480,
            status=m.LEAVE_APPROVED, wants_compensation=True,
            compensation_status=m.LEAVE_COMP_RESOLVED_UNPLANNED, compensation_minutes_needed=480,
            compensation_deadline=dt.date(2026, 6, 30),
        ))
        db.commit()
        bal = engine.leave_balance_v2(db, emp, as_of=dt.date(2026, 6, 15), cfg=CFG)
        assert bal[m.LEAVE_UNPLANNED]["used"] == 480

    def test_resolved_unpaid_counts_under_unpaid_not_unplanned(self, db, emp):
        # resolve_leave_compensation() flips lv.type to LEAVE_UNPAID at
        # resolution time — this row is Unpaid Time from leave_balance_v2's
        # point of view with no special-casing needed for that branch.
        db.add(m.LeaveRecord(
            employee_id=emp.id, start_date=dt.date(2026, 6, 1), end_date=dt.date(2026, 6, 1),
            type=m.LEAVE_UNPAID, minutes_per_day=480, approved_minutes_per_day=480,
            status=m.LEAVE_APPROVED, wants_compensation=True,
            compensation_status=m.LEAVE_COMP_RESOLVED_UNPAID, compensation_minutes_needed=480,
            compensation_deadline=dt.date(2026, 6, 30),
        ))
        db.commit()
        bal = engine.leave_balance_v2(db, emp, as_of=dt.date(2026, 6, 15), cfg=CFG)
        assert bal[m.LEAVE_UNPLANNED]["used"] == 0
        assert bal[m.LEAVE_UNPAID]["used"] == 480

    def test_plain_unplanned_leave_without_compensation_is_unaffected(self, db, emp):
        # No regression: an ordinary Unplanned row that never opted into
        # deferral (compensation_status is None) counts exactly as before.
        db.add(m.LeaveRecord(
            employee_id=emp.id, start_date=dt.date(2026, 6, 1), end_date=dt.date(2026, 6, 1),
            type=m.LEAVE_UNPLANNED, minutes_per_day=480, approved_minutes_per_day=480,
            status=m.LEAVE_APPROVED,
        ))
        db.commit()
        bal = engine.leave_balance_v2(db, emp, as_of=dt.date(2026, 6, 15), cfg=CFG)
        assert bal[m.LEAVE_UNPLANNED]["used"] == 480


# ---- Deferred Unplanned-Time compensation match/allocation (Ganesh, 2026-09-04) ----
class TestDeferredCompensationMatch:
    """Ganesh's own worked example (see the AskUserQuestion explanation this
    feature was built from): an 8h Unplanned (Sick) day, approved with
    wants_compensation=True — the debt should not count against the 40-hour
    pool until/unless it goes unmatched past its deadline."""

    def _pending_leave(self, db, emp, needed_minutes=480, deadline=dt.date(2026, 6, 30)):
        lv = m.LeaveRecord(
            employee_id=emp.id, start_date=dt.date(2026, 6, 1), end_date=dt.date(2026, 6, 1),
            type=m.LEAVE_UNPLANNED, minutes_per_day=needed_minutes, approved_minutes_per_day=needed_minutes,
            status=m.LEAVE_APPROVED, wants_compensation=True,
            compensation_status=m.LEAVE_COMP_PENDING, compensation_minutes_needed=needed_minutes,
            compensation_deadline=deadline,
        )
        db.add(lv)
        db.commit()
        return lv

    def _surplus_day(self, db, emp, d, variance):
        db.add(m.DayStatus(employee_id=emp.id, date=d, status=m.COMPLETE,
                            target_minutes=480, variance_minutes=variance, source="computed"))
        db.commit()

    def test_allocate_surplus_minutes_sources_deficit_from_the_leave_debt_not_daystatus(self, db, emp):
        lv = self._pending_leave(db, emp, needed_minutes=480)
        # deliberately no DayStatus row at all on lv.start_date — a leave
        # day has no shortfall variance, since the approved leave already
        # zeroed its target; the deficit must come from the debt itself.
        self._surplus_day(db, emp, dt.date(2026, 6, 10), 600)  # 10h surplus
        allocation = engine.allocate_surplus_minutes(
            db, emp.id, lv.start_date, [dt.date(2026, 6, 10).isoformat()], leave_debt_id=lv.id,
        )
        assert allocation == {"2026-06-10": 480}  # takes only what's owed, not the full 10h

    def test_two_partial_links_together_fully_match_the_debt(self, db, emp):
        lv = self._pending_leave(db, emp, needed_minutes=480)  # 8h owed
        self._surplus_day(db, emp, dt.date(2026, 6, 5), 120)   # 2h
        self._surplus_day(db, emp, dt.date(2026, 6, 12), 360)  # 6h

        alloc1 = engine.allocate_surplus_minutes(db, emp.id, lv.start_date, [dt.date(2026, 6, 5).isoformat()], leave_debt_id=lv.id)
        link1 = m.CompensationLink(
            employee_id=emp.id, shortfall_date=lv.start_date, pending_leave_id=lv.id,
            surplus_dates="[]", surplus_minutes="{}",
        )
        import json as _json
        link1.surplus_dates = _json.dumps(sorted(alloc1.keys()))
        link1.surplus_minutes = _json.dumps(alloc1)
        db.add(link1)
        db.commit()
        engine.evaluate_link(db, link1)
        assert link1.fully_compensated is False  # only 2h of 8h so far
        assert lv.compensation_status == m.LEAVE_COMP_PENDING  # not matched yet

        alloc2 = engine.allocate_surplus_minutes(db, emp.id, lv.start_date, [dt.date(2026, 6, 12).isoformat()], leave_debt_id=lv.id)
        assert alloc2 == {"2026-06-12": 360}  # remaining 6h, not the day's full surplus (which happens to equal it here)
        link2 = m.CompensationLink(
            employee_id=emp.id, shortfall_date=lv.start_date, pending_leave_id=lv.id,
            surplus_dates=_json.dumps(sorted(alloc2.keys())), surplus_minutes=_json.dumps(alloc2),
        )
        db.add(link2)
        db.commit()
        engine.evaluate_link(db, link2)
        # this second, later link finishes off the debt two partial
        # links started — mirrors the exact aggregate-across-links
        # behavior compensated_dates()/evaluate_link() already guarantee
        # for an ordinary shortfall day.
        assert lv.compensation_status == m.LEAVE_COMP_MATCHED

    def test_leave_debt_and_ordinary_shortfall_cannot_double_spend_the_same_surplus_day(self, db, emp):
        # The exact double-spend risk flagged when this feature was
        # proposed: one surplus day can't pay off both an ordinary
        # shortfall day AND a deferred leave debt beyond its own total
        # variance, since surplus_minutes_used_by_date() sums across every
        # link for the employee regardless of what each one targets.
        lv = self._pending_leave(db, emp, needed_minutes=480)  # 8h owed
        self._surplus_day(db, emp, dt.date(2026, 6, 10), 480)  # exactly 8h surplus
        # An ordinary shortfall day the same employee also has, same month.
        db.add(m.DayStatus(employee_id=emp.id, date=dt.date(2026, 6, 3), status=m.PARTIAL,
                            target_minutes=480, variance_minutes=-240, source="computed"))
        db.commit()

        # First: link the entire 8h surplus day to the ordinary shortfall.
        alloc_shortfall = engine.allocate_surplus_minutes(
            db, emp.id, dt.date(2026, 6, 3), [dt.date(2026, 6, 10).isoformat()],
        )
        assert alloc_shortfall == {"2026-06-10": 240}  # only what that shortfall needs (4h), not the full 8h
        import json as _json
        link_shortfall = m.CompensationLink(
            employee_id=emp.id, shortfall_date=dt.date(2026, 6, 3),
            surplus_dates=_json.dumps(sorted(alloc_shortfall.keys())), surplus_minutes=_json.dumps(alloc_shortfall),
        )
        db.add(link_shortfall)
        db.commit()

        # Now the leave debt can only draw on what's LEFT of that same day (4h), not the original 8h.
        alloc_leave = engine.allocate_surplus_minutes(
            db, emp.id, lv.start_date, [dt.date(2026, 6, 10).isoformat()], leave_debt_id=lv.id,
        )
        assert alloc_leave == {"2026-06-10": 240}  # only the remaining 4h — the debt itself needs 8h, so this is partial

    def test_leave_debt_allocated_minutes_sums_across_every_link_for_that_debt(self, db, emp):
        lv = self._pending_leave(db, emp, needed_minutes=480)
        import json as _json
        db.add(m.CompensationLink(
            employee_id=emp.id, shortfall_date=lv.start_date, pending_leave_id=lv.id,
            surplus_dates=_json.dumps(["2026-06-05"]), surplus_minutes=_json.dumps({"2026-06-05": 120}),
        ))
        db.add(m.CompensationLink(
            employee_id=emp.id, shortfall_date=lv.start_date, pending_leave_id=lv.id,
            surplus_dates=_json.dumps(["2026-06-12"]), surplus_minutes=_json.dumps({"2026-06-12": 200}),
        ))
        # A different employee's/leave's link must never bleed into this total.
        db.add(m.CompensationLink(
            employee_id=emp.id, shortfall_date=dt.date(2026, 7, 1), pending_leave_id=None,
            surplus_dates=_json.dumps(["2026-07-05"]), surplus_minutes=_json.dumps({"2026-07-05": 999}),
        ))
        db.commit()
        assert engine.leave_debt_allocated_minutes(db, lv.id) == 320


# ---- Overtime-for-Missed-Hours match window (requirement 9) -----------------
class TestCompensationWindow:
    def test_within_21_days_is_ok(self):
        assert engine.compensation_window_ok(dt.date(2026, 6, 1), dt.date(2026, 6, 20)) is True
        assert engine.compensation_window_ok(dt.date(2026, 6, 1), dt.date(2026, 5, 15)) is True

    def test_same_calendar_month_is_ok_even_if_far_apart_in_days(self):
        # 30 days apart but both in March -> still allowed via the
        # same-calendar-month half of the OR
        assert engine.compensation_window_ok(dt.date(2026, 3, 1), dt.date(2026, 3, 30)) is True

    def test_outside_both_conditions_fails(self):
        assert engine.compensation_window_ok(dt.date(2026, 1, 1), dt.date(2026, 3, 1)) is False

    def test_exact_21_day_boundary_is_inclusive(self):
        # cross-month pair so the same-calendar-month half of the OR can't
        # rescue a boundary case and mask what's actually being tested
        assert engine.compensation_window_ok(dt.date(2026, 1, 25), dt.date(2026, 2, 15)) is True   # 21 days
        assert engine.compensation_window_ok(dt.date(2026, 1, 25), dt.date(2026, 2, 16)) is False  # 22 days
