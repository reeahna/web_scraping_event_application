from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.core.templating import render
from app.dependencies import OptionalCurrentUser

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def home(request: Request, current_user: OptionalCurrentUser):
    return render(request, "home.html", {"current_user": current_user})
