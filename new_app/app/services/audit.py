import json
from typing import Any

from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog


def record_audit(
    db: Session,
    *,
    actor_id: int | None,
    action: str,
    entity_type: str | None = None,
    entity_id: int | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    correlation_id: str | None = None,
    ip_address: str | None = None,
    detail: str | None = None,
) -> AuditLog:
    """Record an audit entry. Never pass passwords, session tokens, or other
    secrets in `before`/`after`/`detail` — callers are responsible for
    scrubbing those before calling this."""
    entry = AuditLog(
        user_id=actor_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        before_state=json.dumps(before) if before is not None else None,
        after_state=json.dumps(after) if after is not None else None,
        correlation_id=correlation_id,
        ip_address=ip_address,
        detail=detail,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry
