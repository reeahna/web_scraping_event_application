"""Operational reporting dashboard (Phase 15).

One read-only admin view (`GET /admin/reports`) aggregating system health. It
surfaces counts and short recent lists only; the reporting service redacts all
secrets and sensitive payloads. Gated on `reports.view`.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.core.templating import render
from app.dependencies import DbSession
from app.models.user import User
from app.services.rbac import require_permission
from app.services.reporting import build_operational_report

router = APIRouter(prefix="/admin/reports", tags=["admin-reporting"])

ViewReports = Annotated[User, Depends(require_permission("reports.view"))]


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value else default
    except ValueError:
        return default


@router.get("", response_class=HTMLResponse)
def dashboard(request: Request, db: DbSession, current_user: ViewReports):
    audit_page = max(_int(request.query_params.get("audit_page"), 1), 1)
    report = build_operational_report(db, audit_page=audit_page)
    return render(
        request,
        "admin/reports/dashboard.html",
        {"current_user": current_user, "report": report},
    )
