# Jira Backlog — MK Timekeeping & Compliance App

This is ready to copy into Jira by hand. Structure: one **Epic**, with
**Tasks** underneath it (one per feature area), and **Tickets** under each
Task (create these as Sub-tasks of the Task, or as Stories linked to the
Epic if your Jira setup doesn't do sub-tasks — either works).

All names and descriptions are written in plain, everyday language on
purpose, so anyone on the team — not just developers — can read a ticket
and understand what it's about.

**Status key:** most of this is already built and being used every day, so
most tickets below are marked **Done**. A few are marked **Done (Off for
now)** — built and tested, but switched off until we're ready to turn them
on. One section at the end is **Not Started** — things we've talked about
but haven't built yet.

---

## EPIC

**Epic name:** Timekeeping & Compliance App

**Epic description:**
Replace the three manual spreadsheets we used to track time, leave, and
compliance with one web app. Employees log their time once a day, and
everything else — leave balance, extra/short hours, compliance status —
is worked out automatically instead of by hand. Built as a quick,
low-cost stand-in system, not a forever platform, so it's easy to hand
data off if we ever move to different software later.

**Status:** In Progress (most features live and in daily use; a couple of
small things still to come — see the last section)

---

## Task 1: Signing In and Setting a Password

**Description:** Lets each person create their own account and sign in
safely, without anyone else being able to see or guess their password.

**Status:** Done

Tickets:
- **Let people sign up with their work email** — A new person can set their own password the first time, as long as an admin has already added them to the list.
  *Status: Done*
- **Separate sign-in pages for staff and managers** — Staff and managers each get their own sign-in page, so it's harder to mix them up.
  *Status: Done*
- **Lock out repeated wrong passwords** — If someone types the wrong password too many times, that account is temporarily locked so nobody can keep guessing.
  *Status: Done*
- **Stop one computer from spamming the sign-in page** — If a lot of sign-in attempts come from the same place very quickly, they get slowed down automatically, even if each attempt uses a different email.
  *Status: Done*

---

## Task 2: Daily Time Log ("Today" Page)

**Description:** The main page where someone records what they worked on
today — what project, what task, and how long.

**Status:** Done

Tickets:
- **Add a row for each task worked on** — Pick a project and task, add a short note, and enter the start and end time.
- **Warn about overlapping times** — If two rows overlap, the app stops the save and explains why.
- **Flag gaps in the day** — If there's an unexplained gap of more than 15 minutes, it's pointed out (but doesn't block saving).
- **Remember what was typed if something goes wrong** — If a save fails, the form keeps what was already typed instead of clearing it.
- **Start and stop a timer automatically** — Instead of typing start/end times by hand, start a timer when work begins and stop it when it's done; the times fill in by themselves.
- **Lock the day once it's submitted** — Once a day is submitted, it's locked from further changes unless an admin unlocks it.

---

## Task 3: Breaks and the Punch Clock

**Description:** Lets someone start and end their workday, and log breaks,
with the app automatically working out extra time owed.

**Status:** Done

Tickets:
- **Punch in and punch out for the day** — One button to mark "I've started work" and another for "I've finished," with a running clock in between.
- **Log a break with one click** — Start Break / End Break buttons, with lunch/dinner allowed once a day.
- **Add extra work time automatically when breaks run long** — If someone takes more break time than allowed, that extra time is automatically added to their target for the day.
- **Pause the work timer during a break** — The running timer stops counting while someone is on a break, and starts again after.
- **Show a popup when a break ends, that goes away by itself** — A short message confirms the break ended, and disappears on its own after a few seconds instead of staying on screen.
  *Status: Done (fixed 2026-08-19)*

---

## Task 4: My Month (Personal Time History)

**Description:** A calendar-style view where someone can look back over
their own month and see their status for each day.

**Status:** Done

Tickets:
- **Show a full month at a glance** — Every day of the month with its status (worked, leave, holiday, missing, etc.).
- **Click a day to see exactly what was logged** — Expand any day to see the individual rows that made up that day's total.
- **Show running hours balance** — A live total of hours over or under target for the month so far.
- **Show strike count for the month** — How many "missing" or "partial" days have counted against this person this month.

---

## Task 5: Leave Requests

**Description:** Lets an employee ask for time off themselves, instead of
someone in HR typing it in for them.

**Status:** Done

Tickets:
- **Submit a leave request** — Pick the dates, the type of leave (casual, sick, vacation, other), and an optional note.
- **Show remaining leave balance** — Each person can see how much of each leave type they have left this year.
- **Let someone withdraw their own request** — Cancel a request that hasn't been decided on yet.
- **Send requests to a manager for approval** — Requests go to the right manager to approve or decline, with a reason if declined.

---

## Task 6: Overtime Requests

**Description:** Lets someone ask permission to work extra hours *before*
doing it, so it's on record as approved rather than just happening.

**Status:** Done

Tickets:
- **Submit an overtime request in advance** — Pick the date and expected extra hours, with a reason.
- **Send requests to a manager for approval** — A manager approves or declines each request.
- **Let someone withdraw their own request** — Cancel a request that hasn't been decided on yet.
- **Show approved overtime in the reports** — Once approved, the extra hours show up correctly in the attendance reports instead of just looking like unexplained overtime.

---

## Task 7: Company Holidays

**Description:** A shared list of company holidays that everyone sees, so
nobody is marked as missing work on a day off.

**Status:** Done

Tickets:
- **Maintain one shared holiday list for everyone** — One list of holidays that applies to the whole company, not different lists per office. (We tried splitting this by country in mid-August and switched back within days — the team wanted one shared list.)
- **Let an admin add holidays in bulk from a spreadsheet** — Upload a simple spreadsheet of holiday names and dates instead of adding them one at a time.
- **Let employees see the holiday list** — A simple page anyone can check to see upcoming holidays.
- **Turn the whole feature on or off in one place** — A single switch that can hide or show all holiday-related pages and buttons if we ever need to pull it back temporarily.

---

## Task 8: Admin Dashboard

**Description:** The main screen managers and admins use to see how the
whole team is doing, without digging through spreadsheets.

**Status:** Done

Tickets:
- **Show everyone's status for a chosen day** — Green/yellow/red style status for the whole team at a glance, computed automatically.
- **Only show a manager their own department** — A department-level manager sees their own team; only top-level admins see everyone.
- **List people who are short on hours** — A quick list of who's currently under target, to make follow-up easy.

---

## Task 9: Employee List (Roster)

**Description:** The master list of everyone in the company — used to add
new people, update details, or mark someone as no longer active.

**Status:** Done

Tickets:
- **Add, edit, and deactivate people one at a time** — Basic admin controls for keeping the list accurate.
- **Add or update many people at once from a spreadsheet** — Upload a spreadsheet to onboard, update, or deactivate a batch of people in one go, including a "Default" shortcut for standard Monday–Friday schedules.
- **Set each person's role and access level** — Mark someone as a regular employee, department admin, or top-level admin, plus separate flags for "Developer" (ticket system) access.
- **Set who each person reports to** — Record each person's manager, used to route their leave/overtime requests to the right person.

---

## Task 10: Manager and Admin Access Levels

**Description:** Makes sure people can only see and change what they're
actually supposed to — a regular employee can't see company-wide data,
and a department manager can't see or touch other departments.

**Status:** Done

Tickets:
- **Three levels of access** — Regular employee, department-level admin, and top-level (super) admin, each with different screens available.
- **Keep department managers inside their own department** — Every screen that shows or changes data checks this on the server, not just by hiding buttons on screen.
- **Separate "Developer" access for the ticket system** — A person can be a developer (handles bug/feature tickets) independently of whether they're also an admin.

---

## Task 11: Reports and Excel Downloads

**Description:** Ready-made reports for admins, each downloadable as an
Excel file so the data can be shared or handed off outside the app.

**Status:** Done

Tickets:
- **Attendance report** — Shows worked hours, leave, and overtime for a chosen period.
- **Strikes report** — Shows compliance strikes per person over a chosen period.
- **Time by project/task report** — Shows how much time went to each project and task, with a month-by-month trend view.
- **Download any report as an Excel file** — Every report can be exported with one click.

---

## Task 12: Support and Bug Tickets

**Description:** A place for employees and admins to ask questions or
report problems with the app itself, and for a small "developer" team to
track and fix them.

**Status:** Done (Off for now)

Tickets:
- **Let anyone send a support question** — A simple message box that goes to an admin, with the reply visible in the app.
- **Let anyone raise a bug or feature ticket** — Report a problem or suggest an improvement, with comments and status tracking (open, in progress, done).
- **Give developers their own ticket queue** — People marked as "Developer" can see, comment on, and update the status of tickets.
  *Note: this whole feature is built and tested but switched off in the live app for now — turning it on is a one-line settings change whenever we're ready.*

---

## Task 13: Employee Profile (Personal & Bank Details)

**Description:** Lets each employee fill in and update their own personal
and payment details, instead of that information living in someone's
inbox or a shared file.

**Status:** Done

Tickets:
- **Let employees fill in personal details** — Things like emergency contact and ID information, editable by the employee themselves.
- **Let employees fill in bank/payment details** — Bank account information for payroll purposes, entered directly by the employee.
- **Hide sensitive numbers, even from admins** — Bank account and ID numbers are always shown with most digits hidden (e.g. only the last 4 visible), everywhere in the app, no exceptions.
- **Let employees upload a profile photo** — A simple photo upload shown next to their name.
- **Let admins view (but not silently change) these details** — Admins can see what's on file, but can't overwrite an employee's own entries without them knowing.

---

## Task 14: Task and Project Suggestions

**Description:** Lets employees suggest a new project or task name if the
one they need isn't in the list yet, without needing an admin to set
everything up in advance.

**Status:** Done

Tickets:
- **Let employees suggest a new project or task** — Type in a name if it's missing from the list.
- **Require admin approval before it can be used** — A suggested project/task can't be logged against until an admin approves it, to keep the list clean.
- **Let managers assign projects/tasks to their team** — A lightweight, informational way for a manager to suggest which projects their team should be logging time against.

---

## Task 15: Bringing In the Old Spreadsheet Data

**Description:** One-time work to pull the historical data out of the
three old spreadsheets and load it into the new app, without losing or
changing anyone's past record.

**Status:** Done

Tickets:
- **Read the old spreadsheet files reliably** — Handle the old files even though one of them is very large (hundreds of megabytes) and slow to open normally.
- **Load roster, leave, and compliance history into the app** — Bring across the people list, leave history, and strike history.
- **Prove the numbers match exactly** — An automatic check that compares the app's computed strike counts against the old spreadsheet's counts for every person, every month, and confirms they match exactly before trusting the new system with anything new.
- **Keep a record of anything odd found during import** — Any strange or unclear data in the old files is logged during import instead of silently guessed at.

---

## Task 16: Extra Security (Beyond Login Protection)

**Description:** Additional protection added after a routine third-party
risk review noticed unusual traffic hitting the app.

**Status:** Done

Tickets:
- **Slow down repeated attempts from the same computer** — On top of the per-account lockout, one computer/location making a lot of sign-in or sign-up attempts in a short time gets automatically slowed down, even across different email addresses.
- **Keep sensitive numbers masked everywhere** — Same protection as Task 13 — bank and ID numbers are never shown in full, to anyone.
- **Log every change an admin makes to someone's status** — Any manual change to a compliance status requires a reason and is permanently recorded, so it can be reviewed later.

---

## Task 17: Guides and Instructions

**Description:** Plain-English documents that explain how to use the app,
for people who aren't going to read the code.

**Status:** Done

Tickets:
- **Write a guide for employees** — Step-by-step walkthrough of every screen an employee uses.
- **Write a guide for admins** — Step-by-step walkthrough of every admin screen and what each button does.
- **Write up what the app is for and why (business case)** — A plain document explaining the business reasons behind the app and everything added to it.
- **Write up how the app works (technical spec)** — A more detailed document for anyone who needs to understand exactly how each screen and rule works.
- **Provide PDF copies of the business and technical documents** — Same content as above, in PDF form for easy sharing.

---

## Task 18: Small Fixes and Improvements

**Description:** Smaller polish items — things that make the app nicer to
use day-to-day without changing what it does.

**Status:** Done

Tickets:
- **Redesign the "Today" page layout** — Cleaner, less cluttered layout based on manager feedback.
- **Widen the page so it uses the screen properly** — Fixed the app looking cramped on wide monitors.
- **Remove the "POC" label from the top of the app** — Removed the small badge next to the logo now that this is the team's real day-to-day system.
- **Show every timestamp in one consistent format** — All dates now show the same way (month/day/year) everywhere in the app.
- **Always use one official time zone for "today" and clock times** — So a login from a different country doesn't produce the wrong date or time on someone's record.

---

## Not Started Yet

These have been discussed but aren't built:

- **Payslips through the app** — Letting employees view/download their payslip inside the app, instead of it coming through a separate channel.
- **Further UI polish** — General visual/usability improvements beyond what's listed in Task 18, to revisit after the current rollout settles.

---

*This list was put together from the app's actual current features as of
19 Aug 2026. If you create these in Jira and something looks off or
missing, that's a sign this document needs updating too — treat it as a
living list, not a one-time export.*
