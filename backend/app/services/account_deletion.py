"""A user deleting their own account.

The user row is really deleted: every table that points at a user either
removes its rows with it (sessions, roles, saved events, follows, alert
preferences, linked sign-ins, notifications) or blanks the reference (the audit
log, and the admin records of who approved or corrected what). The audit log
also carries the email address as text in some entries (a failed sign-in records
the address typed), so it is replaced there too.
"""

from sqlalchemy import func, update
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.user import User
from app.services.audit import record_audit
from app.services.rbac import assert_not_last_super_admin

REDACTED = "[deleted account]"


def delete_account(
    db: Session,
    user: User,
    *,
    correlation_id: str | None = None,
    ip_address: str | None = None,
) -> None:
    """Raises AppError(403) for the last active Super Administrator, who would
    otherwise leave the site with nobody able to administer it."""
    assert_not_last_super_admin(db, user)
    user_id, email = user.id, user.email

    for column in (AuditLog.detail, AuditLog.before_state, AuditLog.after_state):
        db.execute(
            update(AuditLog)
            .where(column.contains(email))
            .values({column.key: func.replace(column, email, REDACTED)})
        )

    db.delete(user)
    db.commit()

    # Recorded after the delete, with no actor, so it names nobody.
    record_audit(
        db,
        actor_id=None,
        action="account_deleted",
        entity_type="user",
        entity_id=user_id,
        detail="The account holder deleted their account.",
        correlation_id=correlation_id,
        ip_address=ip_address,
    )
