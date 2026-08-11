# PRD: MK Internal Timekeeping and Compliance App (POC)

**Prepared for:** Steve, Director of Growth
**Purpose:** Hand-off document for the internal build team (Claude Code)
**Status:** Draft v1
**Scope:** Interim internal tool. Replaces three manual spreadsheets until the third-party HR software pilot concludes. Built as a proof of concept, not production software.

---

## 1. Problem

HR/admin runs offshore time tracking across three manually maintained spreadsheets. The process works but does not scale, and the files themselves are degrading.

| Artifact | What it does today | Verified pain point |
|---|---|---|
| Task Summary (one file per person) | Every task logged to the minute: Date, Project/Employer, Task, Details, Start Time, End Time, Time taken, Total Time taken. Employee sums total hours at end of day. | One person's file (Divya, 622 task rows since March 2026) is 20 MB and decompresses to a 700 MB XML grid with 16,331 empty repeated columns per row. Files are slow to open, hard to parse programmatically, and grow without bound. |
| Leave Tracker (one workbook, one tab per person, 57 tabs) | Left block logs leaves: Leave Type, When, Hours, Note, plus compensation notes ("compensated 3-5 July"). Middle block totals days by category: Casual, Sick, Vacation. Right blocks hold monthly per-day "extra" and "short" durations entered by hand as hh:mm. | No computed running balance of over/under hours exists in the file. Extra/short entries are typed manually per day. Compensation offsets live in free-text notes, so nothing reconciles automatically. |
| Compliance sheet (one workbook, one tab per month) | Rows are employees grouped by department. One column per calendar day holding Y, N, PARTIAL, LEAVE, blank, or free text ("4 hours 30 min"). Final column: STRIKES FOR MONTH = COUNTIF(N) + COUNTIF(PARTIAL). | Every daily mark is entered by hand after cross-referencing the other two artifacts. The strike count is the only automation in the entire workflow. |

The admin cross-references all three daily. The app replaces the cross-referencing: the employee logs time once, and leave records plus compliance status derive from that single entry automatically.

## 2. What this replaces and supersedes

- Replaces all three spreadsheets as the system of record for time, leave, and compliance.
- Supersedes the Timesheet Compliance Checker (Requirements v1). That tool read and parsed the spreadsheet files from SharePoint. With the app as the system of record there are no files to parse, so the checker's format-validation and AI-adjudication logic is unnecessary. Its status model (Complete / Short / Missing / Exception) carries forward into Section 6.
- Runs until the third-party HR software pilot concludes. Design for export, not for permanence.

## 3. Users and roles

Two roles for the POC.

