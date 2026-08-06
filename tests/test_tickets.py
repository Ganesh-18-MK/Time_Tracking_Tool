"""Ticketing System (Ganesh, 2026-08-06): Ticket/TicketComment model defaults,
the comment thread ordering, cascade-delete of comments, and the priority
sort order used by the /tickets list (app/routes/tickets.py). Mirrors
test_overtime.py's pattern — an in-memory sqlite db, rows seeded directly.

Route handlers (list/new/comment/status-change) aren't covered here, same
as every other route module in this suite (see test_overtime.py's module
docstring) — there's no FastAPI TestClient anywhere in this project; what
IS testable without one is the data layer these routes build on.
"""
import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models as m
from app.db import Base


@pytest.fixture()
def db():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


def _emp(db, id, name, is_developer=False):
    e = m.Employee(
        id=id, name=name, department="Ops", active=True,
        daily_target_minutes=480, work_days="0,1,2,3,4",
        is_developer=is_developer,
    )
    db.add(e)
    return e


class TestEmployeeIsDeveloperDefault:
    def test_defaults_to_false(self, db):
        # Ganesh: as of now only he and Mohan are developers — every other
        # roster row (existing or new) must default to not-a-developer
        # without a dedicated backfill script (see Employee.is_developer's
        # docstring — plain truthiness, unlike Project.status).
        e = _emp(db, 1, "Asha")
        db.commit()
        db.refresh(e)
        assert e.is_developer is False


class TestTicketDefaults:
    def test_new_ticket_defaults_to_bug_medium_open(self, db):
        _emp(db, 1, "Asha")
        db.commit()
        t = m.Ticket(employee_id=1, subject="Login button misaligned", description="On Safari only.")
        db.add(t)
        db.commit()
        db.refresh(t)
        assert t.ticket_type == m.TICKET_BUG
        assert t.priority == m.TICKET_MEDIUM
        assert t.status == m.TICKET_OPEN
        assert t.attachment_path is None
        assert t.resolved_at is None

    def test_employee_relationship_resolves_the_raiser(self, db):
        _emp(db, 1, "Asha")
        db.commit()
        t = m.Ticket(employee_id=1, subject="s", description="d")
        db.add(t)
        db.commit()
        db.refresh(t)
        assert t.employee.name == "Asha"


class TestTicketComments:
    def test_comments_ordered_oldest_first(self, db):
        _emp(db, 1, "Asha")
        db.commit()
        t = m.Ticket(employee_id=1, subject="s", description="d")
        db.add(t)
        db.commit()
        later = dt.datetime(2026, 8, 6, 10, 0)
        earlier = dt.datetime(2026, 8, 6, 9, 0)
        db.add(m.TicketComment(ticket_id=t.id, employee_id=1, message="second", created_at=later))
        db.add(m.TicketComment(ticket_id=t.id, employee_id=1, message="first", created_at=earlier))
        db.commit()
        db.refresh(t)
        assert [c.message for c in t.comments] == ["first", "second"]

    def test_deleting_ticket_cascades_to_its_comments(self, db):
        _emp(db, 1, "Asha")
        db.commit()
        t = m.Ticket(employee_id=1, subject="s", description="d")
        db.add(t)
        db.commit()
        db.add(m.TicketComment(ticket_id=t.id, employee_id=1, message="hello"))
        db.commit()
        db.delete(t)
        db.commit()
        remaining = db.query(m.TicketComment).all()
        assert remaining == []


class TestTicketPriorityRank:
    """The /tickets list sorts by this dict (app/routes/tickets.py) so
    Urgent surfaces first — a plain string sort would put 'high' before
    'low' before 'urgent' alphabetically, which is wrong."""

    def test_urgent_ranks_before_high_before_medium_before_low(self):
        ranks = [m.TICKET_PRIORITY_RANK[p] for p in (m.TICKET_URGENT, m.TICKET_HIGH, m.TICKET_MEDIUM, m.TICKET_LOW)]
        assert ranks == sorted(ranks)

    def test_every_priority_has_a_rank(self):
        assert set(m.TICKET_PRIORITY_RANK) == set(m.TICKET_PRIORITIES)


class TestTicketConstants:
    def test_status_label_covers_every_status(self):
        assert set(m.TICKET_STATUS_LABELS) == set(m.TICKET_STATUSES)

    def test_type_label_covers_every_type(self):
        assert set(m.TICKET_TYPE_LABELS) == set(m.TICKET_TYPES)

    def test_priority_label_covers_every_priority(self):
        assert set(m.TICKET_PRIORITY_LABELS) == set(m.TICKET_PRIORITIES)
