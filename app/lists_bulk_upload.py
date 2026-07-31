"""Bulk-add Project/Employer and Task Type dropdown values via Excel
(Projects & Tasks -> Bulk upload).

Smaller sibling of app/bulk_upload.py and app/leave_bulk_upload.py, same
shape: plain parsing functions the routes call, only process_upload()
touches the database. Deliberately the simplest of the three sheets —
Project and TaskType are single-column models (just `name` + `active`), so
each sheet is one column, one value per row, add-only. There's no "update"
mode (nothing to update on a dropdown value beyond the name itself, which
this sheet never renames) and no deactivate-via-sheet (that stays a single
click on the Lists page, same as it already is) — this only ever adds new
values, exactly like typing into the "Add project/task" box on that page,
just many at once.

`kind` is "project" or "task" throughout, matching the same sentinel
already used by app/routes/admin.py's lists_add()/lists_toggle().
"""
from typing import List, Optional, Tuple

from openpyxl import Workbook
from openpyxl.styles import Font
from sqlalchemy import select

from app import models as m

MAX_ROWS = 500

HEADERS = {
    "project": "Project / Employer Name",
    "task": "Task Name",
}
SHEET_TITLES = {
    "project": "Projects",
    "task": "Tasks",
}
MODELS = {
    "project": m.Project,
    "task": m.TaskType,
}


def _model_for(kind: str):
    model = MODELS.get(kind)
    if model is None:
        raise ValueError(f"Unknown kind '{kind}' — must be 'project' or 'task'")
    return model


def read_upload_names(wb: Workbook, kind: str) -> Tuple[List[Tuple[int, str]], Optional[str]]:
    """Returns ([(row_number, name), ...], error). error is set (rows
    empty) only when the sheet itself is unusable (no header row, or the
    expected single column isn't present at all) — the row number is kept
    alongside each name so skipped-row messages can point back at the
    sheet, same as the other two bulk uploads."""
    header = HEADERS[kind]
    ws = wb.active
    header_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), None)
    if header_row is None or all(c is None or str(c).strip() == "" for c in header_row):
        return [], "The sheet is empty — no header row found."
    col_idx = None
    for idx, cell in enumerate(header_row):
        if cell is not None and str(cell).strip().lower() == header.lower():
            col_idx = idx
            break
    if col_idx is None:
        return [], f"Expected a column called '{header}' — use the sample template."

    out: List[Tuple[int, str]] = []
    for i, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if row is None or col_idx >= len(row):
            continue
        value = row[col_idx]
        name = str(value).strip() if value is not None else ""
        if not name:
            continue  # blank row/cell — skip silently, not an error
        out.append((i, name))
    return out, None


def process_upload(db, wb: Workbook, kind: str) -> dict:
    """Parses + applies an uploaded single-column workbook. New names are
    added and committed in one transaction; anything already on the list
    (case-sensitive exact match, same rule the single "Add" box on the
    Lists page already uses) or repeated within the file is skipped with a
    reason, never silently dropped or duplicated. Returns {"added": int,
    "skipped": [{"row": int, "name": str, "reason": str}],
    "header_error": str | None}."""
    model = _model_for(kind)
    rows, header_error = read_upload_names(wb, kind)
    if header_error:
        return {"added": 0, "skipped": [], "header_error": header_error}
    if len(rows) > MAX_ROWS:
        return {
            "added": 0, "skipped": [],
            "header_error": f"Sheet has {len(rows)} data rows — max is {MAX_ROWS} per upload. Split it into batches.",
        }

    existing_names = {name for (name,) in db.execute(select(model.name)).all()}
    seen_in_file = set()
    to_add: List[str] = []
    skipped = []

    for row_num, name in rows:
        if name in existing_names:
            skipped.append({"row": row_num, "name": name, "reason": "Already on the list — skipped, nothing changed"})
            continue
        if name in seen_in_file:
            skipped.append({"row": row_num, "name": name, "reason": "Duplicate row in this file — only added once"})
            continue
        seen_in_file.add(name)
        to_add.append(name)

    for name in to_add:
        db.add(model(name=name))
    if to_add:
        db.commit()
    return {"added": len(to_add), "skipped": skipped, "header_error": None}


def build_sample_workbook(kind: str) -> Workbook:
    header = HEADERS[kind]
    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_TITLES[kind]
    ws.append([header])
    for c in ws[1]:
        c.font = Font(bold=True)
    example = "Acme Corp." if kind == "project" else "File review"
    ws.append([example])
    ws.column_dimensions["A"].width = 42

    info = wb.create_sheet("Instructions")
    info.append(["Column", "Notes"])
    for c in info[1]:
        c.font = Font(bold=True)
    for row in [
        (header, f"One {'project/employer' if kind == 'project' else 'task type'} name per row."),
        ("", ""),
        ("Add-only — this sheet never renames or removes a value.", ""),
        ("A name already on the list is skipped, not duplicated.", ""),
        ("To deactivate a value, use the Deactivate button on the", ""),
        ("Projects & Tasks page instead — it's not done via this sheet.", ""),
    ]:
        info.append(row)
    info.column_dimensions["A"].width = 46
    info.column_dimensions["B"].width = 50
    return wb


def build_existing_workbook(db, kind: str) -> Workbook:
    """Every current value (active and inactive) for reference, so it's
    easy to see what's already on the list before uploading more."""
    model = _model_for(kind)
    header = HEADERS[kind]
    names = [
        n for (n,) in db.execute(
            select(model.name).where(model.active.is_(True)).order_by(model.name)
        ).all()
    ]
    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_TITLES[kind]
    ws.append([header])
    for c in ws[1]:
        c.font = Font(bold=True)
    for n in names:
        ws.append([n])
    ws.column_dimensions["A"].width = 42
    return wb
