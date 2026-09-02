> **Purpose of this file:** a single, self-contained briefing so a new developer or a fresh Claude session (with no access to prior chat history) can pick this project up safely. It summarizes and points into two much larger living documents that already exist in this repo — `CLAUDE.md` (an exhaustive, dated, append-only log of every decision and change) and `README.md` (a maintained feature/screens reference) — rather than duplicating them. When this file and those disagree, treat `CLAUDE.md`'s most recent entry as the tiebreaker; it is updated in the same batch as every code change and is the closest thing this project has to a changelog.
>
> Written: 2026-09-02. Everything below was verified against the actual repository contents and running code on that date, not recalled from memory — anywhere that wasn't possible is explicitly marked **Needs verification**.

# PROJECT_HANDOVER.md — MK Timekeeping & Compliance App

## 1. Project overview

**Purpose.** An internal FastAPI + SQLAlchemy + Jinja2 web app for MK Immigration Law that replaces three manual spreadsheets (a per-person "Task Summary" file, a 57-tab "Leave Tracker", and a monthly "Compliance sheet") used to track staff time, leave, and compliance. Employees log their work once; leave balances, hours variance, and compliance ("strike") status are all derived automatically instead of hand-calculated. It started as a proof-of-concept (see `HANDOFF.md`, written 2026-07-27) and is now a live, daily-use internal tool — the "POC" framing in some older docs is historical, not current status.

**Users.** ~45 staff, originally all India-based offshore, now a mix of India- and US-based employees. Three access tiers plus one independent flag:
- **Employee** — logs their own time, requests leave/overtime, views their own history.
- **Admin (department-scoped / "Team Lead")** — a narrow, explicit set of capabilities scoped to their own department (see §6 "Authentication & authorization" below for the exact list).
- **Super Admin** — every admin screen, every department; the original full-admin experience.
- **Developer** (`Employee.is_developer`) — a fourth, independent axis (a plain employee can be a Developer; an admin need not be one) that gates only one action: changing a ticket's status in the (currently disabled) Ticketing System.

