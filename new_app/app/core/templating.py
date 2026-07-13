from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.core.csrf import get_or_create_csrf_token, set_csrf_cookie

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def render(
    request: Request,
    template_name: str,
    context: dict[str, Any] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """Render a template with a CSRF token always available in context (and its
    cookie set if this is the first time we've seen this client)."""
    token, is_new = get_or_create_csrf_token(request)
    full_context = {**(context or {}), "csrf_token": token}
    response = templates.TemplateResponse(
        request, template_name, full_context, status_code=status_code
    )
    if is_new:
        set_csrf_cookie(response, token)
    return response
