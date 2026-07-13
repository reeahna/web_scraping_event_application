import json

from fastapi.responses import Response

FLASH_COOKIE = "flash"


def set_flash(response: Response, message: str, category: str = "success") -> None:
    """Attach a one-time status message to the *next* page the browser loads via
    this redirect response. Read (and cleared) by app.core.templating.render on
    that next request."""
    response.set_cookie(
        FLASH_COOKIE,
        json.dumps({"message": message, "category": category}),
        max_age=30,
        httponly=True,
        samesite="lax",
        path="/",
    )
