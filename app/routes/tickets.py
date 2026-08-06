"""Ticketing System (Ganesh, 2026-08-06) — internal bug/enhancement/new-feature
tracker reached from the Support page. Everyone who can log in (Employee,
Admin, Super Admin alike) can raise a ticket, see the full org-wide list, and
comment; only Developers (Employee.is_developer — an axis independent of
is_admin, see app/models.py) can change a ticket's status. See
app/auth.py's require_developer for the one gated action.

No admin-only routes here on purpose — a Super Admin views/acts on tickets
through these exact same routes as anyone else, there's nothing department-
or lead-scoped about visibility (unlike Leave/Overtime)."""
import datetime as dt
import os

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models as m
from app.auth import current_user, require_developer
from app.db import get_db
from app.templating import flash, render
from app.util import audit

router = APIRouter()

# ---- attachment storage -----------------------------------------------------
# Same on-disk pattern as app/routes/employee.py's AVATAR_DIR: env-overridable
# so a host's persistent storage can be pointed at instead of the deployed
# code tree (see that file's comment for why). One attachment per ticket;
# named after the ticket's own id (not the employee's — one employee can
# raise several tickets), so the row is flushed for its id before the file
# is written. jpg/png/mp4 only, ~25 MB cap (Ganesh's plan, approved 2026-08-06).
_STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
TICKET_ATTACHMENT_DIR = (
    os.environ.get("TICKET_ATTACHMENT_UPLOAD_DIR") or os.path.join(_STATIC_DIR, "uploads", "tickets")
)
ALLOWED_ATTACHMENT_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "video/mp4": ".mp4"}
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024  # 25 MB

MIN_TICKET_DESCRIPTION_CHARS = 5
MIN_COMMENT_CHARS = 1


def _ticket_or_none(db: Session, ticket_id: int):
    return db.get(m.Ticket, ticket_id)


@router.get("/tickets")
def tickets_list(
    request: Request,
    status: str = "all",
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    q = select(m.Ticket)
    if status in m.TICKET_STATUSES:
        q = q.where(m.Ticket.status == status)
    rows = list(db.execute(q).scalars())
    # Urgent-first, then newest first within the same priority — the
    # "based on priority we will be working on it" ordering Ganesh described
    # for the Developer view, but harmless/sensible for everyone else too.
    rows.sort(key=lambda t: (m.TICKET_PRIORITY_RANK.get(t.priority, 9), -t.created_at.timestamp()))
    return render(
        request, "tickets.html",
        {"user": user, "rows": rows, "status": status}, db=db,
    )


@router.get("/tickets/new")
def ticket_new_page(
    request: Request,
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    existing_subjects = list(db.execute(select(m.Ticket.subject)).scalars())
    return render(request, "ticket_new.html", {"user": user, "existing_subjects": existing_subjects})


@router.post("/tickets/new")
def ticket_submit(
    request: Request,
    subject: str = Form(...),
    description: str = Form(...),
    ticket_type: str = Form(...),
    priority: str = Form(...),
    attachment: UploadFile = File(None),
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    subject = subject.strip()
    description = description.strip()
    if not subject:
        flash(request, "Enter a subject.", "err")
        return RedirectResponse("/tickets/new", status_code=303)
    if len(description) < MIN_TICKET_DESCRIPTION_CHARS:
        flash(request, f"Please describe the issue (at least {MIN_TICKET_DESCRIPTION_CHARS} characters).", "err")
        return RedirectResponse("/tickets/new", status_code=303)
    if ticket_type not in m.TICKET_TYPES:
        flash(request, "Choose a valid ticket type.", "err")
        return RedirectResponse("/tickets/new", status_code=303)
    if priority not in m.TICKET_PRIORITIES:
        flash(request, "Choose a valid priority.", "err")
        return RedirectResponse("/tickets/new", status_code=303)

    data = None
    ext = None
    if attachment is not None and attachment.filename:
        ext = ALLOWED_ATTACHMENT_TYPES.get(attachment.content_type)
        if ext is None:
            flash(request, "Attachments must be JPEG, PNG, or MP4.", "err")
            return RedirectResponse("/tickets/new", status_code=303)
        data = attachment.file.read(MAX_ATTACHMENT_BYTES + 1)
        if len(data) > MAX_ATTACHMENT_BYTES:
            flash(request, "Attachment must be under 25 MB.", "err")
            return RedirectResponse("/tickets/new", status_code=303)
        if not data:
            data = None  # empty upload — treat as "no attachment", not an error

    ticket = m.Ticket(
        employee_id=user.id, subject=subject, description=description,
        ticket_type=ticket_type, priority=priority, status=m.TICKET_OPEN,
    )
    db.add(ticket)
    db.flush()  # need ticket.id before naming the attachment file

    if data:
        os.makedirs(TICKET_ATTACHMENT_DIR, exist_ok=True)
        filename = f"{ticket.id}{ext}"
        with open(os.path.join(TICKET_ATTACHMENT_DIR, filename), "wb") as f:
            f.write(data)
        ticket.attachment_path = filename

    db.commit()
    audit(db, user.name, "ticket_raised", "Ticket", ticket.id,
          {"subject": subject[:200], "type": ticket_type, "priority": priority})
    flash(request, "Ticket submitted.", "ok")
    return RedirectResponse(f"/tickets/{ticket.id}", status_code=303)


@router.get("/tickets/{ticket_id}")
def ticket_detail(
    ticket_id: int,
    request: Request,
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    ticket = _ticket_or_none(db, ticket_id)
    if ticket is None:
        flash(request, "That ticket doesn't exist.", "err")
        return RedirectResponse("/tickets", status_code=303)
    return render(request, "ticket_detail.html", {"user": user, "ticket": ticket})


@router.post("/tickets/{ticket_id}/comment")
def ticket_comment(
    ticket_id: int,
    request: Request,
    message: str = Form(...),
    user: m.Employee = Depends(current_user),
    db: Session = Depends(get_db),
):
    ticket = _ticket_or_none(db, ticket_id)
    if ticket is None:
        flash(request, "That ticket doesn't exist.", "err")
        return RedirectResponse("/tickets", status_code=303)
    message = message.strip()
    if len(message) < MIN_COMMENT_CHARS:
        flash(request, "Enter a comment before submitting.", "err")
        return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)
    db.add(m.TicketComment(ticket_id=ticket.id, employee_id=user.id, message=message))
    ticket.updated_at = dt.datetime.utcnow()
    db.commit()
    audit(db, user.name, "ticket_commented", "Ticket", ticket.id, {"message": message[:200]})
    return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)


@router.post("/tickets/{ticket_id}/status")
def ticket_status_change(
    ticket_id: int,
    request: Request,
    status: str = Form(...),
    developer: m.Employee = Depends(require_developer),
    db: Session = Depends(get_db),
):
    ticket = _ticket_or_none(db, ticket_id)
    if ticket is None:
        flash(request, "That ticket doesn't exist.", "err")
        return RedirectResponse("/tickets", status_code=303)
    if status not in m.TICKET_STATUSES:
        flash(request, "Choose a valid status.", "err")
        return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)
    ticket.status = status
    ticket.updated_at = dt.datetime.utcnow()
    if status in (m.TICKET_RESOLVED, m.TICKET_CLOSED):
        ticket.resolved_by = developer.name
        ticket.resolved_at = dt.datetime.utcnow()
    else:
        ticket.resolved_by = ""
        ticket.resolved_at = None
    db.commit()
    audit(db, developer.name, "ticket_status_changed", "Ticket", ticket.id, {"status": status})
    flash(request, "Status updated.", "ok")
    return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)
