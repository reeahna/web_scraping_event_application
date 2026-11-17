"""Visitor log (`GET /admin/visitors`): daily visitors, which towns they look
at, top pages, where they come from, who was signed in, and recent sign-ins.
Read-only and gated on `reports.view`. See app.services.site_visits."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.core.templating import render
from app.dependencies import DbSession
from app.models.user import User
from app.services.rbac import require_permission
from app.services.site_visits import build_visitor_report

router = APIRouter(prefix="/admin/visitors", tags=["admin-visitors"])

ViewReports = Annotated[User, Depends(require_permission("reports.view"))]

RANGES = (1, 7, 30, 90)


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value else default
    except ValueError:
        return default


@router.get("", response_class=HTMLResponse)
def visitor_log(request: Request, db: DbSession, current_user: ViewReports):
    days = _int(request.query_params.get("days"), 7)
    if days not in RANGES:
        days = 7
    page = max(_int(request.query_params.get("page"), 1), 1)
    report = build_visitor_report(db, days=days, log_page=page)
    return render(
        request,
        "admin/visitors.html",
        {"current_user": current_user, "report": report, "ranges": RANGES},
    )
