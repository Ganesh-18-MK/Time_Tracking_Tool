"""Formatting + audit helpers shared by routes and templates."""
import datetime as dt
import json
from typing import Optional

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
