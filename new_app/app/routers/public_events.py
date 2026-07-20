from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.core.exceptions import NotFoundError
from app.core.templating import render
from app.dependencies import DbSession, OptionalCurrentUser
from app.repositories.extraction_run import get_latest_successful_run_for_website
from app.repositories.public_events import current_public_date, get_public_event
from app.services.rbac import can_access_admin

router = APIRouter(prefix="/events", tags=["public-events"])


@router.get("/{event_id}", response_class=HTMLResponse)
def event_detail(event_id: int, request: Request, current_user: OptionalCurrentUser, db: DbSession):
    event = get_public_event(db, event_id, today=current_public_date())
    if event is None:
        raise NotFoundError("Event not found")

    admin_access = can_access_admin(db, current_user) if current_user else False
    latest_run = None
    if admin_access and event.website_id is not None:
        latest_run = get_latest_successful_run_for_website(db, event.website_id)

    return render(
        request,
        "public_event_detail.html",
        {
            "current_user": current_user,
            "event": event,
            "can_access_admin": admin_access,
            "latest_run": latest_run,
        },
    )
