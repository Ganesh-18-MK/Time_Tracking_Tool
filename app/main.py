"""MK Internal Timekeeping & Compliance App — POC entrypoint."""
import os

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.auth import AUTH_MODE, Forbidden, RequiresLogin
from app.db import init_db
from app.routes import admin as admin_routes
from app.routes import auth as auth_routes
from app.routes import employee as employee_routes
from app.routes import exports as export_routes
from app.templating import templates  # noqa: F401 (registers filters)

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

static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")

app.include_router(auth_routes.router)
app.include_router(employee_routes.router)
app.include_router(admin_routes.router)
app.include_router(export_routes.router)


@app.exception_handler(RequiresLogin)
async def _requires_login(request: Request, exc: RequiresLogin):
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(Forbidden)
async def _forbidden(request: Request, exc: Forbidden):
    return RedirectResponse("/", status_code=303)


@app.on_event("startup")
def _startup() -> None:
    init_db()
