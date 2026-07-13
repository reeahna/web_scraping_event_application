from fastapi import APIRouter
from sqlalchemy import text

from app.dependencies import DbSession

router = APIRouter()


@router.get("/health")
def health_check(db: DbSession) -> dict:
    db.execute(text("SELECT 1"))
    return {"status": "ok"}
