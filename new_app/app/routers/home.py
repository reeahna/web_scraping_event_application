from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.config import get_settings
from app.core.templating import render
from app.dependencies import DbSession, OptionalCurrentUser
from app.services.rbac import can_access_admin

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def home(request: Request, current_user: OptionalCurrentUser, db: DbSession):
    settings = get_settings()
    return render(
        request,
        "home.html",
        {
            "current_user": current_user,
            "can_access_admin": can_access_admin(db, current_user) if current_user else False,
            "registration_enabled": settings.registration_enabled,
        },
    )
