# CLAUDE.md — MK Timekeeping & Compliance POC

FastAPI + SQLAlchemy + Jinja2 internal tool replacing three legacy spreadsheets for
time/leave/compliance tracking (~45 offshore staff). Spec: docs/PRD.md. Onboarding: HANDOFF.md.

## Commands

```bash
.venv/bin/python -m uvicorn app.main:app --port 8127     # run (http://localhost:8127, dev pick-a-user auth)
.venv/bin/python -m pytest tests/ -q                     # 35 tests — must stay green
.venv/bin/python -m legacy.verify_strikes                # acceptance: MUST print 168/168
rm tms.db && .venv/bin/python -m legacy.import_legacy    # rebuild DB from the 3 legacy .ods files
```

## Hard rules

- **The project directory name ends with a trailing space** — always quote absolute paths.
- Python 3.9 (system). No `X | Y` unions in runtime annotations; use `Optional[...]`.
- All durations/targets/variances are **integer minutes**; times-of-day are minutes since midnight. No floats, no timezones. Format via Jinja filters `hm`/`hm_signed`/`clock`.
- `DayStatus.source='imported'` rows are **frozen legacy fact** — never recompute, migrate, or "fix" them (raw sheet cell is in `imported_token`). Admins change history via overrides only.
- `strike_exempt=True` rows (pre-policy days before 2026-04-15) must never count as strikes — the legacy sheets' own formulas excluded them. Changing this breaks `verify_strikes`.
- Status precedence: override → compensation (`comp_erases_strike` config) → base. Implemented in `DayStatus.effective_status()` and `engine.strikes_in()` — change both or neither.
- Any change to `app/engine.py`, `app/validation.py`, or `legacy/import_legacy.py` ⇒ run pytest **and** verify_strikes before calling it done.
- Schema changes: no alembic in the POC — `rm tms.db` + re-import is the migration path.
- Config lives in the `config` table (defaults: `CONFIG_DEFAULTS` in app/models.py); read via `engine.get_config(db)`, never hardcode thresholds.

## Layout

- `app/engine.py` — all business math (statuses, variance, strikes, recompute, ledger)
- `app/validation.py` — PRD §4 entry rules (overlaps block, gaps flag, 4h cap, backdate window)
- `app/routes/` — auth (dev login), employee (Today/My Month), admin (7 screens), exports (XLSX/CSV)
- `app/auth.py` — the Entra ID swap point; session/role gating stays
- `legacy/` — streaming ODS reader (survives the 700 MB file), extractor, importer, strike verifier
- `legacy/cache/import_report.json` — every oddity the import tolerated
