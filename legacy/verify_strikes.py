"""Acceptance test (PRD §12.3): recomputed March–July 2026 strike counts must
match the legacy sheet's own STRIKES FOR MONTH values.

    .venv/bin/python -m legacy.verify_strikes
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from app import engine, models as m  # noqa: E402
from app.db import SessionLocal  # noqa: E402

REPORT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "legacy", "cache", "import_report.json",
)


def main() -> int:
    data = json.load(open(REPORT_PATH))
    sheet_strikes = data["sheet_strikes"]
    db = SessionLocal()
    cfg = engine.get_config(db)

    emps = {e.name: e for e in db.execute(select(m.Employee)).scalars()}
    total = matched = 0
    mismatches = []
    for name, months in sheet_strikes.items():
        emp = emps.get(name)
        if emp is None:
            mismatches.append((name, "-", "employee missing", "-"))
            continue
        for ym, sheet_val in months.items():
            year, month = int(ym[:4]), int(ym[5:7])
            ours = engine.strikes_for_month(db, emp.id, year, month, cfg)
            total += 1
            if ours == int(sheet_val):
                matched += 1
            else:
                mismatches.append((name, ym, int(sheet_val), ours))

    print(f"STRIKE REPRODUCTION: {matched}/{total} person-months match the sheet")
    if mismatches:
        print(f"\n{'person':28} {'month':8} {'sheet':>6} {'app':>5}")
        for name, ym, sv, ov in mismatches:
            print(f"{name:28} {ym:8} {sv!s:>6} {ov!s:>5}")
    return 0 if not mismatches else 1


if __name__ == "__main__":
    sys.exit(main())
