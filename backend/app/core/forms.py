from typing import Annotated

from fastapi import Request
from pydantic import BeforeValidator

from app.core.exceptions import AppError


async def reject_unexpected_form_fields(request: Request, allowed_fields: set[str]) -> None:
    submitted = await request.form()
    unexpected = set(submitted) - allowed_fields
    if unexpected:
        raise AppError("Unexpected form fields were submitted", status_code=422)


def _blank_to_none(value: object) -> object:
    """An unselected <select> posts "", which `int | None` cannot parse.

    Every admin filter bar offers an "All …" option with `value=""`, so
    submitting the form with any of them selected used to fail the whole
    request with a 422 before the handler ran. Treat blank as "no filter".
    """
    if isinstance(value, str) and not value.strip():
        return None
    return value


OptionalId = Annotated[int | None, BeforeValidator(_blank_to_none)]
OptionalFlag = Annotated[bool | None, BeforeValidator(_blank_to_none)]
OptionalText = Annotated[str | None, BeforeValidator(_blank_to_none)]
