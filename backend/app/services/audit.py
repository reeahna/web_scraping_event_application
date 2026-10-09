import json
from typing import Any

from sqlalchemy.orm import Session

from app.core.auto_onboarding import ACTOR_SYSTEM, ACTOR_USER, SYSTEM_ACTOR_LABEL
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
    actor_type: str = ACTOR_USER,
    actor_label: str | None = None,
) -> AuditLog:
    """Record an audit entry. Never pass passwords, session tokens, or other
    secrets in `before`/`after`/`detail` — callers are responsible for
    scrubbing those before calling this.

    `actor_type` defaults to "user", so every existing call site keeps its
    current meaning. Automatic actions use `record_system_audit` instead.
    """
    entry = AuditLog(
        user_id=actor_id,
        actor_type=actor_type,
        actor_label=actor_label,
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


def record_system_audit(
    db: Session,
    *,
    action: str,
    entity_type: str | None = None,
    entity_id: int | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    correlation_id: str | None = None,
    detail: str | None = None,
    actor_label: str = SYSTEM_ACTOR_LABEL,
) -> AuditLog:
    """An action the application took on its own authority, because an active
    administrator-authored policy permitted it.

    `actor_id` is deliberately None: no User performed this, and attributing
    it to the submitting user would misrepresent both what happened and whose
    permissions authorized it. The submitter is recorded separately on the
    decision record.
    """
    return record_audit(
        db,
        actor_id=None,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        before=before,
        after=after,
        correlation_id=correlation_id,
        detail=detail,
        actor_type=ACTOR_SYSTEM,
        actor_label=actor_label,
    )
