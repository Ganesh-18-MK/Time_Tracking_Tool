# Leave Management Rebuild — Implementation Plan

**Status:** Plan only, nothing built yet. Written 2026-08-20 from
`Leave Management Requirements.pdf` (manager) plus decisions Ganesh made
in chat the same day. Ready to start building.

**Source of truth for behavior:** the requirements PDF, narrowed by the
decisions below. Where the PDF was ambiguous, this plan states the call
made and why, so it's visible and reversible rather than a silent guess.

---

## 1. What's changing, in plain terms

Today, Leave has 4 types (Casual / Sick / Vacation / Other), and each
person's yearly allowance is just a number an admin types in — nothing
earns it over time, and nothing stops an admin approving past it.

The new system has 5 types (Planned / Unplanned / Unpaid / Bereavement /
Special Paid), and Planned Time is **earned automatically** over time
based on how long someone's worked here, with a waiting period before it
starts. Requesting leave also gets a proper Half Day / Full Day / Custom
picker instead of just typing a number.

---

## 2. Decisions made (2026-08-20)

- **Half Day / Full Day / Custom**, chosen after picking the leave type.
  Half Day and Full Day are derived from *that employee's own daily
  target* (target ÷ 2, and target), not a hardcoded 4/8 — this matches
  how the rest of the app already treats `daily_target_minutes` as the
  real per-person number rather than assuming everyone is 8h/day. For
  the ~45 people currently on a standard 8-hour day, this produces
  exactly 4h/8h either way, so nothing looks different day-to-day; it
  just doesn't quietly break for anyone on a different schedule.
  **Flagging this since it's a small deviation from "4 hours / 8 hours"
  as literally written — easy to hardcode instead if that's actually
  wanted.**
- **Accrual is a flat monthly amount, not "per hour worked."** The PDF
  used both phrasings; today's message settled it — 9 days/year for 0-2
  years' experience = 6 hours/month, a flat monthly credit. Same
  divide-by-12 approach for the other two bands (they're just not typed
  out again each time, per today's message).
- **Accrual math done in minutes, matching CLAUDE.md's hard rule (no
  floats, everything in integer minutes).** Worked out in minutes
  instead of hours, all three bands land on exact whole numbers — no
  rounding hacks needed:
  | Experience | Days/year | Minutes/month (÷12) | In hours |
  |---|---|---|---|
  | 0–2 years | 9 | 360 | 6h — confirmed today |
  | 2–5 years | 11 | 440 | 7h 20m |
  | 5+ years | 13 | 520 | 8h 40m |
  (Minutes/month = days/year × daily_target_minutes ÷ 12, using each
  employee's own daily target — so someone on a non-standard schedule
  still gets a proportionally correct number.)
- **Tenure is computed automatically from Joining Date to today** — no
  admin picks a band by hand; the system works out years-of-service
  itself and looks up the right rate.
- **Probation blocks Planned Time only.** Other leave types (Unplanned,
  Unpaid, Bereavement, Special Paid) stay available during probation —
  matches the PDF's "Unplanned Time... available immediately, no
  probation period."
- **Accrual starts after probation ends, not backdated.** No Planned
  Time is earned for the probation window itself.
- **Overtime matching (Missed Hours ↔ Overtime) is employee-requested,
  admin-approved — not admin-initiated.** (2026-08-20) An employee picks
  a Missed Hours day and an Overtime day/hours they want it matched
  against; a SuperAdmin approves or denies it, same shape as Leave and
  Overtime requests already work today (submit → queue → approve/reject
  with a reason). This is actually a simpler build than "admin matches
  unprompted" would have been — it reuses the existing request/approve
  pattern instead of inventing a new one.
- **Approving a leave request can approve fewer hours than requested,
  with a note explaining why.** (2026-08-20) Today, approving a leave
  request just flips its status — the hours are whatever was requested,
  there's no way to grant a smaller amount. The new system needs the
  admin's approve screen to show an editable "Hours approved" field
  (pre-filled with what was requested) alongside the existing note
  field, so a partial approval — e.g. employee asked for a full day,
  admin approves half — is a real, recorded thing, not just a note
  saying so while the stored hours stay wrong.
