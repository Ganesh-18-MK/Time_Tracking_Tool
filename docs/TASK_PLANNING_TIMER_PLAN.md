# Plan for the Day + Start/Pause/Resume/Stop — Implementation Plan

**Status:** Built 2026-08-21, live (no feature flag — see why below),
`py_compile`-clean and render-harness-checked across every relevant
Today-page state. **Built to a narrower, simpler design than Section 3
below describes** — Ganesh's actual build request described each
Start-to-Pause (or Start-to-Stop) segment becoming its OWN ordinary
TaskEntry row, not one row with summed-up "active minutes" and a
first-start/last-stop outer span (Section 3.1/3.2's JSON-segments idea).
That turned out to be the better call anyway: it means every segment is,
to the rest of the app, indistinguishable from a plain hand-typed row —
so **Section 2's "why this is the hard part" concern never materialized**,
and neither `app/engine.py` nor `app/validation.py` needed a single
change. A task paused over a meeting and resumed after just shows as two
normal rows in the log, each with its own real clock time, exactly like
today's Auto time capture already produces one row per Stop. This is also
why the build shipped without a feature flag, unlike Leave Management
V2 — CLAUDE.md's "run pytest + verify_strikes" hard rule only triggers on
`engine.py`/`validation.py`/`legacy/import_legacy.py` changes, none of
which happened here; the new `PlannedTask` table and the nullable
`ActiveTaskTimer.planned_task_id` column are purely additive, and every
Start/Pause/Resume/Stop action runs through the exact same
`validate_entry()` path a manually-typed row already does. A real
`pytest tests/ -q` run (particularly the new `tests/test_task_planning.py`)
before deploy is still good practice — this sandbox has neither
sqlalchemy nor pytest — but it is not gating this feature behind a flag
the way it gated Leave V2.

Scope actually built: "Plan for the Day" add form + "Today's Plan" list
with Start/Pause/Resume/Stop and Details-only inline edit (Section 3.1,
narrowed), overtime-colored task log rows once the day's target is
already reached (not in the original plan at all — a later ask), and a
punch-out reminder popup once Submit Day locks the day while a punch
session is still open (also not in the original plan). **Not built**:
admin/lead "plan a task for someone" (3.4), `shift_start_minute` /
auto-lock the previous day (3.5/3.6), in-app/real reminders (3.7), Plan
for the Week (3.8), and Submit Day carry-forward for still-open rows
(3.9) — none of these were part of the actual request; Sections 3.4-3.9
below are left as-written for future reference if any of them gets asked
for later, but should be re-scoped against this same "does it actually
need a new engine/validation concept" question before being built, since
the segment-per-TaskEntry answer above may simplify some of them too.

Originally written 2026-08-20 from a conversation relaying input from Ms.
Kennedy (via Sruthi) to Ganesh, before the actual build request came in
on 2026-08-21.

**Short answer to "are all these possible":** Yes, all of it is
buildable with what's already in this codebase — no new external
service is required for anything except real push/email/SMS reminders
(see the Reminders section). But this is the **biggest single change**
to the core time-logging engine this project has made. It touches
`TaskEntry`, `validate_entry()`, and the day-total math that
`verify_strikes`'s 168/168 acceptance test depends on — not a bolt-on
feature like Leave Management, which mostly added new tables next to
existing ones. Worth reading the "Why this is the hard part" section
below before starting.

---

## 1. What's being asked, in plain terms

1. An employee (or their admin/team lead) can add a Project/Task to
   today's (or a future day's) log **before working on it** — just
   picking what they plan to do, no times yet.
2. When they're ready to actually work on one of those planned rows,
   they get **Start / Pause / Resume / Stop** instead of just Start/Stop
   — so a call or meeting interrupting the work doesn't force them to
   either lose the row or leave the clock running while they're away.
3. Reminder to submit finished (Stopped) tasks.
4. If not submitted within a set time next morning, the previous day
   auto-locks (this needs to know each employee's actual shift start
   time, since there are multiple shifts — which the app doesn't
   currently capture).
5. A "Plan for the Week" idea — explicitly not defined yet, parked for
   later, not part of this plan.

---

## 2. Why this is the hard part (read this first)

Two things in the existing engine assume a task entry is a single,
uninterrupted block of time:

- **Duration** is just `end_minute - start_minute` (`TaskEntry.
  duration_minutes`, `app/models.py`). Once pausing exists, the wall-
  clock span (first Start to last Stop) and the actual worked time are
  two different numbers — a task paused over lunch could span 5 hours
  clock-time while only 90 minutes of it was worked.