**Main features** (see README.md's "Screens & roles" table for the full, current, per-screen detail — this is a summary):
- Daily time entry ("Today" page) with overlap/gap/cap validation, an auto-timer, Punch In/Out with a live countdown, break tracking, and "Plan for the Day" (pick work in advance, Start/Pause/Resume/Stop, plan up to a week ahead).
- "My Month" personal history: a day-by-day status ledger, running variance balance, and live strike count.
- Employee self-service Leave requests (5 leave types, automatic tenure-based accrual, partial approval) and Overtime pre-approval requests, each now optionally routed through a two-stage "Team Lead recommendation → Super Admin final decision" approval flow.
- Admin compliance dashboard (live monthly grid, strike/violation flags, a weekly compliance-trend chart, a projects-progression chart, a "needs attention" panel).
- Four Reports pages with Excel export: Attendance, Strikes, Time by Project/Task (with a By-Project bar chart), and Task Logs (per-day breakdown with an AI-generated or rule-based daily summary and an "unplanned work" flag).
- Roster management (add/edit/deactivate, bulk Excel onboarding/updating/offboarding), Projects & Tasks list management (department- and project-scoped dropdown values), manual compensation ("shortfall vs. surplus day") linking, Support questions, and a built-but-disabled bug/feature Ticketing System.
- Employee Profile: photo upload, Personal Details, and Employment/Bank Details (sensitive numbers always masked to last-4, everywhere, including to admins).

## 2. Current status

**Complete and in daily use** (per `README.md`'s own "Status" list at the top of the file, which is kept current):
- All original PRD (`docs/PRD.md`) requirements — see §13 "PRD traceability" in README for the mapping.
- Real self-signup password authentication with lockout (`AUTH_MODE=password`).
- Leave Management V2 (5 leave types, tenure-based accrual, partial approval) — **enabled by default**.
- Task Planning ("Plan for the Day" + Start/Pause/Resume/Stop), including plan-ahead (up to the end of the current work week, or next Monday if today is the last work day of the week) and an optional estimated-minutes field.
- Department-scoped Projects and Project-scoped Tasks (a project/task can be narrowed to specific departments/projects; unscoped = visible to everyone).
- Multilevel ("Team Lead → Super Admin") approval for Leave, Overtime, and Overtime↔Missed-Hours match requests, with a visual progress-step tracker — **enabled by default**, as of 2026-09-01/02.
- AI-generated daily task-log summaries via Groq's hosted API, with a deterministic rule-based fallback when the AI call isn't configured or fails.
- Reports (Attendance, Strikes, Time by Project/Task, Task Logs) with KPI tiles, filters, and Excel export.
- Department-based nav/UI reorganization completed 2026-08-29 → 2026-09-02 (see README's "Status" list and `CLAUDE.md`'s dated bullets for the detailed history — there were many rapid UI iterations in this window).

**In progress / not fully verified:**
- **The full `pytest` suite has not been run against this exact checkout.** `tests/` contains 17 files, ~507 individual `test_...` functions as of this writing. The most recent *confirmed* run Ganesh reported was "468/469" on 2026-08-22, before roughly two more weeks of feature work (multilevel approval, AI summaries, department-scoped projects, plan-ahead, etc.) — **Needs verification**: run `pytest tests/ -q` fresh before relying on this number.
- **`legacy.verify_strikes` (the 168/168 acceptance check) has not run in a long time.** This developer's local checkout is missing the legacy `.ods` files, `tms.db`'s original import bundle, and `legacy/cache/import_report.json` (deliberately git-ignored — real HR data). Every `engine.py`/`validation.py` change since at least 2026-08-21 (Leave V2, department-scoped projects, the auto-count-logged-hours change, etc.) has been verified only via `py_compile` + standalone Python re-implementations + hand-built Jinja render harnesses, **never against the real acceptance test.** This is a known, explicitly-accepted gap (Ganesh's own call each time it came up), not an oversight — but it means the 168/168 guarantee has been unconfirmed for several weeks of engine changes. Locating "the handoff bundle" (likely held by a person named "Steve" — see the `steve` git remote in §9) and re-running `python -m legacy.verify_strikes` is the single highest-priority verification task outstanding.
- **Uncommitted local changes exist right now** (see `git status` in §9) — a same-day department-multi-select feature on "Add a project" and the associated CLAUDE.md documentation bullet. Review and commit these (or ask the person who made them) before doing further work.
- Route-level HTTP tests (`httpx.TestClient` smoke tests) — flagged as a to-do in `HANDOFF.md` since the original build; **not started**, per a search of `tests/` (no `test_routes.py`-style file exists).
- `AUTH_MODE=entra` (Microsoft Entra ID / Azure AD SSO) — the long-term target auth mode named throughout `app/routes/auth.py` and `HANDOFF.md`; **not implemented**. The swap point exists (`app/auth.py`) but no MSAL code has been written.
- End-of-day reminder / strike-alert notifications, phase-2 anomaly detection (pre-shift work, meta-work ratios) — named in `HANDOFF.md`'s original backlog; **not started**.
- Payslips through the app, further general UI polish — named in a "future features" note as explicitly post-launch/on-request only; **not started**.
- Ticketing System — fully built and tested (routes, templates, Developer role gating) but deliberately **disabled** in production via `TICKETING_ENABLED=0` (default off) until Ganesh decides to turn it on; no code work needed, just an env var flip.

## 3. Tech stack

- **Language:** Python. **Hard project rule: target Python 3.9 syntax** — no `X | Y` union type annotations at runtime; use `typing.Optional[...]` instead. (The actual interpreter used to build/test this on the original author's Mac drifted to 3.14 at points — see §10 "Known issues"; this rule exists so the code stays correct on whatever Python 3.9+ a host actually runs.)
- **Web framework:** FastAPI (server-rendered, not an API-first app — routes return Jinja `HTMLResponse`s, not JSON, except the health check).
- **ORM / DB layer:** SQLAlchemy 2.x (`DeclarativeBase` style), used with both SQLite (local dev default) and PostgreSQL (production) via one unified `DATABASE_URL`-driven config in `app/db.py`. No migration framework (no Alembic) — see §9.
- **Templating:** Jinja2, server-rendered pages, minimal hand-written vanilla JS (no frontend framework, no build step, no Node dependency) — `app/static/*.js` are small, dependency-free hand-rolled widgets (`combo.js` searchable select, `msdrop.js`/`msfilter.js` multi-select dropdowns, `mdy_datepicker.js`, `tablefilter.js`).
- **Session/auth:** Starlette `SessionMiddleware` (signed cookies via `SECRET_KEY`), stdlib password hashing in `app/security.py` (no external auth library).
- **Excel I/O:** `openpyxl` (all bulk-upload parsing and report/XLSX export).
- **HTTP client:** `httpx` (used for the outbound Groq AI-summary API call, and as the FastAPI `TestClient` engine for any future route tests).
- **AI summary backend:** Groq's hosted, OpenAI-compatible Chat Completions API (`https://api.groq.com/openai/v1/chat/completions`), model `openai/gpt-oss-20b` by default — see `app/llm_summary.py`. This is the *fourth* backend this one feature has had (Anthropic → rule-based-only → Gemini → self-hosted Ollama → Groq); see CLAUDE.md's dated bullets for the full swap history and the specific reasons (cost, data-training policy, and two live production bugs — a deprecated model ID and a reasoning-model token-budget issue).
- **Production server:** `gunicorn` with a `uvicorn.workers.UvicornWorker` (per README's Azure instructions) or plain `uvicorn` (per `Procfile`/local dev) — see §9 for which is actually in use.
- **Testing:** `pytest` (17 test files, ~507 tests — see §2 for current-run caveats).
- **Database (production target):** PostgreSQL 16, via `psycopg[binary]`. **Currently provisioned on Google Cloud SQL** (`db-f1-micro`, `asia-south1`/Mumbai region, project id `mk-timekeeping`) per `deploy_gcp.sh` — see §9 for the important discrepancy between this and README's own "Path to production" section, which still describes Azure.
- **Hosting (production target, currently active):** Google Cloud Run (`gcloud run deploy`, source-based build, no Dockerfile) — see §9.
- **File/blob storage:** avatar photos and (if `TICKETING_ENABLED`) ticket attachments are served from a local directory by default, overridable via `AVATAR_UPLOAD_DIR`/`TICKET_ATTACHMENT_UPLOAD_DIR` env vars; on Cloud Run this is mounted from a Google Cloud Storage bucket via a Cloud Run volume mount (see `deploy_gcp.sh`).
- **No JS package manager, no CSS framework, no ORM migration tool, no message queue, no cache layer, no CDN** — deliberately minimal, "internal tool for ~45 people," not enterprise-scale infrastructure.

## 4. Local setup

### Prerequisites
- Python 3.9+ (the code must stay 3.9-compatible even if a newer interpreter is installed locally — see §3).
- No Node.js / npm required.
- **The project directory name historically ended with a trailing space** in the original handoff bundle (`"...Time Management System "`) — always quote paths if you ever see this. The current checkout at `/Users/Ganesh/Projects/mk-timekeeping-poc-main` does not have this issue, but be aware of it if working from an older copy or the original zip.

### Install
```bash
cd "/Users/Ganesh/Projects/mk-timekeeping-poc-main"
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```
`requirements.txt` is **unpinned** (no version numbers) by design (originally a quick POC) — see §10 for a known dependency-drift bug this has already caused once, and the recommended runtime-introspection-over-hardcoding fix pattern already applied.

### Run locally
```bash
# Real app, dev auth (pick-a-user login, no password) — default AUTH_MODE=dev
.venv/bin/python -m uvicorn app.main:app --port 8127
# → http://localhost:8127
```
```bash
# Demo mode — runs the committed, anonymized tms_demo.db on a separate port; safe to show anyone
.venv/bin/python -m demo.run_demo
# → http://localhost:8128
```
To exercise `AUTH_MODE=password` (the real employee self-signup/login flow) locally:
```bash
AUTH_MODE=password SECRET_KEY=local-test-key .venv/bin/python -m uvicorn app.main:app --port 8127
```
(The app **refuses to boot** without a `SECRET_KEY` when `AUTH_MODE` is anything other than `dev` — this is intentional, see `app/main.py`.)

### Rebuild the database from the legacy source files (only if you have the original bundle)
```bash
.venv/bin/python legacy/extract_tasks.py "Task Summary  - Divya (2).ods" legacy/cache/divya
.venv/bin/python -m legacy.import_legacy
```
The importer refuses to run against a non-empty database — `rm tms.db` first if re-importing. **This developer's checkout does not have the source `.ods` files** — see §10. A pre-seeded `tms.db` already exists in this checkout (348 KB as of writing) and works without re-importing.

### Test / verify
```bash
.venv/bin/python -m pytest tests/ -q          # full unit test suite
.venv/bin/python -m legacy.verify_strikes     # acceptance check — must print 168/168 (needs the legacy bundle; currently NOT runnable in most dev environments — see §2/§10)
```
There is **no separate lint command** configured (no `ruff`/`flake8`/`black` config file found in the repo root). `python3 -m py_compile <file>` is used throughout the project's own history as a lightweight syntax check when a full `pytest` run isn't available (see §10's "sandbox limitations" note — this is a documented, accepted substitute in environments without network/package access, not a replacement for a real `pytest` run before shipping).

### Build
No build step. No compiled assets, no bundler, no Dockerfile in the repo (Cloud Run builds directly from source — see §9).

## 5. Repository map

```
app/
├── main.py                 FastAPI app instance, middleware, exception→error-page handlers,
│                            /healthz, startup hooks (DB init + all ensure_* backfills)
├── db.py                   SQLAlchemy engine/session setup; SQLite by default, DATABASE_URL for Postgres;
│                            additive-only auto-migration (_add_missing_columns)
├── models.py                All 28 ORM tables + CONFIG_DEFAULTS (see §6)
├── engine.py                 ALL business/compliance math: status/variance/strike computation,
│                            recompute, running ledger, leave balances, holidays. TOUCH ONLY WITH
│                            tests + verify_strikes (project hard rule)
├── validation.py            Entry-level validation rules: overlap blocking, gap flagging, 4h row cap,
│                            back-dating window, department/project-task scoping checks. Same hard rule
│                            as engine.py
├── compensation.py          Automatic Punch-Clock compensation balance — independent of engine.py/
│                            DayStatus/strikes
├── auth.py                  AUTH_MODE switch (dev/password/entra) + all role-gating dependency
│                            functions (require_admin, require_super_admin, require_developer,
│                            admin_department_scope, led_by) — THE Entra ID swap point
├── security.py               Stdlib password hashing for AUTH_MODE=password
├── rate_limit.py              In-memory per-email lockout + per-IP throttle for login/signup
├── bulk_upload.py             Roster Excel parsing (onboard/update/deactivate)
├── leave_bulk_upload.py        Bulk leave-entitlement Excel parsing
├── lists_bulk_upload.py        Bulk Project/Task dropdown-value Excel parsing (with department/
│                            project linking columns)
├── holiday_bulk_upload.py      Bulk holiday Excel parsing
├── llm_summary.py              Groq AI day-summary integration — zero DB/app coupling by design,
│                            independently testable/swappable (this is the 4th backend it's had)
├── reports.py                  Attendance/Strikes/Time-by-Project-Task/Task-Logs report aggregation,
│                            rule-based day-summary fallback, Excel export row-builders
├── util.py                     Formatting filters, audit(), employee-code generation, xlsx_response(),
│                            now_local()/today_local()/BUSINESS_TZ, every ensure_*_backfill function
├── templating.py               Jinja env + filters (hm, hm_signed, clock, mdy, mdy_dt, tojson), feature
│                            flags (TICKETING_ENABLED, HOLIDAY_MANAGEMENT_ENABLED,
│                            LEAVE_MANAGEMENT_V2_ENABLED, MULTILEVEL_APPROVAL_ENABLED), nav badges
├── routes/
│   ├── auth.py                 /, /login, /login/employee, /login/admin, /signup, /logout
│   ├── employee.py             /today, /my-month, /leave, /overtime, /holidays, /support, /profile*,
│   │                        plus every POST action under those (entries, breaks, punch, plan/*, etc.)
│   ├── admin.py                 /admin (dashboard), /admin/person/{id}, /admin/roster*, /admin/lists*,
│   │                        /admin/leave*, /admin/overtime*, /admin/holidays*, /admin/config,
│   │                        /admin/audit, /admin/support, /admin/suggestions, /admin/assignments
│   ├── reports.py                /admin/reports/* (time, attendance, strikes, usage, tasklogs) + .xlsx
│   ├── exports.py                 /export/dashboard.xlsx, /export/person/{id}.xlsx, /export/entries.csv
│   └── tickets.py                 /tickets*, gated behind TICKETING_ENABLED
├── templates/                  All server-rendered Jinja pages; templates/admin/ for admin-zone pages;
│                            _macros.html for shared Jinja macros (e.g. the approval-progress stepper)
└── static/                     app.css + hand-rolled vanilla-JS widgets (no framework, no build step)

legacy/                     Self-contained: streaming ODS reader (survives a 700MB degraded XML file),
                            task extractor, the one-time importer, and the acceptance verifier
                            (verify_strikes.py — must print 168/168 after any engine/importer change)
demo/                       make_demo_db.py (builds the anonymized tms_demo.db + fixed test logins),
                            run_demo.py (serves it on port 8128), seed_test_logins.py
tests/                      17 pytest files, ~507 tests — engine, validation, util, all bulk-upload
                            variants, reports, auth, compensation, overtime, tickets, rate_limit
docs/                       PRD.md/PRD.pdf (requirements), BRD.md/BRD.pdf, LEAVE_MANAGEMENT_PLAN.md,
                            TASK_PLANNING_TIMER_PLAN.md, PERFORMANCE_REVIEW.md, JIRA_BACKLOG.md
                            (plain-English backlog, last refreshed 2026-08-19 — now stale, see §12),
                            DATABASE_COSTING.md (GCP Cloud SQL cost breakdown, added 2026-09-02),
                            storage_capacity_estimate.md, flow_diagram.png, training/
load_test/                  locustfile.py + seed script — see load_test/README.md for the "100
                            concurrent users" load-test tooling built during a 2026-08-21 perf review
CLAUDE.md                   THE most detailed and most current running log of every decision/change in
                            this project, in strict chronological order, with reasoning. ~285KB.
                            Read this for anything not covered here.
README.md                   Maintained user/developer-facing reference — screens & roles, status
                            computation rules, config reference, legacy import notes, testing,
                            "Path to production" (see §9 for a caveat on this section), troubleshooting
HANDOFF.md                  The *original* 2026-07-27 handoff note — historically useful for context,
                            but its "current state" table is now outdated (written when this was still
                            a pure POC with dev-only auth and no deploy target)
Procfile                    Leftover from an early Railway-hosting plan (see §9) — harmless, unused by
                            the current GCP Cloud Run deployment
deploy_gcp.sh / redeploy_gcp.sh   The scripts that actually provision/redeploy the current production
                            environment on Google Cloud Run + Cloud SQL — see §9
deploy_azure.sh / redeploy_azure.sh   An alternate/earlier Azure App Service deployment path — appears
                            unused by current production; README's own "Path to production" section
                            still documents this path. **Needs verification** with Ganesh which (if
                            either) is the intended long-term target.
tms.db / tms_demo.db        SQLite database files committed/present in this checkout (see §9's note on
                            why this is unusual for a "production on Postgres" project)
```

## 6. Architecture

### Major components
1. **`app/engine.py`** — the compliance "brain." Computes each employee-day's status (Complete/Partial/Missing/Leave/Holiday/Weekend), variance (`actual − effective_target`), the running balance ledger, and monthly strike counts (`strikes = count(Missing) + count(Partial)`, excluding `strike_exempt` legacy pre-policy days). No cron/scheduler exists — recomputation happens synchronously on relevant page loads (Dashboard, My Month, Person Detail) and after any mutating action (submit day, leave/override/comp-link change). A manual "Recompute month" button exists for peace of mind. At ~45 people × 31 days this costs milliseconds.
2. **`app/validation.py`** — entry-level rules enforced on every `TaskEntry` write: no overlapping rows, no logging over an already-logged break, an unexplained-gap flag (net of any logged break time) at a configurable threshold, a max single-row duration cap, a back-dating window, and (added 2026-08-27/28) department- and project-scoping checks (`project_allowed_for_department`, `task_allowed_for_project`).
3. **Two-layer "frozen history vs. live computation" model**: `DayStatus.source='imported'` rows are permanently frozen legacy fact (raw sheet token preserved in `imported_token`, never recomputed); `source='computed'` rows rebuild freely from live `TaskEntry`/`LeaveRecord` data from `Config.live_start_date` onward. This split is why the legacy acceptance test (`verify_strikes`) stays stable even as the live app keeps computing new days.
4. **Status precedence** (in `DayStatus.effective_status()` + `engine.strikes_in()`): admin override (mandatory reason, audited) → compensation (a fully-covered shortfall reads Complete if `comp_erases_strike` is on) → base computed/imported status. `strike_exempt` days never count as strikes regardless of anything else.
5. **Multilevel approval layer** (added 2026-09-01, `MULTILEVEL_APPROVAL_ENABLED`): an additive set of columns (`requires_lead_review`, `lead_decision`, `lead_reason`, `lead_reviewed_by`, `lead_reviewed_at`) on `LeaveRecord`, `OvertimeApproval`, and `CompensationLink`. A department-scoped admin can record an accept/deny *recommendation*; the existing Super-Admin-only approve/reject routes are the only ones that ever flip the real `status` column. This is a soft gate — a Super Admin can always act with or without a prior Team Lead review (so a department with no admin assigned never permanently stalls a request).
6. **AI/rule-based day summaries** (`app/llm_summary.py` + `app/reports.py`): at Submit Day, task entries for that day are optionally sent to Groq's API for a 3-5 bullet summary, stored on `DaySubmission.summary_text`/`summary_error`. If Groq isn't configured or the call fails, `reports.rule_based_day_summary()` (a pure, deterministic, zero-network function, sorted most-time-project-first) is used instead — this fallback is always computed and always available regardless of AI configuration.

### Data flow
```
Employee (Today page)  ──┐
Admin (leave/override/   ├──► TaskEntry / LeaveRecord / DaySubmission / etc. (SQLAlchemy models) ──► SQLite or Postgres
  comp-link actions)    ──┘
                                          │
                                          ▼
                              app/engine.py (recompute on read)
                                          │
                                          ▼
                     DayStatus rows ──► Dashboard / My Month / Person Detail / Reports
                                          │
                                          ▼
                              XLSX/CSV exports (openpyxl)
```
Legacy one-time import path (only relevant if re-seeding from the original spreadsheets — see §4):
```
Legacy .ods files ──► legacy/extract_tasks.py / legacy/import_legacy.py ──► tms.db ──► legacy/verify_strikes.py (168/168 check)
```

### Database tables/models (28 tables, all in `app/models.py`)
`Employee`, `EmployeePersonalDetails`, `EmployeeBankDetails`, `Project`, `TaskType`, `ProjectAssignment`, `TaskAssignment`, `ProjectTask` (project↔task scoping), `ProjectDepartment` (project↔department scoping), `TaskEntry`, `BreakEntry`, `PunchSession`, `ActiveTaskTimer`, `PlannedTask` (Task Planning), `DaySubmission` (also holds the AI summary fields), `UnlockRequest`, `LeaveRecord`, `SupportQuery`, `OvertimeApproval`, `Ticket`, `TicketComment`, `Holiday`, `DayStatus`, `CompensationLink`, `SpecialPaidGrant`, `Config` (key/value settings, defaults in `CONFIG_DEFAULTS`), `AuditLog`.

Notable `Config` keys (full list — `app/models.py`'s `CONFIG_DEFAULTS`): `tolerance_minutes` (60), `strike_threshold` (5), `max_row_minutes` (240), `backdate_working_days` (1), `gap_flag_minutes` (15), `min_details_chars` (5), `comp_erases_strike` (on), `live_start_date`, `max_break_minutes` (30), `probation_days_default` (180), `planned_days_year_0_2/_2_5/_5_plus` (9/11/13 days), `unplanned_hours_year_cap` (40 hrs/yr), `compliance_target_pct` (90). All durations are **integer minutes** — there is a strict, repeatedly-enforced project rule against floats anywhere in duration/target/variance math.

### API / route map
This is a server-rendered app, not a JSON API — every route below returns an HTML page (Jinja) except the health check and `.xlsx`/`.csv` export endpoints. See §5 for the file each router lives in. Key prefixes: `/` (auth), `/today`, `/my-month`, `/leave`, `/overtime`, `/holidays`, `/support`, `/profile*` (employee, no prefix), `/admin/*` (admin dashboard/roster/lists/leave/overtime/holidays/config/audit/support), `/admin/reports/*` (the four report pages + their `.xlsx` twins), `/export/*` (dashboard/person/entries exports), `/tickets*` (gated off by default). `/healthz` runs a real `SELECT 1` for uptime monitoring.

### Authentication & authorization
- **`AUTH_MODE` env var**, three values: `dev` (default — pick-a-user, no password, local only), `password` (current production mode — self-signup email+password against a roster row an admin already created, 5-failed-attempt lockout), `entra` (**not implemented** — the intended long-term target; `app/auth.py` is explicitly documented as "THE swap point," session/role gating is designed to stay identical once it's built).
- **Session:** Starlette `SessionMiddleware`, signed with `SECRET_KEY`. HTTPS-only cookies except in `dev` mode. The app refuses to start without a real `SECRET_KEY` outside `dev` mode.
- **Role gating** (`app/auth.py`, all as FastAPI `Depends`): `require_admin` (any admin tier), `require_super_admin` (org-wide only), `require_developer` (ticket status changes only), `require_developer_or_admin` (Usage Report). `admin_department_scope(admin)` returns `None` for a Super Admin (unscoped) or the admin's own department string otherwise. `led_by(admin, db)` returns the set of employee IDs who report directly to that admin (used for Overtime Request scoping, a *per-person* axis independent of department).
- **Exact department-admin capability boundary** (narrowed 2026-08-28, partially reopened 2026-08-30 — see `app/auth.py`'s `require_super_admin` docstring for the authoritative, current version): a department-scoped admin can add Project/Task names (form + bulk upload), assign Projects/Tasks to their team, approve suggested Project/Task names, view (read-only) task logs and Time/Project/Strikes/Attendance reports for their team, and — since 2026-08-30 — **view-only** Leave Management/Overtime Management. Every mutating Leave/Overtime route, Roster, Settings, Audit Log, Support Inbox, and Projects & Tasks list-maintenance (deactivate/rename) stay Super-Admin-only.

### Integrations
- **Groq** (AI day summaries) — the only outbound third-party API call in the app. Optional; the app functions identically without a `GROQ_API_KEY` set (falls back to the rule-based summary). See §8 for the env vars and §10 for the data-handling tradeoff already evaluated.
- **Google Cloud SQL / Cloud Storage** — current production database and avatar/ticket-attachment file storage, provisioned via `deploy_gcp.sh` (see §9).
- No other third-party services (no email/SMTP integration exists yet — "reset password" is an admin-driven Roster button, not a self-service email flow; no payment/billing integration; no SSO provider wired up yet).

## 7. Key decisions

- **One fixed reference timezone (`America/Chicago`), never per-employee, never the server's own clock.** Every "now"/"today"/clock-face value the app captures goes through `app/util.py`'s `now_local()`/`today_local()` (backed by `BUSINESS_TZ`), never `dt.datetime.now()`/`dt.date.today()` directly. Decided 2026-08-10 after an offshore (India-based) employee's auto-timer logged the wrong start time because the server container defaulted to UTC. `dt.datetime.utcnow()` is still correct and used, unchanged, for pure audit-trail timestamps (e.g. `reviewed_at`) that aren't clock-face values.
- **Integer minutes everywhere, no floats**, for every duration/target/variance. Prevents rounding-error drift across a large number of small time computations.
- **No migration framework (no Alembic).** Schema evolution is additive-only: `app/db.py`'s `_add_missing_columns()` runs `ALTER TABLE ADD COLUMN` for any ORM-declared column the live table doesn't yet have, on every startup. This is deliberately *never* a full "drop the DB and reimport" step once real employee data exists — that path is reserved for genuinely early development only. A recurring gotcha (hit multiple times, see §10) is that this auto-migration never *backfills* a meaningful default into existing rows — SQLite/Postgres both leave them `NULL` — so any new column with a non-NULL-safe default needs its own dedicated `ensure_*_backfill()` function in `app/util.py`, wired into `app/main.py`'s startup sequence.
- **"Each Start-to-Pause/Stop segment becomes its own ordinary `TaskEntry` row"** (Task Planning design, 2026-08-21) — a simpler alternative to the original design doc's "sum multiple segments into one row" approach, chosen specifically because it meant `engine.py`/`validation.py` needed **zero** changes (every segment flows through the exact same validation path a manually-typed row already used). This is a deliberate, documented trade-off — don't "improve" this into a summed-segments model without understanding it reopens `engine.py`/`validation.py` risk that was specifically avoided.
- **Holidays are one shared, company-wide list, not per-country.** A brief 2026-08-12 attempt to split holidays by country (`Employee.location`) was reverted within two days (2026-08-14) after the team decided they wanted one common calendar for both US and India staff. The `Employee.location`/`Holiday.location` columns still exist (dropping a column isn't a safe additive-only change once real data exists) but are no longer read for any filtering logic.
- **"Zero links = unrestricted" convention for scoping.** Both `ProjectDepartment` (project↔department) and `ProjectTask` (project↔task) use the same rule: a project/task with no explicit link rows is visible/usable everywhere; one or more links narrow it. This was chosen specifically so rolling out department/task scoping never silently broke any of the ~300 pre-existing, unlinked Project rows.
- **Sensitive personal data (bank account numbers, PAN/Aadhaar/UAN/ESI) is masked to last-4 everywhere, permanently, including to admins**, the instant it's saved — never round-tripped back to the browser in full. The edit form for these fields always starts blank; submitting blank means "leave unchanged." This convention (`app/util.py`'s `mask_tail`) is the mandatory pattern for any future sensitive field added to this app.
- **AI day-summary backend is deliberately isolated** (`app/llm_summary.py` has zero DB/ORM/FastAPI imports) so it can be unit-tested and swapped independently of the rest of the app — it has already been swapped 4 times (Anthropic → rule-based-only → Gemini → Ollama → Groq) for cost and data-handling reasons, and the deterministic rule-based fallback (`app/reports.py`'s `rule_based_day_summary()`) has never changed through any of those swaps.
- **Feature flags for anything that touches pay-adjacent logic or a large surface area**, defaulting to a safe/existing behavior: `TICKETING_ENABLED` (default off), `HOLIDAY_MANAGEMENT_ENABLED` (default on), `LEAVE_MANAGEMENT_V2_ENABLED` (default on), `MULTILEVEL_APPROVAL_ENABLED` (default on). Task Planning is the one large recent feature deliberately shipped **without** a flag, specifically because it doesn't touch `engine.py`/`validation.py` (see above).

## 8. Environment configuration

None of the actual secret values are reproduced below — only the variable names and what each controls. **Do not commit real values for any of these to source control.**

| Variable | Purpose | Default if unset |
|---|---|---|
| `AUTH_MODE` | `dev` / `password` / `entra` — which login flow is active | `dev` |
| `SECRET_KEY` | Session-cookie signing key. **App refuses to boot without this set to a real value when `AUTH_MODE != dev`.** | hardcoded dev-only fallback (only used in `dev` mode) |
| `DATABASE_URL` | SQLAlchemy connection string. `sqlite:///...` for local dev; `postgresql+psycopg://...` for production | local `sqlite:///tms.db` |
| `DB_POOL_SIZE` | SQLAlchemy connection pool size (Postgres only, no effect on SQLite) | `20` |
| `DB_MAX_OVERFLOW` | SQLAlchemy pool max overflow (Postgres only) | `20` |
| `BOOTSTRAP_ADMINS` | `Name:email,Name:email,...` — creates these people as Super Admins on first startup only against an empty employee table; permanent no-op once any employee row exists | (unset — no bootstrap) |
| `AVATAR_UPLOAD_DIR` | Filesystem path where profile photos are stored/served from | in-repo `app/static/uploads/avatars` |
| `TICKET_ATTACHMENT_UPLOAD_DIR` | Filesystem path for ticket attachments (only relevant if `TICKETING_ENABLED=1`) | in-repo `app/static/uploads/tickets` |
| `TICKETING_ENABLED` | `1`/`0` — turns the entire Ticketing System (routes, nav links, templates) on/off | `0` (off) |
| `HOLIDAY_MANAGEMENT_ENABLED` | `1`/`0` — turns Holiday Management (`/holidays`, `/admin/holidays*`) on/off | `1` (on) |
| `LEAVE_MANAGEMENT_V2_ENABLED` | `1`/`0` — 5-leave-type system with tenure accrual vs. the old 4-type flat system | `1` (on) |
| `MULTILEVEL_APPROVAL_ENABLED` | `1`/`0` — Team Lead recommendation stage ahead of Super Admin approval for Leave/Overtime/comp-match requests | `1` (on) |
| `RATE_LIMIT_DISABLED` | `1`/`0` — disables the login/signup rate limiter (e.g. for load testing) | `0` (enabled) |
| `GROQ_API_KEY` | Groq API key for AI day summaries. Blank/unset = feature silently no-ops, falls back to rule-based summaries — safe to deploy without this set | (unset) |
| `GROQ_MODEL` | Which Groq-hosted model to call | `openai/gpt-oss-20b` |
| `GROQ_TIMEOUT_SECONDS` | Request timeout for the Groq call (sits inside the synchronous Submit Day request) | `10` |

Two more variables are documented in README/CLAUDE.md history but are **not** currently read anywhere in `app/` (searched via grep across the whole `app/` tree) — **Needs verification** whether these were ever wired up or are purely aspirational documentation: none found beyond the table above; the table above is the complete, code-verified list as of this writing.

## 9. Deployment

**Current production target: Google Cloud Run + Cloud SQL (PostgreSQL 16), region `asia-south1` (Mumbai), GCP project id `mk-timekeeping`.** This is based on direct inspection of `deploy_gcp.sh`/`redeploy_gcp.sh` (the only deploy scripts with recent, detailed inline commentary matching the rest of this project's current history) and `docs/DATABASE_COSTING.md` (written 2026-09-02, pricing the exact Cloud SQL config `deploy_gcp.sh` provisions).

**⚠️ Important discrepancy to flag to whoever owns this project:** `README.md`'s own "Path to production" section (§9 of that file) still describes an **Azure App Service** deployment (with `gunicorn`/`az webapp` instructions), and `deploy_azure.sh`/`redeploy_azure.sh` still exist in the repo root. The `Procfile` is a third, even earlier leftover from a brief Railway-hosting plan (per the project memory log). **This suggests the hosting plan changed at least twice (Render → Railway → possibly Azure → GCP) without README's "Path to production" section being updated to match** — a violation of this project's own stated convention ("keep README in sync with every feature/behavior change"). Before doing any deployment work, confirm with the project owner (Ganesh) which environment is actually live right now — the evidence strongly points to GCP Cloud Run, but this is marked **Needs verification** rather than stated as fact, since no direct `gcloud` access was available to confirm a running service at the time this document was written.

### Environments
- **Local dev** — SQLite, `AUTH_MODE=dev`, port 8127.
- **Local demo** — SQLite (`tms_demo.db`, anonymized), `demo/run_demo.py`, port 8128. Safe to show to anyone; refuses to build if its own leak-scan finds a real (non-fictional) string.
- **Production (believed current)** — GCP Cloud Run service `mk-timekeeping`, Cloud SQL Postgres instance `mk-timekeeping-pg`, `AUTH_MODE=password`, avatars on a GCS bucket mounted as a Cloud Run volume.

### Deployment steps (GCP path)
First-time provisioning (creates the Cloud SQL instance, GCS bucket, IAM bindings — safe to re-run, skips what already exists):
```bash
cd /Users/Ganesh/Projects/mk-timekeeping-poc-main
bash deploy_gcp.sh
```
Every subsequent code update (does **not** touch env vars — Cloud Run carries the previous revision's env vars forward automatically):
```bash
bash redeploy_gcp.sh
```
`deploy_gcp.sh` generates a fresh random `SECRET_KEY` every time it runs — **do not re-run it for routine updates**, only `redeploy_gcp.sh`, or every signed-in session gets invalidated for no reason. Both scripts build directly from source via `gcloud run deploy --source .` (no Dockerfile needed).

### CI/CD
**No CI/CD pipeline exists.** No `.github/workflows/`, no GitLab CI, no other automated pipeline was found in the repo. Deployment is a manual, human-run shell script (`redeploy_gcp.sh`). There is no automated test gate before deploy — running `pytest`/`verify_strikes` before deploying is a manual discipline documented in `CLAUDE.md`'s hard rules, not an enforced CI check.

### Migrations
No migration tool (no Alembic). New/changed nullable columns are picked up automatically at the next app startup via `app/db.py`'s `_add_missing_columns()` (a pure additive `ALTER TABLE ADD COLUMN` sweep — it never drops, renames, or backfills). Anything beyond adding a nullable column (renaming, dropping, changing a type, or needing a real default backfilled into existing rows) requires either a dedicated one-off script or, in true early development only, `rm tms.db && python -m legacy.import_legacy` — the latter is **never safe once real production data exists**.

### Rollback notes
- **Code rollback:** `gcloud run services update-traffic mk-timekeeping --to-revisions=<previous-revision>=100 --region asia-south1` (standard Cloud Run traffic-split rollback) — **Needs verification**, this exact command wasn't tested as part of this handover, but it's the standard Cloud Run rollback mechanism and no project-specific override was found.
- **Database rollback:** no automated DB rollback/backup-restore script exists in the repo. Cloud SQL's own automated backups (if enabled on the instance) would be the mechanism — **Needs verification** whether automated backups are actually turned on for `mk-timekeeping-pg`.
- **Feature-flag rollback:** every large recent feature is behind an env var (see §8) that can be flipped off without a code change or redeploy of new code — this is the fastest, lowest-risk "rollback" available for Ticketing/Holiday Management/Leave V2/Multilevel Approval specifically.
- **Schema rollback:** not supported by design (additive-only migration, no down-migrations). A bad column addition would need a manual `ALTER TABLE DROP COLUMN` or living with an unused column.

## 10. Known issues and risks

- **`legacy.verify_strikes` (the 168/168 historical-accuracy acceptance test) cannot currently be run** in the environment these recent changes were made in — the original legacy `.ods` files, `import_report.json`, and the pre-import `tms.db` are missing from this developer's checkout (deliberately git-ignored as real HR data, shared only via an original handoff bundle believed to be held by "Steve" — see the `steve` git remote). **Multiple weeks of `engine.py`/`validation.py` changes (Leave V2, department-scoped projects, the "auto-count logged hours" change, multilevel approval's read-paths) have shipped without this acceptance test confirming 168/168.** This is the single highest-risk open item — locate the bundle and re-run this check before treating the compliance math as fully trustworthy.
- **The real `pytest` suite has similarly not been confirmed against the current checkout** for the same reason in some working environments (a broken `.venv` symlink pointing at a machine-specific Python install, and no network access to reinstall dependencies in that sandboxed environment) — verification of many recent changes relied on `py_compile` + hand-written logic re-implementations + Jinja render harnesses instead of a real test run. **Before trusting any recent change, run the real `pytest tests/ -q` on a working local machine.**
- **Uncommitted local changes exist in the working tree right now** (as of this writing): `CLAUDE.md`, `app/llm_summary.py`, `app/reports.py`, `app/routes/admin.py`, `app/templates/admin/lists.html`, `test_groq_summary.py`, and a binary diff in `tms_demo.db`, plus two untracked new files (`Backend_Overview.docx`, `docs/DATABASE_COSTING.md`). These are the most recent AI-summary prompt fix and the department-multi-select-on-project-creation feature. **Review and commit (or discard) these before starting new work** — do not assume `git log`/`origin/main` reflects the exact current file contents.
- **`requirements.txt` is completely unpinned** (no version numbers on any package). This has already caused one real production bug: a newer Starlette pulled in by an unpinned install changed `Jinja2Templates.TemplateResponse`'s calling convention, 500-erroring every page until `app/templating.py`'s `render()` was made to introspect the installed library's function signature at import time rather than assume a fixed API shape. The established, documented fix pattern for *any* future dependency-drift bug is runtime introspection, not a hardcoded version check — but the underlying unpinned-dependency risk itself has not been resolved (pinning `requirements.txt` via `pip freeze` against a known-good environment is still an open, lower-urgency hardening task).
- **Rate limiting is in-memory and process-local** (`app/rate_limit.py`) — correct today because Cloud Run is believed to run a single instance for this traffic level, but it will silently stop protecting against distributed login-brute-force attempts the moment Cloud Run autoscales past one instance. No CSRF token implementation exists yet either (mitigated partially by `SameSite=Lax` cookies) — flagged in the code's own comments as worth revisiting now that production uses real self-signup passwords, not just a local dev login.
- **Two separate git remotes exist** (`origin` → `Ganesh-18-MK/Time_Tracking_Tool`, `steve` → `skennedy18/mk-timekeeping-poc`) and three local branches (`main`, `admin-sidebar-redesign`, `production-readiness-updates`) beyond the checked-out `main`. **Needs verification** with the project owner: which remote/branch is the actual source of truth, whether the two remotes are meant to stay in sync, and what state the other two local branches are in (untested, abandoned, or in-progress work).
- **A brand-new SQLite database has zero employee rows**, and `/signup` can only *claim* an existing roster row by email — it never creates one. `BOOTSTRAP_ADMINS` is the only way anyone can ever sign into a fresh deploy; without it correctly set on first boot, the app is completely inaccessible. This is a one-time bootstrap concern, not an ongoing risk, but easy to forget on a brand-new environment.
- **The "Add a project" department-dropdown feature** (implemented in this same session, immediately before this handover doc was requested) is new, uncommitted code, verified only via `py_compile` + a standalone Jinja render harness — not yet exercised against a real running app or a real `pytest` run. Treat it as unverified-in-production until confirmed.
- **`app/main.py`'s Python-3.9 constraint is a real, load-bearing constraint, not a style preference** — `X | Y` union syntax anywhere in a runtime type annotation would break on whatever Python 3.9 interpreter production actually runs, even though the union syntax works fine on the newer Python versions used for local development at various points in this project's history. Watch for this in any new code, especially copy-pasted from newer examples.
- **Docstrings throughout `app/models.py`/`app/engine.py`/`app/routes/*.py` carry extensive dated design-decision context** (who asked for what, why, and what alternative was rejected) — this is genuinely valuable and should be preserved/extended for any future change in the same style, not stripped out as "noise" during a refactor.

## 11. Recent work

The most detailed record of this is `CLAUDE.md`'s own dated, append-only bullet log (read that file directly for full detail on any item below — it is the authoritative source). In roughly the last two weeks of activity (2026-08-21 through 2026-09-02), in chronological order:

- **Task Planning** ("Plan for the Day" + Start/Pause/Resume/Stop) built and shipped with no feature flag (`app/models.py` — new `PlannedTask` table, nullable `ActiveTaskTimer.planned_task_id`; `app/routes/employee.py`; `app/templates/today.html`). Later extended: gap-fill rows, auto punch-in on first plan, carry-forward of unfinished plans, admin-assignable plans ("Assign Work" page, TK-04), plan-ahead up to the end of the current work week, and an optional estimated-minutes field.
- **Leave Management V2** — 5 new leave types with tenure-based automatic accrual, partial approval, Bereavement relationship field, management-granted Special Paid Time, and an employee-requested Overtime↔Missed-Hours compensation match — built and enabled by default (`app/engine.py`, `app/models.py`, `app/routes/employee.py`, `app/routes/admin.py`, `app/templates/leave.html`/`admin/leave.html`).
- **Developer Usage Report** (`/admin/reports/usage`) — feature-adoption tracking by a new `TaskEntry.entry_method` column, gated by a new `require_developer_or_admin` auth dependency.
- **Department-scoped Projects and Project-scoped Tasks** — new `ProjectDepartment`/`ProjectTask` join tables and matching `app/validation.py` enforcement (`project_allowed_for_department`, `task_allowed_for_project`), plus a Department→Project→Task tree UI on the Projects & Tasks admin page (`app/templates/admin/lists.html`).
- **Reports overhaul** — Attendance/Strikes/Time-by-Project-Task redesigned with KPI tiles and day-strip mini calendars; new **Task Logs** report (`app/reports.py`, `app/routes/reports.py`, `app/templates/admin/reports_tasklogs.html`) with per-day summaries and an "unplanned work" flag; a **Compliance Trend** and **Projects Progression** chart added to the admin Dashboard (`app/routes/admin.py`, `app/templates/admin/dashboard.html`).
- **AI day summaries** — built against Anthropic's API, immediately replaced with a deterministic rule-based summary (`app/reports.py`'s `rule_based_day_summary()`) after a "can we do this without an LLM" request, then an AI path was reintroduced via Google Gemini, then self-hosted Ollama, then (final, current state) **Groq's hosted API** (`app/llm_summary.py`) — each swap driven by a real production bug or cost/data-policy concern, documented in detail in `CLAUDE.md`. Two live production bugs were fixed same-day on the Groq path: a deprecated default model ID (404s) and a reasoning-model token-budget issue (empty responses) — both fixed in `app/llm_summary.py`. The summary format was also changed from prose to a bulleted list, and (most recently) the bullet ordering was changed to lead with whichever project took the most time that day, in both the AI and rule-based paths.
- **Multilevel approval** for Leave, Overtime, and Overtime↔Missed-Hours match requests — new additive columns on three models, three new `*_lead_review` routes, a two-card admin UI split ("Awaiting Team Lead review" vs. "Pending requests"), and a shared visual step-progression tracker (`app/util.py`'s `approval_progress_steps()`, `app/templates/_macros.html`).
- **Department-scoped admin access to Leave/Overtime narrowed then partially reopened** — full lockout to Super-Admin-only (2026-08-28), then reopened as view-only for department-scoped admins (2026-08-30), both enforced server-side (route-level `Depends`), not just hidden in templates.
- **A significant, rapid series of UI/nav reorganizations** across the admin and employee zones (card-header restyling app-wide, admin nav reordered/relabeled into "My Work / Dashboard / Team Requests / Projects & Tasks / Employees / Analytics & Reports / Settings & Configuration / Support", the standalone "Profile" nav link removed in favor of clicking the user-chip, Roster regrouped by department, and many Today/My Month layout iterations) — see `CLAUDE.md`'s dated bullets for the very detailed blow-by-blow; not repeated here for length.
- **Most recent, same session as this handover doc**: a department multi-select dropdown was added to the "Add a project" form on the Projects & Tasks admin page, so a new project can be scoped to specific departments at creation time instead of always defaulting to "every department" (`app/routes/admin.py`'s `lists_add()`, `app/templates/admin/lists.html`). **This change is uncommitted** — see §9/§10.

## 12. Recommended next steps

In rough priority order:

1. **Locate the missing legacy data bundle and re-run `python -m legacy.verify_strikes`.** This is the project's own stated acceptance gate for any `engine.py`/`validation.py` change, and it has not run in weeks despite several such changes shipping in that window. Likely held by "Steve" (see the `steve` git remote, and `HANDOFF.md`'s framing of Steve as the original demo audience/data holder).
2. **Run the real `pytest tests/ -q` suite** on a working machine with the actual dependencies installed, and reconcile the result against the "468/469 on 2026-08-22" figure that predates roughly two more weeks of feature work. Fix or explain any new failures before further changes.
3. **Reconcile the deployment story.** Confirm with the project owner which of GCP Cloud Run (current evidence points here), Azure App Service (README still documents this), or Railway (the `Procfile` leftover) is actually serving production traffic today, then update README's "Path to production" section (and remove/relabel the unused deploy script(s)) to match reality — this project has an explicit, previously-stated convention of keeping README current with reality.
4. **Review and commit (or discard) the uncommitted working-tree changes** noted in §9/§10 before building anything else on top of them.
5. **Clarify the two-git-remote, three-local-branch situation** (`origin` vs. `steve`, and the `admin-sidebar-redesign`/`production-readiness-updates` branches) with the project owner — determine whether any of that work needs merging, is abandoned, or represents a parallel effort that should be coordinated with.
6. **Pin `requirements.txt`** (e.g. via `pip freeze` against a known-good, tested environment) to prevent a repeat of the Starlette-API-drift incident described in §10.
7. **Build route-level HTTP smoke tests** (`httpx.TestClient` — login, add a time entry, submit a day, load the dashboard, run an export, all asserting a 200) — named as a to-do since the original 2026-07-27 handoff and still not started.
8. **Decide on `AUTH_MODE=entra`** (Microsoft Entra ID SSO) — still the long-term target named throughout the codebase but with zero implementation. If the organization has since standardized on a different SSO provider, `app/auth.py`'s explicit "swap point" design should make implementing whichever provider was actually chosen straightforward.
9. **Reassess rate limiting and CSRF protection** now that production runs real self-signup passwords rather than a local dev login — the in-memory, single-instance rate limiter and the absence of CSRF tokens were both flagged as acceptable-for-now, revisit-later trade-offs in the code's own comments.
10. Lower priority, explicitly deferred by the project owner in the past: payslips through the app, further general UI polish, end-of-day reminder/strike-alert notifications, and "phase-2" anomaly detection (pre-shift work patterns, meta-work ratios).

## 13. Instructions for the next Claude

**Read `CLAUDE.md` in full before making any change of real substance.** It is an exhaustive, dated, first-person log of every decision this project has been through, including the *reasoning* behind each one and several deliberately-rejected alternatives — treating it as background noise rather than load-bearing context is the single most common way to accidentally re-break or re-litigate a settled decision in this codebase. `README.md` is the maintained, more skimmable feature/screens reference; keep it updated in the same batch as any feature or behavior change (this is an explicit, previously-stated project convention, not optional).

**Hard rules that must not be violated:**
- Python 3.9 syntax only — no `X | Y` runtime type unions.
- All durations/targets/variances are integer minutes; times-of-day are minutes since midnight. Never introduce a float into this math.
- Every real-world "now"/"today" the app captures goes through `app/util.py`'s `now_local()`/`today_local()` — never `dt.datetime.now()`/`dt.date.today()` directly. (`dt.datetime.utcnow()` remains correct, unchanged, for pure audit-timestamp fields.)
- Any change to `app/engine.py`, `app/validation.py`, or `legacy/import_legacy.py` requires running `pytest tests/ -q` **and** `python -m legacy.verify_strikes` (must print 168/168) before calling the work done. If neither can actually run in your environment (see §10's known sandbox/bundle-availability issues), say so explicitly to the user rather than silently treating `py_compile` or a hand-written re-implementation check as equivalent — those are documented, accepted *substitutes when nothing else is possible*, never a stated replacement for the real thing.
- Never hand-edit a `DayStatus` row where `source == 'imported'` — it's frozen legacy fact. Admins change history only via the override mechanism.
- Never hardcode a threshold that already has (or plausibly should have) a `Config` entry — read via `engine.get_config(db)`.
- Any new schema column with a meaningful (non-NULL-safe) default needs a dedicated `ensure_*_backfill()` function wired into `app/main.py`'s startup — the additive auto-migration never backfills existing rows on its own. This has been the single most-repeated gotcha in this project's history (see §10).
- Any new sensitive personal-data field (bank/ID numbers or similar) must follow the existing `mask_tail` + blank-means-unchanged pattern (§7) — never a plain text column shown in full.
- **Never delete a file in the connected project folder without asking the user first**, even confidently-dead code with zero remaining references — a plain file delete from an automated environment can succeed silently and irrecoverably in a way that other write operations (git, direct SQLite writes) do not. If a file becomes obsolete, leave it in place with a comment, or ask before removing it.
- **Never attempt a git write (commit/add/checkout/reset) or a direct SQLite write transaction from an automated/sandboxed environment against files in the real project folder** if you are running in an environment with known write-limitation issues (see §10) — these operations can appear to succeed while leaving a lock/journal file behind that corrupts the real, user-visible repository or database. Prefer writing a standalone script and asking the user to run it themselves, or verify first whether your specific environment is actually affected by this limitation before assuming it is.
- Keep `README.md` current in the same batch as any feature/behavior change — this project has an explicit prior instruction that this is not optional/deferrable documentation debt.

**How to verify a change safely:**
1. `python3 -m py_compile <every changed .py file>` — minimum bar, catches syntax errors.
2. If your environment can run a full `pytest` (dependencies installed, real Python 3.9+ interpreter, no sandbox restrictions) — run the full suite, and `legacy.verify_strikes` if the change touched `engine.py`/`validation.py`/`legacy/import_legacy.py` and you have access to the legacy data bundle.
3. If you cannot run a full `pytest` (broken `.venv`, no network to install dependencies, no legacy data bundle) — say so explicitly, then do the best available substitute: extract and directly execute the real changed function's source against hand-built test cases (not a hand-copied re-implementation, the actual source), and/or a Jinja `Environment(...).parse()` + `.render()` check with fake context objects for any changed template, checking the rendered HTML for tag balance. Always tell the user which of these you actually did and that a real `pytest`/`verify_strikes` run by them is still needed before the change is considered fully confirmed.
4. For template-only or CSS-only changes, a render+balance-check harness (as above) plus a visual sanity read of the diff is normally sufficient.

**Conventions to follow:**
- Feature flags (env var, default chosen deliberately, gated server-side in the route — never just hidden in the template) for any large or pay-adjacent feature, following the existing `TICKETING_ENABLED`/`HOLIDAY_MANAGEMENT_ENABLED`/`LEAVE_MANAGEMENT_V2_ENABLED`/`MULTILEVEL_APPROVAL_ENABLED` pattern.
- Shared JS widgets belong in `app/static/*.js` and are loaded globally via `base.html` once a second page needs the same interaction pattern (this has already happened twice — `combo.js` and `msdrop.js` both started as one page's inline script and were promoted to shared files) — don't copy-paste a widget's JS a third time.
- Write extensive, dated, first-person docstring/comment context explaining *why* a decision was made and what was considered and rejected — this is this project's established documentation style throughout `app/models.py`, `app/engine.py`, and every recent route change, and it is what makes `CLAUDE.md` and the codebase itself navigable for whoever picks this up next. Match it.
- When genuinely uncertain about scope or a design choice with real trade-offs, this project's established pattern is to ask a short, structured clarifying question before building (many `CLAUDE.md` bullets reference an explicit "AskUserQuestion" moment) rather than silently guessing — follow that pattern rather than assuming the more elaborate or more conservative option is automatically correct.
