# Handoff — MK Timekeeping & Compliance POC

*Written 27 Jul 2026, the day the POC was built and verified. Audience: the developer picking this up.*

## 30-second start

The bundle ships with a **pre-seeded `tms.db`** — you don't need the import step to see it working:

```bash
cd "<project dir>"        # NOTE: the original dir name ends with a trailing space
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn app.main:app --port 8127
```

http://localhost:8127 → sign in as **Steve** (admin) or any employee. Python 3.9+ is enough; no Node, no build step, no external services.

No bundle? The repo alone still demos: `python -m demo.run_demo` serves the committed **anonymized** database on port 8128 (fictional names/clients, real structure — safe for any audience). Admin login: Dana Whitmore.

## Current state — honest summary

| Area | State |
|---|---|
| PRD coverage | Everything in §4–§8 and all nine §7 screens. §10's nine open questions are config dials/roster fields. |
| Acceptance (§12.3) | **168/168** person-months of historical strikes reproduce the legacy sheets. `python -m legacy.verify_strikes` re-checks in ~2 s. Keep it green. |
| Tests | 35 passing (`pytest tests/ -q`) — engine rules and entry validation. No route-level tests yet (see backlog). |
| Data | Real data imported from the three legacy files: 60 people, 3,694 frozen day-statuses, 473 leave records, 297 projects, 35 task types, 492 historical task entries. |
| Auth | Dev mode (pick-a-user). Entra ID is *not* wired — it's the top backlog item for any real rollout. |
| DB | SQLite file `tms.db`. Fine for demo/single-admin use; switch to Postgres before ~45 people submit concurrently. |
| Deploy | Runs locally only. Azure App Service is the target (PRD §9); nothing Azure-specific exists yet. |

## The five concepts you must understand (10 minutes)

1. **Minutes, everywhere.** All durations/targets/variances are integer minutes; times-of-day are minutes since midnight. No floats. No *per-employee* timezones — every clock-face value is captured in one fixed reference timezone (`app/util.py`'s `BUSINESS_TZ` = America/Chicago, the firm's home timezone, via `now_local()`/`today_local()`), regardless of the server's own OS clock or wherever an employee physically is (2026-08-10, after an offshore employee's auto-timer showed the wrong start time — the server container defaults to UTC). Formatting is Jinja filters `hm` / `hm_signed` / `clock`.

2. **Frozen history vs live computation.** `DayStatus.source='imported'` rows are legacy fact — raw sheet token kept in `imported_token`, never recomputed. Live rows (`source='computed'`) rebuild freely from entries+leave, but only from `live_start_date` (config) onward. This split is why the acceptance test stays stable while the app keeps computing new days. **Never hand-edit imported rows;** admins use overrides instead.

3. **Status precedence** (in `DayStatus.effective_status()` + `engine.strikes_in()`):
   `override` (reason mandatory, audited, ⚑) → `compensated` (fully-covered shortfall reads Complete if `comp_erases_strike`) → base status. `strike_exempt` rows (legacy pre-policy, see below) never count as strikes regardless of status.

4. **The April quirk.** The compliance policy took effect mid-April 2026; the sheet's own `STRIKES` formulas start at **column R = Apr 15**. The importer parses each row's COUNTIF range and marks earlier N/PARTIAL days `strike_exempt`. If you ever "fix" that flag, 13 person-months stop reproducing and `verify_strikes` fails — it's a feature, not a bug.

5. **Recompute triggers.** There's no cron. `engine.recompute_employee/all` runs on dashboard/My Month/person-page load and after submit/leave/override/comp-link changes. At 45 people × 31 days this is milliseconds. A "Recompute month" button exists on the dashboard for peace of mind.

## Where things live

