"""Short sign-in addresses that point at the real page, /auth/login.

/register used to be a password sign-up form. Accounts are now created by
signing in with Google, Microsoft, Facebook, X or Yahoo, so old links land on
sign-in. /login is what people type and what other sites guess."""

from urllib.parse import urlencode

from fastapi import APIRouter
from fastapi.responses import RedirectResponse

from app.services.oauth_login import safe_next

router = APIRouter(tags=["registration"])


@router.get("/register", include_in_schema=False)
def register_moved() -> RedirectResponse:
    return RedirectResponse(url="/auth/login", status_code=301)


@router.get("/login", include_in_schema=False)
def login_shortcut(next: str | None = None) -> RedirectResponse:
    """A temporary redirect, so the sign-in page can move again later. A
    `next` that is a path on this site is passed along; anything else (an
    absolute URL, //host) is dropped, as /auth/login itself would drop it."""
    target = "/auth/login"
    if next and safe_next(next) == next:
        target += "?" + urlencode({"next": next})
    return RedirectResponse(url=target, status_code=307)
