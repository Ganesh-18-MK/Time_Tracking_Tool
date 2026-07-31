"""Bulk-add Project/Employer and Task Type dropdown values (Projects & Tasks
-> Bulk upload). One column per sheet, add-only — see
app/lists_bulk_upload.py's module docstring for why this is deliberately
simpler than app/bulk_upload.py / app/leave_bulk_upload.py. Same in-memory
SQLite fixture pattern as test_reports.py/test_compensation.py for the
process_upload() tests; read_upload_names()/build_sample_workbook() are
pure enough to test without a db at all.
"""
import pytest
from openpyxl import Workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models as m
from app.db import Base
from app.lists_bulk_upload import (
    HEADERS,
    build_existing_workbook,
    build_sample_workbook,
    process_upload,
    read_upload_names,
)


@pytest.fixture()
def db():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


def _sheet(header, *values):
    wb = Workbook()
    ws = wb.active
    ws.append([header])
    for v in values:
        ws.append([v])
    return wb


class TestReadUploadNames:
    def test_happy_path_skips_blanks_and_trims_whitespace(self):
        wb = _sheet("Project / Employer Name", "Acme Corp.", "", None, "  Beta LLC  ")
        names, err = read_upload_names(wb, "project")
        assert err is None
        assert names == [(2, "Acme Corp."), (5, "Beta LLC")]

    def test_header_match_is_case_insensitive(self):
        wb = _sheet("project / employer name", "Gamma Inc.")
        names, err = read_upload_names(wb, "project")
        assert err is None
        assert names == [(2, "Gamma Inc.")]

    def test_wrong_column_header_is_an_error(self):
        wb = _sheet("Some Other Column", "x")
        names, err = read_upload_names(wb, "project")
        assert names == []
        assert err is not None and HEADERS["project"] in err

    def test_empty_sheet_is_an_error(self):
        wb = Workbook()
        names, err = read_upload_names(wb, "task")
        assert names == []
        assert err is not None and "empty" in err.lower()

    def test_task_sheet_uses_its_own_header(self):
        wb = _sheet("Task Name", "File review")
        names, err = read_upload_names(wb, "task")
        assert err is None
        assert names == [(2, "File review")]


class TestProcessUpload:
    def test_new_names_are_added(self, db):
        from sqlalchemy import select
        wb = _sheet("Project / Employer Name", "Acme Corp.", "Beta LLC")
        result = process_upload(db, wb, "project")
        assert result["header_error"] is None
        assert result["added"] == 2
        assert result["skipped"] == []
        names = {p.name for p in db.execute(select(m.Project)).scalars()}
        assert names == {"Acme Corp.", "Beta LLC"}

    def test_name_already_on_the_list_is_skipped_not_duplicated(self, db):
        db.add(m.Project(name="Acme Corp."))
        db.commit()
        wb = _sheet("Project / Employer Name", "Acme Corp.", "New Client Inc.")
        result = process_upload(db, wb, "project")
        assert result["added"] == 1
        assert len(result["skipped"]) == 1
        assert result["skipped"][0]["name"] == "Acme Corp."
        assert "already on the list" in result["skipped"][0]["reason"].lower()

    def test_duplicate_within_the_same_file_is_added_once(self, db):
        wb = _sheet("Project / Employer Name", "New Client Inc.", "New Client Inc.")
        result = process_upload(db, wb, "project")
        assert result["added"] == 1
        assert len(result["skipped"]) == 1
        assert "duplicate" in result["skipped"][0]["reason"].lower()

    def test_task_kind_writes_to_task_type_not_project(self, db):
        wb = _sheet("Task Name", "File review")
        result = process_upload(db, wb, "task")
        assert result["added"] == 1
        from sqlalchemy import select
        tasks = list(db.execute(select(m.TaskType)).scalars())
        projects = list(db.execute(select(m.Project)).scalars())
        assert [t.name for t in tasks] == ["File review"]
        assert projects == []

    def test_header_error_short_circuits_before_touching_the_db(self, db):
        wb = _sheet("Wrong Column", "x")
        result = process_upload(db, wb, "project")
        assert result["header_error"] is not None
        assert result["added"] == 0
        from sqlalchemy import select
        assert list(db.execute(select(m.Project)).scalars()) == []

    def test_no_new_rows_means_no_commit_needed_but_still_reports_correctly(self, db):
        db.add(m.Project(name="Acme Corp."))
        db.commit()
        wb = _sheet("Project / Employer Name", "Acme Corp.")
        result = process_upload(db, wb, "project")
        assert result["added"] == 0
        assert len(result["skipped"]) == 1


class TestSampleAndExistingWorkbooks:
    def test_sample_workbook_header_matches_kind(self):
        for kind in ("project", "task"):
            wb = build_sample_workbook(kind)
            assert wb.active["A1"].value == HEADERS[kind]

    def test_existing_workbook_lists_only_active_names(self, db):
        db.add(m.Project(name="Active One", active=True))
        db.add(m.Project(name="Retired One", active=False))
        db.commit()
        wb = build_existing_workbook(db, "project")
        ws = wb.active
        values = [ws.cell(row=r, column=1).value for r in range(2, ws.max_row + 1)]
        assert values == ["Active One"]
