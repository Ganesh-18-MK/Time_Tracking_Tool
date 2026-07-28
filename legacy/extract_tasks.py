"""Extract task rows from a legacy Task Summary .ods into a JSONL cache.

Usage:
    python legacy/extract_tasks.py "<file.ods>" <out_prefix>

Writes <out_prefix>.jsonl (one task row per line) and <out_prefix>.meta.json
(sheet names, distinct projects/tasks, date range, oddities). Times are kept
as raw minutes-since-midnight exactly as recorded; the DB importer fixes the
12-hour-clock wrap problem (e.g. "12:00 -> 1:00" afternoons) monotonically.
"""
from __future__ import annotations

import json
import re
import sys
from ods_reader import iter_rows

ISO_DUR = re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?$")


def dur_minutes(v):
    """ODS time value 'PT7H30M0S' -> minutes since midnight (int) or None."""
    if not isinstance(v, str):
        return None
    m = ISO_DUR.match(v)
    if not m:
        return None
    h = int(m.group(1) or 0)
    mi = int(m.group(2) or 0)
    s = float(m.group(3) or 0)
    return h * 60 + mi + (1 if s >= 30 else 0)


def cell_str(c):
    if c.value is not None and isinstance(c.value, str):
        return c.value.strip()
    return (c.text or "").strip()


def main(path: str, out_prefix: str) -> None:
    tasks = []
    other_sheets: dict = {}
    header_cols: dict = {}
    oddities = []
    n_rows = 0
    task_sheet = None

    with open(out_prefix + ".jsonl", "w") as out:
        for sheet, idx, cells in iter_rows(path, max_cols=10):
            n_rows += 1
            if n_rows % 100000 == 0:
                print(f"...scanned {n_rows} rows", flush=True)
            if not cells:
                continue
            texts = [cell_str(c) for c in cells]
            # detect the task header row
            if "Date" in texts and "Task" in texts and sheet not in header_cols:
                header_cols[sheet] = {t: i for i, t in enumerate(texts) if t}
                task_sheet = sheet
                continue
            if sheet in header_cols:
                h = header_cols[sheet]
                date_c = cells[h.get("Date", 0)] if h.get("Date", 0) < len(cells) else None
                if date_c is None or date_c.value_type != "date":
                    nonempty = [t for t in texts if t]
                    if nonempty:
                        oddities.append({"sheet": sheet, "row": idx, "cells": nonempty[:6]})
                    continue

                def col(name):
                    i = h.get(name)
                    return cells[i] if i is not None and i < len(cells) else None

                start = col("Start Time")
                end = col("End time") or col("End Time")
                taken = col("Time taken")
                rec = {
                    "date": str(date_c.value)[:10],
                    "project": cell_str(col("Project/Employer")) if col("Project/Employer") else "",
                    "task": cell_str(col("Task")) if col("Task") else "",
                    "details": cell_str(col("Details")) if col("Details") else "",
                    "start_min": dur_minutes(start.value) if start else None,
                    "end_min": dur_minutes(end.value) if end else None,
                    "taken_min": dur_minutes(taken.value) if taken else None,
                    "row": idx,
                }
                tasks.append((rec["date"], rec["project"], rec["task"]))
                out.write(json.dumps(rec) + "\n")
            else:
                # non-task sheet (e.g. the hidden dropdown List sheet)
                if any(texts):
                    other_sheets.setdefault(sheet, [])
                    if len(other_sheets[sheet]) < 300:
                        other_sheets[sheet].append([t for t in texts if t])

    meta = {
        "task_sheet": task_sheet,
        "n_task_rows": len(tasks),
        "date_min": min((t[0] for t in tasks), default=None),
        "date_max": max((t[0] for t in tasks), default=None),
        "distinct_projects": sorted({t[1] for t in tasks if t[1]}),
        "distinct_tasks": sorted({t[2] for t in tasks if t[2]}),
        "other_sheets": other_sheets,
        "oddities": oddities[:200],
        "n_oddities": len(oddities),
    }
    with open(out_prefix + ".meta.json", "w") as f:
        json.dump(meta, f, indent=1)
    print(f"DONE: {len(tasks)} task rows; sheets seen incl. {list(other_sheets)}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
