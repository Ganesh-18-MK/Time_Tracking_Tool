"""Formatting + audit helpers shared by routes and templates."""
import datetime as dt
import io
import json
import re
from typing import Optional

from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models as m


def fmt_hm(minutes: Optional[int]) -> str:
    """480 -> '8:00', 259 -> '4:19'. None -> em dash."""
    if minutes is None:
        return "—"
    sign = "-" if minutes < 0 else ""
    minutes = abs(int(minutes))
    return f"{sign}{minutes // 60}:{minutes % 60:02d}"


def fmt_hm_signed(minutes: Optional[int]) -> str:
    if minutes is None:
        return "—"
    if minutes == 0:
        return "0:00"
    return ("+" if minutes > 0 else "-") + fmt_hm(abs(minutes))


def fmt_time(minute: Optional[int]) -> str:
    """Minutes-since-midnight -> '9:30 AM'."""
    if minute is None:
        return "—"
    h, mi = divmod(int(minute), 60)
    suffix = "AM" if h < 12 or h == 24 else "PM"
    display_h = h % 12 or 12
    return f"{display_h}:{mi:02d} {suffix}"


def parse_hhmm(value: str) -> int:
    """'09:30' (from <input type=time>) -> minutes since midnight."""
    parts = value.strip().split(":")
    return int(parts[0]) * 60 + int(parts[1])


def clamp_break_end(start_minute: int, end_minute: int) -> int:
    """A break's end-of-day clamp: an end time numerically *before* its
    start means the break ran past midnight (e.g. started 23:58, ended
    00:02) — clamp to end of day, same no-rows-past-midnight convention
    used elsewhere. Equal minutes (started and ended within the same clock
    minute — a real, valid ~0-minute break) must NOT hit this clamp; a
    previous off-by-one (<=) here turned a same-minute break into a
    fabricated multi-hour one."""
    return 1440 if end_minute < start_minute else end_minute


# ---- admin form parsing -----------------------------------------------------
# Every admin POST route below eventually calls dt.date.fromisoformat/int/float
# on raw Form(...) strings. Route through these so a fat-fingered field flashes
# a message instead of a raw 500.
class FormError(Exception):
    """A form field failed to parse. Routes catch this and flash a
    user-readable message instead of letting the request 500."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def parse_date_field(value: str, label: str = "Date") -> dt.date:
    try:
        return dt.date.fromisoformat((value or "").strip())
    except (ValueError, TypeError):
        raise FormError(f"{label} must be a valid date (YYYY-MM-DD).")


def parse_int_field(value, label: str) -> int:
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        raise FormError(f"{label} must be a whole number.")


def parse_hours_field(value, label: str) -> int:
    """'8' / '1.5' -> minutes. Used for target/tolerance/row-length dials."""
    try:
        return int(round(float(value) * 60))
    except (ValueError, TypeError):
        raise FormError(f"{label} must be a number.")


# ---- employee ID generation --------------------------------------------
# "LOMK001", "LOMK002", ... — monotonic and never reused (based on the
# highest number ever assigned, including deactivated employees), so a
# departed employee's old ID is never handed to someone new.
EMPLOYEE_CODE_PREFIX = "LOMK"
_CODE_RE = re.compile(rf"^{EMPLOYEE_CODE_PREFIX}(\d+)$")


def format_employee_code(n: int) -> str:
    return f"{EMPLOYEE_CODE_PREFIX}{n:03d}"


def highest_employee_code_number(db: Session) -> int:
    best = 0
    for (code,) in db.execute(select(m.Employee.employee_code)).all():
        if not code:
            continue
        match = _CODE_RE.match(code)
        if match:
            best = max(best, int(match.group(1)))
    return best


def next_employee_code(db: Session) -> str:
    """For single-row creation (Roster -> Add person). Bulk upload assigns
    a block of codes itself instead of re-querying per row — see
    app/bulk_upload.py."""
    return format_employee_code(highest_employee_code_number(db) + 1)


def ensure_employee_codes(db: Session) -> None:
    """Backfill employee_code for rows created before this column existed.
    Assigns in id order (i.e. original creation order) so codes stay stable
    across repeated runs. A no-op once every row already has one — safe to
    call on every app startup (see app/main.py)."""
    missing = list(
        db.execute(
            select(m.Employee).where(m.Employee.employee_code.is_(None)).order_by(m.Employee.id)
        ).scalars()
    )
    if not missing:
        return
    n = highest_employee_code_number(db) + 1
    for emp in missing:
        emp.employee_code = format_employee_code(n)
        n += 1
    db.commit()


def xlsx_response(wb: Workbook, filename: str) -> StreamingResponse:
    """Shared by every route that streams an openpyxl Workbook back as a
    download (exports.py, bulk_upload's templates, reports.py) so the
    buf/save/seek/StreamingResponse boilerplate exists in exactly one place."""
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def fmt_hours(minutes: Optional[int]) -> str:
    """480 -> '8.0h' for compact numeric display."""
    if minutes is None:
        return "—"
    return f"{minutes / 60:.1f}h"


def month_label(year: int, month: int) -> str:
    return dt.date(year, month, 1).strftime("%B %Y")


def prev_next_month(year: int, month: int):
    first = dt.date(year, month, 1)
    prev_last = first - dt.timedelta(days=1)
    nxt = (first.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    return (prev_last.year, prev_last.month), (nxt.year, nxt.month)


def parse_ym(ym: Optional[str], default: Optional[dt.date] = None):
    d = default or dt.date.today()
    if ym:
        try:
            y, mo = ym.split("-")
            return int(y), int(mo)
        except ValueError:
            pass
    return d.year, d.month


def audit(
    db: Session,
    actor: str,
    action: str,
    entity: str = "",
    entity_id: str = "",
    detail: Optional[dict] = None,
) -> None:
    db.add(
        m.AuditLog(
            actor=actor,
            action=action,
            entity=entity,
            entity_id=str(entity_id),
            detail=json.dumps(detail or {}, default=str),
        )
    )
    db.commit()


STATUS_LABELS = {
    m.COMPLETE: "Y",
    m.PARTIAL: "PARTIAL",
    m.MISSING: "N",
    m.LEAVE: "LEAVE",
    m.HOLIDAY: "HOL",
    m.WEEKEND: "",
}

STATUS_NAMES = {
    m.COMPLETE: "Complete",
    m.PARTIAL: "Partial",
    m.MISSING: "Missing",
    m.LEAVE: "Leave",
    m.HOLIDAY: "Holiday",
    m.WEEKEND: "Weekend",
}
