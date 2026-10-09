"""/register used to be a password sign-up form. Accounts are now created by
signing in with Google, Microsoft or Facebook, so old links land on sign-in."""

from fastapi import APIRouter
from fastapi.responses import RedirectResponse

router = APIRouter(tags=["registration"])


@router.get("/register", include_in_schema=False)
def register_moved() -> RedirectResponse:
    return RedirectResponse(url="/auth/login", status_code=301)