- **Overlap blocking** (`app/validation.py validate_entry()`) currently
  checks whether two rows' `[start, end]` windows overlap at all. But
  the whole point of allowing Pause is that an employee might start a
  *different* planned task during the pause — which would, correctly,
  fall inside the paused row's outer time window. The overlap check has
  to become aware of pause gaps, not just outer start/end.
- **The 4-hour cap per row** (`max_row_minutes` in Config) is also
  currently `end - start`. That needs to switch to checking *actual
  worked* minutes — otherwise a task legitimately paused across a long
  meeting would get wrongly capped even though very little real work
  happened during that span.

None of this is a blocker — it's solvable, cleanly, and mapped out
below — but it means this isn't "add a Pause button," it's "teach the
engine the difference between clock-time and worked-time." Every one of
these changes needs to leave every **historical** row (imported and
already-logged) reading exactly as it does today — see Section 6.

---

## 3. Proposed design

### 3.1 A new "planned row" concept

Today, a row only exists in two states: running (`ActiveTaskTimer`, one
at a time, in-memory-ish) or finished (`TaskEntry`, permanent). There's
no "planned but not started yet" state at all, and `ActiveTaskTimer`
doesn't support being paused and resumed, or having several sit
un-started at once.

Proposed: extend `ActiveTaskTimer` into a row that can hold one of four
states — `planned` → `running` → `paused` → (Stop) → becomes a real
`TaskEntry`, same conversion `_finish_task_timer()` already does today.
Any number of rows can sit in `planned` or `paused` state at once; still
**only one can be `running` at a time** (same single-active-timer rule
already enforced today, `UniqueConstraint("employee_id")` — this design
keeps that constraint, it just now also allows several *paused* rows to
coexist, which is new).

Each Start/Resume opens a segment; each Pause/Stop closes it. Segments
are stored as a small JSON list on the row itself (same pattern already
used for `CompensationLink.surplus_dates` — "JSON list of ISO dates" —
rather than a whole new child table, since segments are only ever read
and summed together, never queried individually). First segment's start
= the row's overall start time; last segment's end (once Stopped) = the
overall end time — **this is exactly what lets the existing Start/Stop
times keep meaning what they already mean**, per the question raised in
the original message. Sum of segment durations = actual worked minutes.

### 3.2 `TaskEntry` gets one new field, not a rewrite

Add `TaskEntry.active_minutes` (nullable int). When it's set (a row that
went through Pause/Resume), that's the real worked duration. When it's
NULL (every row created the old way — typed by hand, or a plain
Start→Stop timer with no pauses, and every historical/imported row),
`duration_minutes` computes exactly as it does today
(`end_minute - start_minute`). This is the change that keeps blast
radius small: **`duration_minutes` stays a property with the same name
that every other part of the app already calls** — day totals, strike
computation, exports, reports — none of those call sites need to
change, because the property itself is what's smarter now, not its
callers.

### 3.3 Overlap + 4-hour cap

- Overlap check: compare segment-by-segment instead of outer-span vs.
  outer-span, for any row that has segments. A row with no segments
  (the common case, no pausing) behaves exactly as today.
- 4-hour cap: compare against `active_minutes` when present, else
  `end - start` as today.
- `suggest_non_overlapping_start()` (the "here's the next free slot"
  helper used by the sticky Add Row form) needs the same segment-aware
  update, since it deliberately duplicates the overlap logic (its own
  docstring already flags "must be kept in sync" with `validate_entry`).

### 3.4 Admin/team lead can pre-create a planned row for an employee

Yes — directly answering the question asked. Same `planned` row from
3.1, just created from an admin/lead screen instead of the employee's
own Today page: pick employee, project, task, date, optional note.
Reuses the exact same scoping the Overtime Requests screen already uses
(`app/auth.py`'s `led_by()` — a team lead only sees/acts on people whose
**Reports To** is them specifically), so a lead can only plan tasks for
their own people, not the whole company. It then sits in that
employee's log as a `planned` row until *they* press Start on it — same
row, same table, just a different `created_by`. Not a second feature —
one feature, two entry points, same pattern already used for
Suggestions (employee-created vs. lead-created, told apart the same
way).

### 3.5 Standard work hours (shift start time)