- **Employees can see Used vs. Pending hours, per leave type, not just a
  single balance number.** (2026-08-20) Today's balance table only shows
  entitlement/used/remaining, and "used" only counts already-approved
  leave. The new balance view needs a third number: hours still sitting
  in a request that hasn't been approved or denied yet, so an employee
  can tell "I have 6 hours left" apart from "I have 6 hours left, but
  2 of those are already tied up in a pending request."

## 3. Still open — confirm before/while building

These don't have to block starting tomorrow, but need an answer during
the build:

- Does "Other" (today's 4th type, free-text note) stay as a 6th type, or
  go away entirely now that Special Paid/Bereavement cover most of what
  it was used for?
- Do existing historical leave records (people's real past Casual/Sick/
  Vacation leave) get *relabeled* into the new types, or stay exactly as
  they are (frozen history) while only new requests use the new 5 types?
  **Recommended: leave history untouched, map old types into new
  buckets only for balance math** — same "never rewrite frozen fact"
  principle CLAUDE.md already uses for imported DayStatus rows.
- Does a full calendar month need to be 100% complete to earn that
  month's Planned Time, or does joining mid-month earn a partial amount
  for that first month? **Recommended: only full completed months
  count** — simpler, and avoids a second layer of proration math.
- PIP flag: who sets it (Roster? Person Detail?), and does it need its
  own start/end date, or just an on/off switch?
- Special Paid Time: does "management approval" need its own approval
  step in the system, or is a SuperAdmin granting the hours *treated as*
  the approval (i.e., no separate two-step workflow)? **Recommended:
  no separate step** — matches how compensation links already work
  today (one SuperAdmin action, reason required, done).
- Compensation for Missed Hours / Matching Hours: this looks very close
  to the existing Compensation Links feature already in the app
  (`CompensationLink` model, Dashboard shortfall list). Plan is to
  extend that feature rather than build a second, separate one. Today
  `CompensationLink` has no status field — it's created directly by an
  admin, nothing to approve. Since this is now employee-requested (see
  decision above), it needs a `status` column (requested / approved /
  rejected, same values as `LeaveRecord`/`OvertimeApproval`) plus an
  employee-facing "request a match" form — still needs a closer look at
  `app/routes/admin.py`'s current compensation routes to confirm exactly
  what else changes.

---

## 4. Files that will change, and why

**`app/models.py`**
- `LEAVE_TYPES` becomes the 5 new values. New `Employee` columns, all
  nullable/defaulted (additive-only — real production data exists now,
  no `rm tms.db`): `is_on_pip` (bool), `probation_days` (nullable int —
  NULL means "use the company default from Config"). New `LeaveRecord`
  columns: `relation` (nullable string, only used when type is
  Bereavement) and `approved_minutes_per_day` (nullable int — NULL until
  a decision is made; set at approve time, may be less than
  `minutes_per_day` for a partial approval; `review_note` is where the
  "why partial" reason goes, already exists). Stored as minutes
  internally (CLAUDE.md's integer-minutes hard rule), but the admin
  never sees "minutes" — the approve screen's field is labeled **"Hours
  approved"** and takes a plain hours number (e.g. "4" or "4.5"), same
  input convention already used for Target/day elsewhere in Roster. New
  small table for
  Special Paid Time grants (employee, hours, reason, granted_by,
  granted_at) — a ledger, since those hours are handed out one grant at
  a time, not accrued.

**`app/util.py`**
- A startup backfill (`ensure_*`, same pattern as every other new column
  in this project) for `is_on_pip`/`probation_days` so existing rows get
  a real, explicit value rather than relying on SQLite/Postgres's
  column-add default silently doing the right thing.

**`app/engine.py`**
- New accrual function: given an employee and a date, work out years of
  service from Joining Date, check probation, pick the right band from
  Config, and return minutes accrued to date (running total, not reset
  each January — this is a real change from how `leave_balance()` works
  today, which recomputes fresh and resets every calendar year by
  design). Reuses the existing `is_working_day()`/holiday-aware helpers
  for the notice-period "working days" counting.
- Notice-period check: 1 day → 2 working days' notice, 2-3 days → 7
  working days, 4+ days → 3 weeks. Runs when a Planned Time request is
  submitted.
- Balance function now returns three numbers per type instead of two:
  **used** (sum of `approved_minutes_per_day` across approved records —
  not the originally-requested amount, so a partial approval is
  reflected correctly), **pending** (sum of `minutes_per_day` across
  records still `status == requested`), and **remaining** (accrued minus
  used minus pending — so pending hours are held against the balance
  the moment they're requested, not just once approved, matching how
  the employee's own message described it: "used" and "pending" shown
  separately, not pending hiding inside "remaining" unaccounted for).

**`app/routes/employee.py` + leave template**
- Leave request form: Type of Leave dropdown (new 5 types) → then
  Half Day / Full Day / Custom appears. Custom reuses the existing
  `hours` field already in the form (no new plumbing needed there).
  Bereavement adds a Relationship dropdown when that type is picked.
  Notice-period and probation checks block submission with a clear
  message, same style as today's existing validation errors.
- New "Request a match" form (Missed Hours ↔ Overtime): employee picks
  the Missed Hours day and which Overtime day/hours to match it against,
  submits for approval — same submit-then-wait pattern as Leave/Overtime
  requests already use, so it can reuse most of that plumbing.
- Leave balance table gets a Pending column added next to Used and
  Remaining, per leave type — pulls from the new three-number balance
  function above.

**`app/routes/admin.py` + admin templates**
- Roster/Person Detail: probation-days override field, PIP toggle.
- New small screen for granting Special Paid Time (SuperAdmin picks
  employee, hours, reason).
- Leave approval screen gets a new editable "Hours approved" field
  (pre-filled with what was requested) alongside the existing note
  field — approving no longer just flips status, it also decides how
  many hours are actually granted. Reason/note should stay required on
  deny (likely already true, confirm), and is strongly encouraged
  whenever approved hours are less than requested.
- Compensation-links screen gains an approve/reject queue for
  employee-submitted match requests, alongside whatever admin-direct
  linking it already supports — enforcing the 3-week/same-calendar-month
  window as a validation check at approval time.

**`CONFIG_DEFAULTS` (`app/models.py`) + Settings screen**
- New config keys instead of hardcoded numbers, matching CLAUDE.md's
  existing rule ("read via `engine.get_config(db)`, never hardcode
  thresholds"): `probation_days_default`, and the three accrual rates
  (`planned_days_year_0_2`, `planned_days_year_2_5`,
  `planned_days_year_5_plus`), each stored as days/year — the app
  converts to minutes/month itself.

**`tests/`**
- New `test_leave_accrual.py`-style file: probation blocking, band
  selection by tenure, the minutes-per-month math above, carry-over
  across a year boundary, notice-period edge cases (exactly on the
  boundary, non-working days not counted), PIP forcing Unpaid, partial
  approval (`approved_minutes_per_day` < `minutes_per_day` correctly
  reduces "used" and doesn't touch "remaining" twice), and the
  used/pending/remaining split (a pending request holds its hours out of
  "remaining" without counting as "used" until it's actually approved).

**Feature flag**
- Ship behind an env-var flag, default **off**, same pattern already
  used twice in this project (`TICKETING_ENABLED`,
  `HOLIDAY_MANAGEMENT_ENABLED`) — this touches something that affects
  real pay, so it should be fully built and verified before it's live
  for real employees, with a one-line switch to turn it on when ready.

---

## 5. Suggested build order (phased, not all in one day)

1. **Data model + config** — new columns, new config keys, startup
   backfill, feature flag off. Nothing user-visible yet.
2. **Accrual engine** — the tenure/probation/band-lookup function, fully
   unit tested against the minutes table in Section 2, before anything
   calls it from a route.
3. **Employee leave request form** — new types, Half/Full/Custom picker,
   Bereavement relationship field, notice-period + probation validation.
4. **Admin side** — Roster/Person Detail fields, Special Paid Time grant
   screen, balance display.
5. **Compensation for Missed Hours / Matching Hours** — extend the
   existing Compensation Links feature (needs the closer look flagged
   above first).
6. **Tests, docs, flip the feature flag on.**

Realistic scope for tomorrow: steps 1-2, maybe starting 3. This is a
genuine rebuild of a core module the business runs pay-related decisions
on — worth taking in verified steps rather than rushing the whole thing
into one sitting.
