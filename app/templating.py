"""Shared Jinja2 environment with app filters."""
import os

from fastapi.templating import Jinja2Templates

from app import models as m
from app.util import (
    STATUS_LABELS,
    STATUS_NAMES,
    fmt_hm,
    fmt_hm_signed,
    fmt_hours,
    fmt_time,
    month_label,
)

templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
)
templates.env.filters["hm"] = fmt_hm
templates.env.filters["hm_signed"] = fmt_hm_signed
templates.env.filters["hours"] = fmt_hours
templates.env.filters["clock"] = fmt_time
templates.env.globals["month_label"] = month_label
templates.env.globals["STATUS_LABELS"] = STATUS_LABELS
templates.env.globals["STATUS_NAMES"] = STATUS_NAMES
templates.env.globals["STATUSES"] = [m.COMPLETE, m.PARTIAL, m.MISSING, m.LEAVE, m.HOLIDAY, m.WEEKEND]


def flash(request, message: str, kind: str = "ok") -> None:
    request.session.setdefault("flash", []).append({"kind": kind, "msg": message})


def pop_flashes(request):
    return request.session.pop("flash", [])


def render(request, name: str, ctx: dict):
    ctx = dict(ctx)
    ctx["request"] = request
    ctx["flashes"] = pop_flashes(request)
    return templates.TemplateResponse(name, ctx)
