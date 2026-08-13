# CLAUDE.md — MK Timekeeping & Compliance POC

FastAPI + SQLAlchemy + Jinja2 internal tool replacing three legacy spreadsheets for
time/leave/compliance tracking (~45 staff, originally all India-based offshore, now also
US-based). Spec: docs/PRD.md. Onboarding: HANDOFF.md.

## Commands

```bash
.venv/bin/python -m uvicorn app.main:app --port 8127     # run (http://localhost:8127, dev pick-a-user auth)
.venv/bin/python -m pytest tests/ -q                     # 369 tests — must stay green
.venv/bin/python -m legacy.verify_strikes                # acceptance: MUST print 168/168
rm tms.db && .venv/bin/python -m legacy.import_legacy    # rebuild DB from the 3 legacy .ods files
```

## Hard rules

- **The project directory name ends with a trailing space** — always quote absolute paths.
- Python 3.9 (system). No `X | Y` unions in runtime annotations; use `Optional[...]`.
- All durations/targets/variances are **integer minutes**; times-of-day are minutes since midnight. No floats. Format via Jinja filters `hm`/`hm_signed`/`clock`.
- **One fixed reference timezone, not per-employee ones** (manager request, 2026-08-10): every `start_minute`/`end_minute`/"today" the app captures from the real world is expressed in `app/util.py`'s `BUSINESS_TZ` (`America/Chicago`, handles CST/CDT automatically) — never the server container's own OS clock (Cloud Run defaults to UTC) and never wherever an employee physically is. Always capture "now"/"today" via `util.now_local()`/`util.today_local()` — never call `dt.datetime.now()`/`dt.date.today()` directly anywhere a clock-face value or business day is being determined. (`dt.datetime.utcnow()` is still correct and unchanged for audit-trail timestamps like `reviewed_at`/`updated_at`/`started_at` — those are pure elapsed-time/audit values, not clock-face-of-day, so they stay in UTC.)
- Every human-read date/timestamp is **MM/DD/YYYY** (manager request, 2026-08-03) via the `mdy`/`mdy_dt` Jinja filters or `app/util.py`'s `fmt_date`/`fmt_datetime` — never a raw `.isoformat()`/`.strftime()` in a template or export. Exception: `<input type="date">` values, hidden form fields, and option/checkbox submit values stay ISO (`YYYY-MM-DD`) — that's the HTML date-input spec and what `parse_date_field`/`dt.date.fromisoformat` expect back, not something a human reads.
- `DayStatus.source='imported'` rows are **frozen legacy fact** — never recompute, migrate, or "fix" them (raw sheet cell is in `imported_token`). Admins change history via overrides only.
- `strike_exempt=True` rows (pre-policy days before 2026-04-15) must never count as strikes — the legacy sheets' own formulas excluded them. Changing this breaks `verify_strikes`.
- Status precedence: override → compensation (`comp_erases_strike` config) → base. Implemented in `DayStatus.effective_status()` and `engine.strikes_in()` — change both or neither.
- Any change to `app/engine.py`, `app/validation.py`, or `legacy/import_legacy.py` ⇒ run pytest **and** verify_strikes before calling it done.
- Schema changes: no alembic in the POC — `rm tms.db` + re-import is the migration path.
- Config lives in the `config` table (defaults: `CONFIG_DEFAULTS` in app/models.py); read via `engine.get_config(db)`, never hardcode thresholds.
- **Holidays are one shared company-wide list, not per-country** (manager request, 2026-08-14 — reverted a brief 2026-08-12 per-country split; holidays apply to every employee regardless of location). `engine.holidays_set(db, location=None)` always returns every Holiday row unscoped — `location` is accepted for backward compatibility but ignored, so it's fine (and preferred going forward) to just call `holidays_set(db)`. `Employee.location` and `Holiday.location` still exist as columns (see Holiday's docstring in app/models.py for why — dropping them isn't an additive schema change, and this app has real production data now) but nothing reads `Holiday.location` to filter anything anymore; new Holiday rows just get `DEFAULT_LOCATION` stamped on invisibly. Don't reintroduce per-employee holiday scoping without checking with Ganesh first — it was tried once and reverted within days.
- **Holiday Management is behind `HOLIDAY_MANAGEMENT_ENABLED`** (env var, `app/templating.py`, defaults **on**). Briefly defaulted off for the 2026-08-13 deploy so My Month's read-only lookback and a button-highlight fix could ship first; back to on the same day once that landed. Guarded server-side either direction, not just hidden from nav: `holidays_page`/`update_location` in `app/routes/employee.py` and all six `holiday_*` routes in `app/routes/admin.py` 404 while off. Roster add/edit pass `None` instead of the submitted `location` to `_emp_from_form` while off, so editing an employee for an unrelated reason never silently resets their country back to the default. This flag also still gates the Roster/Profile Country fields even though they're no longer wired to Holidays at all (kept as general employee metadata). Set `HOLIDAY_MANAGEMENT_ENABLED=0` on a host to take it dark again without a code change.

## Layout

- `app/util.py` — formatting/audit helpers shared by routes and templates; also the one place `now_local()`/`today_local()`/`BUSINESS_TZ` live (see Hard rules above)
- `app/engine.py` — all business math (statuses, variance, strikes, recompute, ledger)
- `app/validation.py` — PRD §4 entry rules (overlaps block, gaps flag, 4h cap, backdate window)
- `app/routes/` — auth (dev login), employee (Today/My Month/Leave/Overtime/Holidays/Support/Profile), admin (dashboard/roster/lists/leave/overtime/holidays/config/audit/support), tickets (Ticketing System — raise/list/detail/comment/status-change), exports (XLSX/CSV)
- `app/holiday_bulk_upload.py` — Excel upload for Holiday rows (name/date only — one shared calendar), same shape as `app/leave_bulk_upload.py`
- `app/auth.py` — the Entra ID swap point; session/role gating stays
- `legacy/` — streaming ODS reader (survives the 700 MB file), extractor, importer, strike verifier
- `legacy/cache/import_report.json` — every oddity the import tolerated
