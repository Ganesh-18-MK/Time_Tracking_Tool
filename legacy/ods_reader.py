"""Streaming reader for OpenDocument Spreadsheet (.ods) files.

Stdlib only. Built to survive the degraded legacy files this app replaces:
one Task Summary file is a 20 MB .ods whose content.xml decompresses to
700 MB with 16,331 empty repeated columns per row. We therefore:

  * stream with xml.etree.ElementTree.iterparse (never load the tree),
  * cap column expansion (repeated empty cells beyond max_cols are dropped),
  * cap emission of repeated empty rows while keeping true row indices,
  * aggressively clear processed elements to keep memory flat.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from typing import Iterator, List, Optional, Sequence

NS_OFFICE = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"
NS_TABLE = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
NS_TEXT = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"

TABLE_TABLE = f"{{{NS_TABLE}}}table"
TABLE_ROW = f"{{{NS_TABLE}}}table-row"
TABLE_CELL = f"{{{NS_TABLE}}}table-cell"
COVERED_CELL = f"{{{NS_TABLE}}}covered-table-cell"
ATTR_NAME = f"{{{NS_TABLE}}}name"
ATTR_COLS_REPEATED = f"{{{NS_TABLE}}}number-columns-repeated"
ATTR_ROWS_REPEATED = f"{{{NS_TABLE}}}number-rows-repeated"
ATTR_FORMULA = f"{{{NS_TABLE}}}formula"
ATTR_VALUE_TYPE = f"{{{NS_OFFICE}}}value-type"
ATTR_VALUE = f"{{{NS_OFFICE}}}value"
ATTR_DATE_VALUE = f"{{{NS_OFFICE}}}date-value"
ATTR_TIME_VALUE = f"{{{NS_OFFICE}}}time-value"
ATTR_BOOL_VALUE = f"{{{NS_OFFICE}}}boolean-value"
ATTR_STRING_VALUE = f"{{{NS_OFFICE}}}string-value"


@dataclass
class Cell:
    """One spreadsheet cell. `value` is typed when ODS provides a typed value;
    `text` is the display string; `formula` is the stored formula if any."""

    value: object = None          # float | str | bool | None (dates/times as ISO strings)
    value_type: Optional[str] = None  # float, string, date, time, boolean, percentage, currency
    text: str = ""
    formula: Optional[str] = None

    @property
    def is_empty(self) -> bool:
        return self.value is None and not self.text

    def __repr__(self) -> str:  # compact, for structure dumps
        if self.is_empty:
            return "·"
        v = self.value if self.value is not None else self.text
        return f"{self.value_type or 't'}:{v!r}"


EMPTY = Cell()


def _cell_from_elem(elem: ET.Element) -> Cell:
    vt = elem.get(ATTR_VALUE_TYPE)
    value: object = None
    if vt in ("float", "percentage", "currency"):
        raw = elem.get(ATTR_VALUE)
        if raw is not None:
            try:
                value = float(raw)
            except ValueError:
                value = raw
    elif vt == "date":
        value = elem.get(ATTR_DATE_VALUE)
    elif vt == "time":
        value = elem.get(ATTR_TIME_VALUE)
    elif vt == "boolean":
        value = elem.get(ATTR_BOOL_VALUE) == "true"
    elif vt == "string":
        value = elem.get(ATTR_STRING_VALUE)  # may be None; fall back to text

    text = "\n".join(
        "".join(p.itertext()) for p in elem.findall(f"{{{NS_TEXT}}}p")
    ).strip()
    if value is None and vt == "string":
        value = text or None
    return Cell(value=value, value_type=vt, text=text, formula=elem.get(ATTR_FORMULA))


def sheet_names(path: str) -> List[str]:
    """Names of all sheets, streaming (cheap even on huge files)."""
    names: List[str] = []
    with zipfile.ZipFile(path) as zf, zf.open("content.xml") as fh:
        for event, elem in ET.iterparse(fh, events=("start",)):
            if elem.tag == TABLE_TABLE:
                names.append(elem.get(ATTR_NAME, ""))
            elem.clear()
    return names


def iter_rows(
    path: str,
    sheets: Optional[Sequence[str]] = None,
    max_cols: int = 64,
    max_empty_row_repeat: int = 3,
    max_rows_per_sheet: Optional[int] = None,
) -> Iterator[tuple]:
    """Yield (sheet_name, row_index, cells) for each row, 0-based row_index.

    * Repeated cells expand up to max_cols; repeated empties beyond that drop.
    * A fully-empty row repeated N times emits at most max_empty_row_repeat
      copies but advances row_index by N, so indices always match the grid.
    * Rows longer than max_cols are truncated (legacy junk columns).
    """
    wanted = set(sheets) if sheets is not None else None
    with zipfile.ZipFile(path) as zf, zf.open("content.xml") as fh:
        context = ET.iterparse(fh, events=("start", "end"))
        current_sheet: Optional[str] = None
        table_elem: Optional[ET.Element] = None
        skip_sheet = False
        row_index = 0
        emitted = 0
        for event, elem in context:
            if event == "start":
                if elem.tag == TABLE_TABLE:
                    current_sheet = elem.get(ATTR_NAME, "")
                    table_elem = elem
                    skip_sheet = wanted is not None and current_sheet not in wanted
                    row_index = 0
                    emitted = 0
                continue
            # end events
            if elem.tag == TABLE_ROW:
                repeat = int(elem.get(ATTR_ROWS_REPEATED, "1"))
                if not skip_sheet and (
                    max_rows_per_sheet is None or emitted < max_rows_per_sheet
                ):
                    cells: List[Cell] = []
                    for child in elem:
                        if child.tag not in (TABLE_CELL, COVERED_CELL):
                            continue
                        crep = int(child.get(ATTR_COLS_REPEATED, "1"))
                        cell = (
                            EMPTY if child.tag == COVERED_CELL else _cell_from_elem(child)
                        )
                        if cell.is_empty:
                            room = max_cols - len(cells)
                            if room > 0:
                                cells.extend([EMPTY] * min(crep, room))
                        else:
                            for _ in range(min(crep, max(0, max_cols - len(cells)))):
                                cells.append(cell)
                        if len(cells) >= max_cols:
                            # keep parsing nothing further; grid junk beyond cap
                            break
                    while cells and cells[-1].is_empty:
                        cells.pop()
                    n_emit = repeat
                    if not cells:  # fully empty row
                        n_emit = min(repeat, max_empty_row_repeat)
                    for k in range(n_emit):
                        if max_rows_per_sheet is not None and emitted >= max_rows_per_sheet:
                            break
                        yield current_sheet, row_index + k, cells
                        emitted += 1
                row_index += repeat
                elem.clear()
                if table_elem is not None:
                    # drop processed rows from the open table element so the
                    # 700 MB files never accumulate a tree in memory
                    try:
                        del table_elem[:]
                    except Exception:
                        table_elem.clear()
            elif elem.tag == TABLE_TABLE:
                elem.clear()
                table_elem = None
                current_sheet = None


def read_sheet(path: str, sheet: str, max_cols: int = 64,
               max_rows: Optional[int] = None) -> List[List[Cell]]:
    """Materialize one sheet as a dense list of rows (small sheets only)."""
    rows: List[List[Cell]] = []
    for name, idx, cells in iter_rows(
        path, sheets=[sheet], max_cols=max_cols, max_rows_per_sheet=max_rows
    ):
        while len(rows) <= idx:
            rows.append([])
        rows[idx] = cells
        if max_rows is not None and len(rows) >= max_rows:
            break
    return rows
