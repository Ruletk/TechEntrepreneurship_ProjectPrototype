from sqlalchemy.orm import Session
from .models import AuditLog, AuditAction


def log(
    db: Session,
    user_id: int,
    action: AuditAction,
    entity_type: str,
    entity_id: int | None = None,
    group_id: int | None = None,
    outlet_id: int | None = None,
    details: str | None = None,
):
    db.add(
        AuditLog(
            user_id=user_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            group_id=group_id,
            outlet_id=outlet_id,
            details=details,
        )
    )