| Role | Who | Can do |
|---|---|---|
| Employee | ~45 active offshore staff (July 2026 compliance sheet count) | Log tasks for their own day, submit their day, view their own hours, variance balance, leave history, and strike count. Nothing else. |
| Admin | HR/admin (currently Norine's function), Steve, Mary | Everything: roster, dropdown lists, leave entry and approval, status overrides, strike review, thresholds, exports. |

Team leads exist in the data (department groupings with TEAM LEAD designations). A read-only team view for leads is a fast follow, not POC scope.

## 4. Module A: Daily time log (replaces Task Summary)

### Entry

The employee's main screen is today's log. Each row:

| Field | Rules |
|---|---|
| Date | Defaults to today. Employees cannot log more than 1 working day in the past (configurable, see Section 10). |
| Project/Employer | Dropdown, admin-managed list. The current files already use dropdowns fed by a hidden List sheet (client names such as "AB2 Consulting, Inc.", "9Logic Technologies INC"). Free text not allowed. |
| Task | Dropdown, admin-managed list (current values include "Check emails", "Internal Meetings", "PWD initiation", "Drafting letters", "Send email"). Free text not allowed. |
| Details | Free text, required, minimum 5 characters. |
| Start Time | Time picker. |
| End Time | Time picker. Must be after Start Time. |
| Duration | Computed from Start and End. Never typed. This kills the current cross-check problem where "Time taken" and the Start-to-End math disagree. |

### Validation on entry (the strict guidelines)

- No overlapping rows within a day.
- Gaps between rows over 15 minutes get a visual flag, not a block. Breaks are legitimate, so logged break time inside the gap is netted out first — only the leftover, still-unexplained minutes get flagged (added 2026-08-11, after the flag fired on a gap almost entirely covered by a logged break).
- A row cannot be logged over a time span the employee already logged as a break — blocked outright, with a message pointing at the break window, so work time and break time can't overlap in the log (added 2026-08-11).
- No row may span past midnight; split across days.
- Maximum single-row duration 4 hours (configurable). Longer rows force the employee to break work down, which is the point of minute-level logging.

### Day submission

- The employee clicks Submit Day when done. Daily total is computed, never typed.
- Submission locks the day. Edits after submission require an admin unlock, and every unlock is logged (who, when, what changed).
- Unsubmitted days roll into compliance as missing (Section 6).

## 5. Module B: Leave and hours ledger (replaces Leave Tracker)

### Leave records

Admin-entered in the POC (see open question 5 on employee self-service requests).

| Field | Rules |
|---|---|
| Employee | From roster. |
| Date(s) | Single day or inclusive range. |
| Type | Casual, Sick, Vacation, plus Other with a note (current data contains types like "Traveling", "Holiday", and reasons like "power outage" that do not fit the three categories). |
| Hours | Full day (defaults to the person's daily target) or partial hours. |
| Note | Free text. |

Category totals per person (the Days Taken block) are computed, not typed.

### Hours variance (the extra/short tracking)

Computed daily per person, no manual entry:

```
variance = submitted hours for the day - daily target for the day
```

- Daily target comes from the roster (default 8.0 hours full time; the sheets show part-time staff such as "4 Hours 19 min" days, so target is per person).
- Approved leave reduces that day's target by the leave hours. A full-day leave means target 0 and no variance.
- The ledger shows each day's variance and a running net balance. This is the running total the current sheet implies but never actually computes.

### Compensation (make-up hours)

The current tracker handles this in free text ("compensated 3-5 July", "compensate"). The app makes it a first-class link: an admin marks a shortfall as compensated by pointing it at one or more surplus days. Both sides display the link. The running balance already nets out mathematically; the link exists so a human can see which short day was made up when, which is what the disciplinary conversation needs.

## 6. Module C: Compliance and strikes (replaces the compliance sheet)

### Daily status, auto-derived

Computed nightly and on demand for every active person for every working day. Never typed.

| Status | Condition | Maps to current sheet |
|---|---|---|
| Complete | Day submitted, hours >= target minus tolerance | Y |
| Partial | Day submitted, hours below target minus tolerance | PARTIAL |
| Missing | Working day, no submission, no approved leave | N |
| Leave | Approved leave covers the day | LEAVE |
| Holiday / Weekend | Non-working day | blank |

Default tolerance 1.0 hour (carried from the Compliance Checker v1 requirements, open question 2). Target and actual always display as numbers so the admin can judge borderline cases rather than trusting the threshold.

### Strikes

Verified rule from the current sheet's formula: strikes per month = count of Missing days + count of Partial days, each worth one strike. The July 2026 sheet shows real counts from 0 to 10.

- Strike counts compute automatically from daily statuses. The admin's manual daily marking disappears entirely.
- Admin can override any day's status with a mandatory reason. Overrides are logged and visibly flagged, because an override changes someone's strike count and potentially their pay.
- Compensated shortfalls: whether a compensated Partial day still counts as a strike is open question 3. Default for the POC: a fully compensated day is recomputed as Complete and the original status is retained in the audit log.

### Violations

The current data counts strikes but defines no threshold for action. The user rule is: X strikes = violation, repeated violations = pay reduction. Neither X nor the pay consequence appears anywhere in the files.

- POC ships with a configurable monthly strike threshold (placeholder default: 5) that flags the person on the admin dashboard as In Violation.
- The app flags. It does not calculate or apply pay changes. Pay action stays a human decision outside the tool (open question 4).

## 7. Screens

### Employee

1. Today: task log entry, running total for the day, Submit Day.
2. My Month: calendar of own statuses, hours vs target per day, running variance balance, leave taken by category, current strike count. Employees seeing their own strike count in real time is deliberate; today they find out after the fact.

### Admin

1. Compliance dashboard: the current monthly sheet, rebuilt live. Rows = employees grouped by department, columns = days, cells = auto-derived status, strike total per row. Filters: department, exceptions only, month. This view existing at all is the core payoff.
2. Person detail: one employee's full log, ledger, leave history, overrides.
3. Roster: add/deactivate people, set department, designation, daily target, work schedule, start date. Deactivated people keep history (the current tracker's GONE section) but drop out of compliance runs.
4. Lists: manage Project/Employer and Task dropdown values. Deactivating a value hides it from new entries without breaking old rows.
5. Leave entry and compensation linking.
6. Config: tolerance, strike threshold, max row duration, back-dating window.
7. Export: any view to CSV/XLSX. This is the bridge to the third-party software and the safety net if the POC dies.

## 8. Data model

Entities, minimum fields. The build team owns the schema details.

| Entity | Key fields |
|---|---|
| Employee | id, name, department, designation, daily_target_hours, schedule (work days, standard hours, timezone), start_date, active |
| TaskEntry | id, employee_id, date, project_id, task_type_id, details, start_time, end_time, duration (computed), created_at |
| DaySubmission | employee_id, date, total_hours (computed), submitted_at, locked, unlock_log |
| LeaveRecord | id, employee_id, date_range, type, hours, note, entered_by |
| DayStatus | employee_id, date, status, actual_hours, target_hours, variance, override (reason, by, at, original_status) |
| CompensationLink | shortfall_day, surplus_day(s), linked_by, note |
| Project, TaskType | id, name, active |
| Config | tolerance, strike_threshold, max_row_hours, backdate_days |
| AuditLog | actor, action, entity, before, after, timestamp |

Statuses and strikes are computed views over TaskEntry, DaySubmission, and LeaveRecord, materialized into DayStatus for speed and override support. Nothing about compliance is ever hand-entered except overrides.

## 9. Build guidance for the POC

Suggestions, not mandates. The build team may substitute equivalents.

- Single web app, one codebase, role-gated views. Python (FastAPI or Django) or Node, server-rendered or light React. Nothing fancy.
- Postgres. Not SQLite, because ~45 concurrent users submit at end of day, and not files, because files are the disease being cured.
- Auth through the existing Microsoft tenant (Entra ID) so there are no new passwords and access is revocable centrally. Same model as the compliance checker plan.
- Host on Azure App Service inside the current tenant, matching the email system and checker architecture already approved.
- Seed script that imports the three existing files: roster and departments from the compliance sheet, leave history from the tracker, and task history from the Task Summary files where parseable. Historical strike counts from March through July 2026 should reproduce the sheet's numbers; that reproduction is the acceptance test for the status engine.

## 10. Open questions

Defaults let the build start. Every one of these is a config value or a one-line change.

| # | Question | POC default |
|---|---|---|
| 1 | How many strikes in a month = violation, and what escalation follows? Not defined anywhere in the current data. | Threshold 5, dashboard flag only |
| 2 | Tolerance below target before a day counts as Partial? | 1.0 hour |
| 3 | Does a compensated shortfall erase the strike? | Yes, with audit trail |
| 4 | Should the app model pay reductions at all? | No, human decision |
| 5 | Do employees request leave in-app with admin approval, or does admin enter everything? | Admin enters (matches today) |
| 6 | Leave quotas per category per year? None appear in the tracker. | No quotas enforced, totals displayed |
| 7 | How far back can an employee log or edit an unsubmitted day? | 1 working day |
| 8 | Are weekends universally non-working? Compliance sheet leaves them blank, but Prem's history shows weekend and pre-shift logging. | Mon-Fri working, per-person schedule override available |
| 9 | Company holiday calendar source and regional scoping? | Admin-maintained table, single region |

## 11. Out of scope for the POC

- Anomaly detection (pre-shift work, meta-work ratios, unauthorized projects, the Prem-style analysis). Phase 2, same as the checker plan. The structured data this app produces makes that analysis trivial later, which is a reason to build this even with the third-party pilot running.
- Payroll integration or pay calculations.
- Mobile apps. Responsive web is enough.
- Notifications (end-of-day reminders, strike alerts). Fast follow.
- Team-lead role and views.
- Migration of full task history from every legacy file. Roster, leave, and strike history migrate; task-level history migrates best-effort.

## 12. Success criteria

1. An employee logs and submits a full day in under the time the spreadsheet takes, with zero manual arithmetic.
2. The admin's daily cross-referencing task is eliminated: the compliance dashboard shows every person's status for yesterday with no data entry.
3. Recomputed March-July 2026 strike counts match the existing sheet's STRIKES FOR MONTH column for migrated data.
4. Any admin view exports cleanly to XLSX for the third-party pilot handoff.
