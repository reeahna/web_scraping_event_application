import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.core.csrf import get_or_create_csrf_token, set_csrf_cookie
from app.core.formatting import human_date, human_date_long, human_time

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

templates.env.filters["human_date"] = human_date
templates.env.filters["human_date_long"] = human_date_long
templates.env.filters["human_time"] = human_time

FLASH_COOKIE = "flash"


def _unread_notification_count(user_id: int) -> int:
    """Registered as a Jinja global (below) so admin_base.html can show a
    live unread-notification count without every admin route needing to
    thread it through context. Opens its own short-lived session on the
    same engine every request/service call already uses — a single bounded
    COUNT query, not a payload scan."""
    from app.database import SessionLocal
    from app.repositories.notification import count_unread_for_user

    db = SessionLocal()
    try:
        return count_unread_for_user(db, user_id)
    finally:
        db.close()


templates.env.globals["unread_notification_count"] = _unread_notification_count
templates.env.globals["current_year"] = lambda: datetime.now(UTC).year


def render(
    request: Request,
    template_name: str,
    context: dict[str, Any] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """Render a template with a CSRF token always available in context (and its
    cookie set if this is the first time we've seen this client), plus any
    one-time flash message left by a prior redirect (see app.core.flash.set_flash)."""
    token, is_new = get_or_create_csrf_token(request)

    flash = None
    raw_flash = request.cookies.get(FLASH_COOKIE)
    if raw_flash:
        try:
            flash = json.loads(raw_flash)
        except ValueError:
            flash = None

    full_context = {**(context or {}), "csrf_token": token, "flash": flash}
    response = templates.TemplateResponse(
        request, template_name, full_context, status_code=status_code
    )
    if is_new:
        set_csrf_cookie(response, token)
    if raw_flash:
        response.delete_cookie(FLASH_COOKIE, path="/")

    return response