New field: `Employee.shift_start_minute` (nullable int, minutes since
midnight, same `BUSINESS_TZ` convention as every other clock-time value
in this app — see `app/util.py`). Nullable and admin-set via Roster
(same as `daily_target_minutes`/`work_days` today — not self-service,
since it's tied to attendance/payroll like those two). NULL means "not
set yet" — anyone without it set is simply excluded from the auto-lock
check (3.6) until an admin fills it in, so this can roll out gradually
without locking anyone out by surprise.

### 3.6 Auto-lock the previous day

Computed on read, not a background job — same philosophy already used
for `leave_balance()`'s calendar-year reset ("nothing is stored or
decremented... no year-end job to run"). Whenever a relevant page loads
(Today, My Month, or an admin viewing that person), check: is there a
completed-but-unsubmitted day from before today, and has current time
passed that employee's `shift_start_minute + 60 minutes` for *today*?
If so, lock it (create/update `DaySubmission` with `locked=True`,
computed `total_minutes`, and a flag marking it as auto-locked rather
than employee-submitted, for audit clarity on Person Detail/Reports).

**Trade-off worth knowing:** this only fires when something touches
that data — if an employee doesn't open the app at all the next
morning, the lock won't apply until the next time they (or an admin)
do. For ~45 people who open the app daily to log time, that's a minor
gap in practice. A true background trigger (Cloud Scheduler hitting an
internal endpoint on a timer — the standard pattern on Cloud Run, which
has no built-in cron) is the upgrade path if that gap ever matters, but
it's a separate, later piece of infrastructure work, not needed for a
first version.

### 3.7 Reminders

Two different things hiding under one word:

- **In-app reminder** (buildable now, no new infra): a banner on Today/
  My Month — "You have N completed tasks from yesterday not submitted —
  submit within Xh Ym or they'll auto-lock." Computed live on page load,
  same style as the existing overtime alert on the Today timer widget.
- **Real notifications** (email/Slack/SMS/push) — this needs an actual
  delivery channel and, almost certainly, the same background-trigger
  infra as 3.6's upgrade path (something has to run even when nobody's
  looking at the app). This is a bigger, separate decision: which
  channel, and are we ready to add that infrastructure. Recommend
  shipping the in-app banner first and treating real notifications as
  its own follow-up once it's clear the banner alone isn't enough.

### 3.8 Plan for the Week

Explicitly not designed here — flagged by the original message as
needing more definition first. Noted for later, not part of this build.

### 3.9 Submit Day and unfinished tasks (2026-08-20)

The problem: today, `submit_day()` only sums *finished* `TaskEntry`
rows (`engine.day_total_minutes()`) — it doesn't even look at whether a
timer is currently running. With Plan/Pause/Resume, an employee could
easily have a `running` or `paused` row sitting open at Submit Day time,
and that time would otherwise just be lost or force the employee to
scramble to press Stop first.

**Decided approach, per today's conversation:**

- At Submit Day, if any row is still `running` or `paused` (has real
  time on it, just never explicitly Stopped), the employee is shown a
  short confirmation for each one — **not** auto-carried-forward
  silently. Two choices per row: "Mark done" (finalizes it into today's
  `TaskEntry` as-is, no copy made) or "Carry forward to tomorrow"
  (finalizes today's portion into a `TaskEntry` *and* creates a fresh
  `planned` row — same project/task/details, no times — dated the next
  working day). This avoids wrongly assuming every unfinished-looking
  row was actually left open by mistake.
- Either way, today's captured time counts toward today's total the
  moment Submit Day completes — "carry forward" only affects what
  happens tomorrow, never what counts today.
- A `planned` row that was never started at all (zero time, nothing to
  finalize) isn't part of this confirmation step — it just has its
  `date` shifted to the next working day automatically, since there's
  nothing to "complete," and it can't stay dated a day that's about to
  be locked.
- "Next working day" reuses `engine.is_working_day()` (already
  location/holiday-aware), so a carried-forward task correctly skips
  weekends and company holidays, landing on whoever's actual next
  working day it is.
- Same day-by-day design as the rest of this plan: each day's
  `TaskEntry` reflects only that day's own worked time, never a
  multi-day blob — a task worked on for a week just means a chain of
  same-project/task rows, one per day, each showing that day's real
  portion.

---

## 4. Files that will change, and why

**`app/models.py`**
- Extend `ActiveTaskTimer` (or a new model reusing its shape) with a
  `status` field (planned/running/paused), a `segments` JSON column,
  and `created_by_employee_id` (self vs. admin/lead-created).
- `TaskEntry.active_minutes` — new nullable column (Section 3.2).
- `Employee.shift_start_minute` — new nullable column (Section 3.5).
- `DaySubmission` — a flag distinguishing auto-locked from
  employee-submitted, for clear audit history.

