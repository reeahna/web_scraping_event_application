"""Admin System health page: is Bulletin working right now?"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.core.templating import render
from app.dependencies import DbSession
from app.models.user import User
from app.routers.admin import _onboarding_groups
from app.services.rbac import require_permission
from app.services.system_health import collect_system_health, time_ago

router = APIRouter(prefix="/admin/health", tags=["admin-health"])

ViewSites = Annotated[User, Depends(require_permission("sites.view"))]


@router.get("", response_class=HTMLResponse)
def system_health(request: Request, db: DbSession, current_user: ViewSites):
    broken, waiting = _onboarding_groups(db)
    health = collect_system_health(db, waiting_rows=waiting)
    return render(
        request,
        "admin/system_health.html",
        {
            "current_user": current_user,
            "health": health,
            "broken": broken,
            "waiting": waiting,
            "ago": lambda value: time_ago(value, health.now),
        },
    )