```
app/engine.py        ← all business math. Touch with tests + verify_strikes.
app/validation.py    ← PRD §4 entry rules. Client JS mirrors it; server is authoritative.
app/models.py        ← schema + CONFIG_DEFAULTS. Schema change = rm tms.db + re-import (no alembic in POC).
app/auth.py          ← THE swap point for Entra ID. Session/role gating stays as-is.
app/routes/          ← thin controllers; admin.py has most of them.
app/templates/       ← server-rendered Jinja; only JS is the duration preview on Today.
legacy/              ← ODS streaming reader, extractor, importer, verifier. Self-contained.
legacy/cache/import_report.json ← every oddity the import tolerated. Skim it once.
docs/PRD.md          ← the spec. HANDOFF (this file) + README are the map.
```

## Known rough edges (deliberate POC trade-offs)

- **185 legacy task rows** (of 677) had no usable start/end times → skipped, listed in the import report (PRD §11 says best-effort).
- Imported weekday rows show actual `—` (the sheet tokens carry no hours); their variance comes from the tracker's extra/short blocks where available, else `—` and the running balance simply doesn't move that day.
- Two part-timers' daily targets were only hinted at in the sheets (`Part Time` dept, free-text "4 Hours 19 min") — set real targets in Roster → Edit when HR confirms.
- `unlock_log` from PRD §8 is realized as audit-log entries + an `unlock_count` on the submission (simpler, same information).
- Today counts as Partial the moment someone submits a short day (Srividhya demo). If the team dislikes intraday strike movement, gate `strikes_in` on `date < today` — one line in `engine.py`.
- No CSRF tokens / rate limiting — fine behind dev auth on localhost, revisit with Entra.

## Suggested next steps, in order

| # | Task | Size | Notes |
|---|---|---|---|
| 1 | Entra ID sign-in | M | MSAL auth-code flow in `app/routes/auth.py`; map tenant email → `Employee.email` (populate emails in Roster first); `AUTH_MODE=entra`, real `SECRET_KEY`. |
| 2 | Postgres + Azure App Service | M | `DATABASE_URL` switch + `psycopg[binary]`; App Service in the current tenant; re-run importer against final file copies at cutover; freeze sheets read-only. |
| 3 | Route-level smoke tests | S | `httpx.TestClient` — login, add entry, submit, dashboard 200, export 200. The export check in this repo's history is a good template. |
| 4 | End-of-day reminder + strike alert notifications | S–M | PRD fast-follow. All the data is in `DayStatus`; needs only a mail hook. |
| 5 | Team-lead read-only view | S | Departments + TEAM LEAD designations already imported; it's a filtered dashboard route + role flag. |
| 6 | Employee leave self-service (open Q5) | M | LeaveRecord + a `status=requested/approved` column + two buttons on My Month. |
| 7 | Import remaining Task Summary files | S each | `legacy/extract_tasks.py "<file>.ods" legacy/cache/<name>` then extend the Divya block in `import_legacy.py` into a loop over caches. |
| 8 | Phase-2 anomaly detection | L | Pre-shift work, meta-work ratios, unauthorized projects — the structured data now makes this trivial to query. |

## Demo script (5 minutes, works right now)

1. Sign in as **Steve** → Dashboard: July 2026 rebuilt live, department groups, strike totals, violation flags. Hover any cell — the raw legacy token is in the tooltip. Scroll right for STRIKES.
2. Click a person (Divya's page shows the richest data) → running balance, ledger, legacy leave notes, her imported task log with locked submissions.
3. Compensation: her Jul 24 shortfall is linked to six +0:30 surplus days → day reads **Complete ↺**, strike erased, original PARTIAL retained in notes.
4. Sign out → sign in as any employee → **Today**: add a row (dropdowns are the real client list), watch duration compute; try an overlapping row — blocked with the exact reason; leave a >15 min gap — flagged, not blocked. **Submit Day** → locked.
5. **My Month** as that employee: live strike count and variance balance — the transparency employees never had.
6. Back as Steve → **Export XLSX** on the dashboard: the legacy sheet layout, generated from the database.

## If you work on this with Claude Code

Open the project folder and it will pick up `CLAUDE.md` (commands + the same gotchas, condensed). The PRD is in `docs/PRD.md`; point at specific sections when asking for changes, and always finish with `pytest` + `verify_strikes`.
