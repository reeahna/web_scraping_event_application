from fastapi import Request

from app.core.exceptions import AppError


async def reject_unexpected_form_fields(request: Request, allowed_fields: set[str]) -> None:
    submitted = await request.form()
    unexpected = set(submitted) - allowed_fields
    if unexpected:
        raise AppError("Unexpected form fields were submitted", status_code=422)
