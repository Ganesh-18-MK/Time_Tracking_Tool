"""Admin -> Reports: Attendance Reports and Strike Reports.

Both pages share the same cascading filter bar (Department -> Employee ->
Date range) and the same drill-down rule (see app/reports.py). This file is
the thin controller layer — HTML pages plus matching .xlsx exports — per
the layout convention in CLAUDE.md; all the actual aggregation lives in
app/reports.py.
"""
import datetime as dt
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from openpyxl import Workbook
from openpyxl.styles import Font
from sqlalchemy.orm import Session

from app import models as m, reports
from app.auth import require_admin
from app.db import get_db
from app.templating import render
from app.util import xlsx_response

router = APIRouter(prefix="/admin/reports")


def _header(ws, cols):
    ws.append(cols)
    for c in ws[1]:
        c.font = Font(bold=True)


def _parse_date(value: str) -> Optional[dt.date]:
    try:
        return dt.date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _filter_ctx(db: Session, dept: str, emp: int, rng: str, start: str, end: str) -> dict:
    dept = dept or ""
    start_date, end_date = reports.resolve_date_range(rng, _parse_date(start), _parse_date(end))
    return {
        "dept": dept, "emp": emp, "range": rng, "start": start or "", "end": end or "",
        "resolved_start": start_date, "resolved_end": end_date,
        "departments": reports.departments_list(db),
        "employees": reports.employees_list(db, dept or None),
        "range_presets": reports.RANGE_PRESETS,
    }


@router.get("")
def reports_landing(
    request: Request,
    admin: m.Employee = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return render(request, "admin/reports_landing.html", {"user": admin}, db=db)


# ---- Attendance Reports -----------------------------------------------------
@router.get("/attendance")
def reports_attendance(
    request: Request,
    dept: str = "",
    emp: int = 0,
    rng: str = Query(reports.DEFAULT_RANGE, alias="range"),
    start: str = "",
    end: str = "",
    admin: m.Employee = Depends(require_admin),
    db: Session = Depends(get_db),
):
    ctx = _filter_ctx(db, dept, emp, rng, start, end)
    result = reports.attendance_report(
        db, ctx["resolved_start"], ctx["resolved_end"], department=dept or None, employee_id=emp or None
    )
    return render(request, "admin/reports_attendance.html", {"user": admin, "result": result, **ctx}, db=db)


@router.get("/attendance.xlsx")
def reports_attendance_xlsx(
    dept: str = "",
    emp: int = 0,
    rng: str = Query(reports.DEFAULT_RANGE, alias="range"),
    start: str = "",
    end: str = "",
    admin: m.Employee = Depends(require_admin),
    db: Session = Depends(get_db),
):
    start_date, end_date = reports.resolve_date_range(rng, _parse_date(start), _parse_date(end))
    result = reports.attendance_report(db, start_date, end_date, department=dept or None, employee_id=emp or None)
    wb = Workbook()
    ws = wb.active
    if result["mode"] == "daily":
        ws.title = "Daily detail"
        _header(ws, ["Date", "Status"])
        for r in result["rows"]:
            ws.append([r["date"].isoformat(), reports.STATUS_LABELS.get(r["status"], r["status"])])
    else:
        ws.title = "Summary"
        _header(ws, ["Name", "Department"] + [reports.STATUS_LABELS[s] for s in reports.STATUS_ORDER] + ["Attendance %"])
        for r in result["rows"]:
            ws.append(
                [r["employee"].name, r["department"]]
                + [r["counts"][s] for s in reports.STATUS_ORDER]
                + [r["attendance_pct"] if r["attendance_pct"] is not None else ""]
            )
    return xlsx_response(wb, f"attendance_{start_date}_{end_date}.xlsx")


# ---- Strike Reports ----------------------------------------------------------
@router.get("/strikes")
def reports_strikes(
    request: Request,
    dept: str = "",
    emp: int = 0,
    rng: str = Query(reports.DEFAULT_RANGE, alias="range"),
    start: str = "",
    end: str = "",
    admin: m.Employee = Depends(require_admin),
    db: Session = Depends(get_db),
):
    ctx = _filter_ctx(db, dept, emp, rng, start, end)
    result = reports.strikes_report(
        db, ctx["resolved_start"], ctx["resolved_end"], department=dept or None, employee_id=emp or None
    )
    return render(request, "admin/reports_strikes.html", {"user": admin, "result": result, **ctx}, db=db)


@router.get("/strikes.xlsx")
def reports_strikes_xlsx(
    dept: str = "",
    emp: int = 0,
    rng: str = Query(reports.DEFAULT_RANGE, alias="range"),
    start: str = "",
    end: str = "",
    admin: m.Employee = Depends(require_admin),
    db: Session = Depends(get_db),
):
    start_date, end_date = reports.resolve_date_range(rng, _parse_date(start), _parse_date(end))
    result = reports.strikes_report(db, start_date, end_date, department=dept or None, employee_id=emp or None)
    wb = Workbook()
    ws = wb.active
    if result["mode"] == "daily":
        ws.title = "Strike days"
        _header(ws, ["Date", "Status"])
        for r in result["rows"]:
            ws.append([r["date"].isoformat(), reports.STATUS_LABELS.get(r["status"], r["status"])])
    else:
        ws.title = "Summary"
        _header(ws, ["Name", "Department", "Strikes"])
        for r in result["rows"]:
            ws.append([r["employee"].name, r["department"], r["strikes"]])
    return xlsx_response(wb, f"strikes_{start_date}_{end_date}.xlsx")
