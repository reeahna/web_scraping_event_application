from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.exceptions import NotAuthenticatedError
from app.database import get_db
from app.models.user import User
from app.services.auth import resolve_current_user

DbSession = Annotated[Session, Depends(get_db)]


def get_current_user(request: Request, db: DbSession) -> User:
    settings = get_settings()
    return resolve_current_user(request, db, settings.session_cookie_name)


CurrentUser = Annotated[User, Depends(get_current_user)]


def get_current_user_optional(request: Request, db: DbSession) -> User | None:
    """For public pages that vary their display for logged-in users without
    requiring login — never use this to gate access to anything sensitive."""
    settings = get_settings()
    try:
        return resolve_current_user(request, db, settings.session_cookie_name)
    except NotAuthenticatedError:
        return None


OptionalCurrentUser = Annotated[User | None, Depends(get_current_user_optional)]


def get_correlation_id(request: Request) -> str | None:
    return getattr(request.state, "correlation_id", None)


CorrelationId = Annotated[str | None, Depends(get_correlation_id)]


def get_client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


ClientIp = Annotated[str | None, Depends(get_client_ip)]
