"""Health, liveness, and readiness endpoints (Phase 18).

* `/health` and `/health/live` — cheap liveness (the process is up).
* `/health/ready` — readiness: database connectivity, scheduler leader
  freshness, AI provider health, and the production-readiness checklist. It
  returns 503 only when the database is unreachable; production-config blockers
  are reported in the body (not a 503) so a dev/staging box is still usable
  while operators can see exactly what remains before a real deployment.

Nothing secret is ever returned.
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import get_settings
from app.core.production_checks import production_blockers
from app.dependencies import DbSession

router = APIRouter()


@router.get("/health")
def health_check(db: DbSession) -> dict:
    db.execute(text("SELECT 1"))
    return {"status": "ok"}


@router.get("/health/live")
def liveness() -> dict:
    return {"status": "alive"}


@router.get("/health/ready")
def readiness(db: DbSession) -> JSONResponse:
    settings = get_settings()
    checks: dict[str, object] = {}

    try:
        db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["database"] = f"error: {type(exc).__name__}"

    try:
        from app.services.scheduler import scheduler_health

        health = scheduler_health(db)
        checks["scheduler"] = {
            "leader_present": health.leader_holder is not None,
            "leader_fresh": health.leader_is_fresh,
            "running": health.running_count,
        }
    except Exception as exc:  # noqa: BLE001
        checks["scheduler"] = f"error: {type(exc).__name__}"

    try:
        from app.services.ai.provider import usage_status

        status = usage_status()
        checks["ai_provider"] = {"enabled": status.enabled, "healthy": status.healthy}
    except Exception as exc:  # noqa: BLE001
        checks["ai_provider"] = f"error: {type(exc).__name__}"

    blockers = production_blockers(settings)
    checks["production_blockers"] = [
        {"code": b.code, "severity": b.severity, "message": b.message} for b in blockers
    ]

    ready = checks["database"] == "ok"
    return JSONResponse(
        {"status": "ready" if ready else "not_ready", "checks": checks},
        status_code=200 if ready else 503,
    )
