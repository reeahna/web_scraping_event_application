from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.core.templating import templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(request, "home.html", {})
