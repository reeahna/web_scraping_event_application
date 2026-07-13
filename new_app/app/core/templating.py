import json
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.core.csrf import get_or_create_csrf_token, set_csrf_cookie

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

FLASH_COOKIE = "flash"


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