**`app/util.py`**
- Startup backfill pattern (as every new column in this project needs)
  — though both new columns here are meaningfully NULL-by-default
  ("not set / no pauses happened"), so likely no real backfill data is
  needed, just confirmation that every read site treats NULL correctly.

**`app/validation.py`**
- Segment-aware overlap check (3.3).
- 4-hour cap checks `active_minutes` when present (3.3).
- `suggest_non_overlapping_start()` gets the same segment-aware update.
- **Any change here requires running pytest AND `verify_strikes`
  before it's done — CLAUDE.md hard rule, not optional.**

**`app/engine.py`**
- Auto-lock check (3.6), reusing `now_local()`/`today_local()` and the
  existing `DaySubmission` shape.
- In-app reminder computation (3.7) — how many completed-not-submitted
  tasks, how much time remains before auto-lock.

**`app/routes/employee.py` + Today template**
- Plan/Start/Pause/Resume/Stop UI on planned rows.
- The reminder banner.
- `submit_day()` gets the confirmation step for unfinished rows and the
  carry-forward/mark-done logic (3.9).

**`app/routes/admin.py` + a new admin template**
- "Plan a task for someone" screen, scoped via `led_by()` (3.4).

**`tests/`**
- Segment math (sum of segments = active_minutes, first-start/last-stop
  preserved).
- Overlap: a second planned task starting during another's pause is
  allowed; two tasks with truly overlapping active segments are still
  blocked.
- 4-hour cap uses active minutes, not wall-clock span.
- Every existing test in `test_engine.py`/`test_validation.py` must
  stay green unchanged — they exercise the NULL-`active_minutes` path,
  which must behave identically to today.
- Auto-lock: exactly at the boundary, before it, after it, employee
  with no `shift_start_minute` set (excluded, not crashed).

**Feature flag**
- Same pattern as Ticketing/Holiday Management — ship dark, verify,
  then turn on. Given this touches the core compliance engine, this one
  matters more than most: it should be fully built, tested, and
  `verify_strikes`-clean before it's live for anyone.

---

## 5. Still open — confirm before/while building

- Does Pause on task A let the employee immediately Start a *different*
  planned task B, or does Pause just stop the clock with nothing else
  allowed to run until Resume? (Design above assumes yes, since that
  matches "interrupted by a call or another task" — but worth
  confirming, since it changes how much of the single-active-timer rule
  stays intact.)
- When an admin/lead pre-plans a task for someone, does the employee
  get any notice it's there, or do they just see it sitting in their
  log next time they open Today? (Suggests reusing the in-app reminder/
  banner mechanism for this too, rather than inventing a second one.)
- Auto-lock exact wording: is it "1 hour after shift start" for
  everyone, or does that number itself need to be a Config value
  (admin-tunable, matching CLAUDE.md's "never hardcode thresholds"
  rule) rather than a fixed 60 minutes?
- Does `shift_start_minute` also want a **shift end** captured at the
  same time, or is total daily target (`daily_target_minutes`) still
  how "end of day" gets figured, with only the start being new?

---

## 6. Safety rule for the whole build

Every one of these changes must be **invisible to every existing row**.
An old `TaskEntry` (typed by hand, or auto-captured with no pauses) has
`active_minutes = NULL` and behaves exactly as it does in production
today. `verify_strikes` must still print **168/168** after every step
that touches `app/engine.py` or `app/validation.py` — that check exists
specifically to catch exactly this kind of change silently altering
historical compliance numbers.

---

## 7. Suggested build order

1. **Data model** (3.5's `shift_start_minute`, `TaskEntry.
   active_minutes`, the extended planned-row model) — feature flag off,
   nothing user-visible.
2. **Segment-aware validation** (3.3) — the highest-risk piece,
   built and tested in isolation, `verify_strikes` re-run immediately.
3. **Employee-side Plan/Start/Pause/Resume/Stop UI** on Today.
4. **Submit Day carry-forward** (3.9) — depends directly on step 3
   existing first, since it's about what happens to those same rows.
5. **Admin/lead "plan a task for someone" screen** (3.4).
6. **Auto-lock + in-app reminder banner** (3.6, 3.7).
7. **Documentation** — new Use-Cases section for the employee manual
   (coordinate with Sruthi's review so it's written against the same
   version she's updating, not a separate draft that has to be merged
   later).
8. **Real notifications** (3.7) — only if the in-app banner alone turns
   out not to be enough; separate infrastructure decision.

This is realistically a multi-session build, not a single day —
flagging that now rather than after starting, given how much of
Section 3.3 alone needs care.
