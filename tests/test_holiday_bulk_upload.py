"""Bulk holiday upload: one sheet, creates or updates Holiday rows directly
(no employee to match against — see app/holiday_bulk_upload.py's module
docstring). Same in-memory SQLite fixture pattern as test_leave_bulk_upload.py.
"""
import datetime as dt

import pytest
from openpyxl import Workbook
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import models as m
from app.db import Base
from app.holiday_bulk_upload import (
    build_existing_holidays_workbook,
    build_sample_workbook,
    parse_country,
    parse_row,
    process_upload,
    read_upload_rows,
)


class TestParseCountry:
    def test_exact_match(self):
        assert parse_country("US") == "US"
        assert parse_country("India") == "India"

    def test_case_insensitive(self):
        assert parse_country("us") == "US"
        assert parse_country("india") == "India"
        assert parse_country("INDIA") == "India"

    def test_blank_raises(self):
        with pytest.raises(ValueError, match="required"):
            parse_country("")
        with pytest.raises(ValueError, match="required"):
            parse_country(None)

    def test_unrecognized_raises(self):
        with pytest.raises(ValueError, match="isn't a recognized country"):
            parse_country("Canada")


class TestParseRow:
    def test_happy_path(self):
        row = {"Holiday Name": "Diwali", "Holiday Date": "2026-11-08", "Country": "India"}
        result = parse_row(row)
        assert result["mode"] == "ok"
        assert result["date"] == dt.date(2026, 11, 8)
        assert result["location"] == "India"
        assert result["name"] == "Diwali"

    def test_excel_date_cell_passes_through(self):
        row = {"Holiday Name": "Independence Day", "Holiday Date": dt.date(2026, 7, 4), "Country": "US"}
        result = parse_row(row)
        assert result["mode"] == "ok"
        assert result["date"] == dt.date(2026, 7, 4)

    def test_missing_date_is_an_error(self):
        row = {"Holiday Name": "X", "Holiday Date": None, "Country": "US"}
        result = parse_row(row)
        assert result["mode"] == "error"
        assert "Date is required" in result["error"]

    def test_missing_country_is_an_error(self):
        row = {"Holiday Name": "X", "Holiday Date": "2026-07-04", "Country": ""}
        result = parse_row(row)
        assert result["mode"] == "error"
        assert "required" in result["error"]

    def test_blank_name_is_allowed(self):
        row = {"Holiday Name": "", "Holiday Date": "2026-07-04", "Country": "US"}
        result = parse_row(row)
        assert result["mode"] == "ok"
        assert result["name"] == ""

    def test_bad_date_format_is_an_error(self):
        row = {"Holiday Name": "X", "Holiday Date": "not a date", "Country": "US"}
        result = parse_row(row)
        assert result["mode"] == "error"


class TestSampleAndExportWorkbooks:
    def test_sample_workbook_rows_parse_cleanly(self):
        wb = build_sample_workbook()
        rows, header_error = read_upload_rows(wb)
        assert header_error is None
        assert len(rows) == 2
        for r in rows:
            result = parse_row(r)
            assert result["mode"] == "ok"

    def test_empty_sheet_is_a_sheet_level_error(self):
        wb = Workbook()
        rows, error = read_upload_rows(wb)
        assert rows == [] and error is not None

    def test_blank_rows_skipped_silently(self):
        wb = Workbook()
        ws = wb.active
        ws.append(["Holiday Name", "Holiday Date", "Country"])
        ws.append(["Diwali", dt.date(2026, 11, 8), "India"])
        ws.append([None, None, None])
        rows, error = read_upload_rows(wb)
        assert error is None
        assert len(rows) == 1


@pytest.fixture()
def db():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


def _wb_from_rows(header, rows):
    wb = Workbook()
    ws = wb.active
    ws.append(header)
    for r in rows:
        ws.append(r)
    return wb


class TestProcessUpload:
    def test_valid_row_creates_holiday(self, db):
        wb = _wb_from_rows(
            ["Holiday Name", "Holiday Date", "Country"],
            [["Diwali", dt.date(2026, 11, 8), "India"]],
        )
        result = process_upload(db, wb)
        assert result["header_error"] is None
        assert result["added"] == 1
        assert result["updated"] == 0
        row = db.execute(select(m.Holiday)).scalar_one()
        assert row.name == "Diwali" and row.location == "India"

    def test_same_date_different_country_both_created(self, db):
        wb = _wb_from_rows(
            ["Holiday Name", "Holiday Date", "Country"],
            [["A", dt.date(2026, 12, 25), "US"], ["B", dt.date(2026, 12, 25), "India"]],
        )
        result = process_upload(db, wb)
        assert result["added"] == 2
        rows = list(db.execute(select(m.Holiday)).scalars())
        assert {(r.location, r.name) for r in rows} == {("US", "A"), ("India", "B")}

    def test_re_upload_same_date_and_country_updates_name_not_duplicates(self, db):
        db.add(m.Holiday(date=dt.date(2026, 11, 8), name="Old Name", location="India"))
        db.commit()
        wb = _wb_from_rows(
            ["Holiday Name", "Holiday Date", "Country"],
            [["Diwali (fixed)", dt.date(2026, 11, 8), "India"]],
        )
        result = process_upload(db, wb)
        assert result["added"] == 0
        assert result["updated"] == 1
        rows = list(db.execute(select(m.Holiday)).scalars())
        assert len(rows) == 1
        assert rows[0].name == "Diwali (fixed)"

    def test_invalid_country_reported_not_silently_ignored(self, db):
        wb = _wb_from_rows(
            ["Holiday Name", "Holiday Date", "Country"],
            [["X", dt.date(2026, 7, 4), "Canada"]],
        )
        result = process_upload(db, wb)
        assert result["added"] == 0
        assert len(result["skipped"]) == 1
        assert "isn't a recognized country" in result["skipped"][0]["reason"]

    def test_multiple_rows_mixed_valid_and_invalid(self, db):
        wb = _wb_from_rows(
            ["Holiday Name", "Holiday Date", "Country"],
            [["Good", dt.date(2026, 7, 4), "US"], ["Bad", None, "US"]],
        )
        result = process_upload(db, wb)
        assert result["added"] == 1
        assert len(result["skipped"]) == 1

    def test_too_many_rows_rejected_at_sheet_level(self, db):
        rows = [["X", dt.date(2026, 1, 1), "US"] for _ in range(1001)]
        wb = _wb_from_rows(["Holiday Name", "Holiday Date", "Country"], rows)
        result = process_upload(db, wb)
        assert result["header_error"] is not None
        assert "max is 1000" in result["header_error"]

    def test_export_round_trips_into_a_valid_row(self, db):
        db.add(m.Holiday(date=dt.date(2026, 11, 8), name="Diwali", location="India"))
        db.commit()
        wb = build_existing_holidays_workbook(db)
        rows, error = read_upload_rows(wb)
        assert error is None
        assert len(rows) == 1
        result = parse_row(rows[0])
        assert result["mode"] == "ok"
        assert result["location"] == "India"
