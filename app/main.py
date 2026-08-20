"""MK Internal Timekeeping & Compliance App — POC entrypoint."""
import logging
import os

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware
from fastapi.staticfiles import StaticFiles

from app.auth import AUTH_MODE, Forbidden, RequiresLogin
from app.db import SessionLocal, get_db, init_db
from app.routes import admin as admin_routes
from app.routes import auth as auth_routes
from app.routes import employee as employee_routes
from app.routes import exports as export_routes
from app.routes import reports as report_routes
from app.routes import tickets as ticket_routes
from app.templating import TICKETING_ENABLED, render, templates  # noqa: F401 (templates import registers filters)
from app.util import (
    ensure_bootstrap_admins,
    ensure_employee_codes,
    ensure_list_status_backfill,
    ensure_location_backfill,
    ensure_super_admin_backfill,
)

# Most hosts (Azure App Service included) just capture stdout — a basic
# config here is the difference between "the logs say what broke" and
# "nothing at all" once this isn't running on a laptop where you can rm
# tms.db and start over.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("app")

_SECRET_KEY = os.environ.get("SECRET_KEY")
if not _SECRET_KEY:
    if AUTH_MODE == "dev":
        _SECRET_KEY = "poc-dev-secret-change-in-azure"
    else:
        # Real employees, real sessions: refuse to boot on the hardcoded
        # fallback rather than silently sign cookies with a public secret.
        raise RuntimeError(
            "SECRET_KEY environment variable is required when AUTH_MODE != 'dev'. "
            "Set a long random value in the host's environment config."
        )

app = FastAPI(title="MK Timekeeping & Compliance")
app.add_middleware(
    SessionMiddleware,
    secret_key=_SECRET_KEY,
    same_site="lax",
    # HTTPS-only cookies once real credentials are in play; dev keeps plain
    # http://localhost working.
    https_only=AUTH_MODE != "dev",
)
# Ganesh, 2026-08-21 (performance pass, "will it work with 100 concurrent
# users"): compresses every text response (HTML pages, the CSS/JS under
# /static, XLSX/CSV exports) over minimum_size bytes before it goes over
# the wire. This is the single biggest, lowest-risk win available for
# "slow loading" — Reports pages in particular render large HTML tables —
# and it's pure transport-layer plumbing: no route, query, or template
# logic changes, so there's no compliance-math risk and no pytest/
# verify_strikes re-run needed (see CLAUDE.md's rule — that only applies
# to engine.py/validation.py/legacy/import_legacy.py).
app.add_middleware(GZipMiddleware, minimum_size=1000)

static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
# Avatar directory may live outside app/static entirely (see
# app/routes/employee.py AVATAR_DIR — AVATAR_UPLOAD_DIR env var) so it can
# point at a host's persistent storage instead of the deployed code tree.
# Mounted first, at the same /static/uploads/avatars prefix the app has
# always used, so it wins over the general /static mount below and no
# template ever needs to know where the directory actually lives.
os.makedirs(employee_routes.AVATAR_DIR, exist_ok=True)
app.mount("/static/uploads/avatars", StaticFiles(directory=employee_routes.AVATAR_DIR), name="avatars")
# Same reasoning as avatars above — ticket attachments (jpg/png/mp4) may
# live outside app/static too (see app/routes/tickets.py TICKET_ATTACHMENT_DIR
# — TICKET_ATTACHMENT_UPLOAD_DIR env var). Guarded by TICKETING_ENABLED (see
# app/templating.py) — Ticketing is built and tested but not live yet
# (Ganesh, 2026-08-06: shipping the Time by Project/Task report on its own
# first). Flip the flag to True when it's ready; nothing else changes.
if TICKETING_ENABLED:
    os.makedirs(ticket_routes.TICKET_ATTACHMENT_DIR, exist_ok=True)
    app.mount(
        "/static/uploads/tickets", StaticFiles(directory=ticket_routes.TICKET_ATTACHMENT_DIR),
        name="ticket_attachments",
    )
app.mount("/static", StaticFiles(directory=static_dir), name="static")

app.include_router(auth_routes.router)
app.include_router(employee_routes.router)
app.include_router(admin_routes.router)
app.include_router(export_routes.router)
app.include_router(report_routes.router)
if TICKETING_ENABLED:
    app.include_router(ticket_routes.router)


@app.exception_handler(RequiresLogin)
async def _requires_login(request: Request, exc: RequiresLogin):
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(Forbidden)
async def _forbidden(request: Request, exc: Forbidden):
    return RedirectResponse("/", status_code=303)


@app.exception_handler(StarletteHTTPException)
async def _http_exception(request: Request, exc: StarletteHTTPException):
    # Covers unmatched routes (404) and any explicit HTTPException raises.
    # Without this, FastAPI's default is a bare JSON {"detail": ...} body —
    # fine for an API, jarring for a server-rendered Jinja app real people
    # click around in.
    if exc.status_code == 404:
        heading, message = "Page not found", "That page doesn't exist, or you don't have access to it."
    else:
        heading, message = f"Error {exc.status_code}", str(exc.detail) or "Something went wrong."
    return render(request, "error.html", {"heading": heading, "message": message}, status_code=exc.status_code)


@app.exception_handler(Exception)
async def _unhandled_exception(request: Request, exc: Exception):
    # Last resort for real bugs — log the full traceback (so it's visible
    # in the host's log stream) and show a friendly page instead of a raw
    # stack trace to whichever of the ~45 employees happened to hit it.
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return render(
        request, "error.html",
        {
            "heading": "Something went wrong",
            "message": "We've logged the error — try again in a moment, or contact your admin if it keeps happening.",
        },
        status_code=500,
    )


@app.get("/healthz")
def healthz(db: Session = Depends(get_db)):
    """Uptime-monitor target for Azure App Service (or anything else pinging
    this service). Checks real DB connectivity, not just that the process
    is alive — a hung/unreachable database is exactly the kind of failure
    a process-alive check would miss."""
    db.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.on_event("startup")
def _startup() -> None:
    init_db()
    db = SessionLocal()
    try:
        ensure_employee_codes(db)
        ensure_super_admin_backfill(db)
        ensure_list_status_backfill(db)
        ensure_location_backfill(db)
        ensure_bootstrap_admins(db)
    finally:
        db.close()
    # Printed so the exact absolute path is visible in the host's deploy
    # logs — on Azure App Service, set AVATAR_UPLOAD_DIR to a path under
    # /home (e.g. /home/data/avatars) so uploads survive a redeploy; /home
    # persists automatically there, unlike the rest of the deployed code
    # tree. No env var set = falls back to the in-repo path (fine for local
    # dev; on hosts without a persistent /home equivalent, mount a volume
    # at whichever path this prints and point AVATAR_UPLOAD_DIR at it).
    logger.info("Avatar uploads directory: %s", employee_routes.AVATAR_DIR)
    if TICKETING_ENABLED:
        logger.info("Ticket attachments directory: %s", ticket_routes.TICKET_ATTACHMENT_DIR)
