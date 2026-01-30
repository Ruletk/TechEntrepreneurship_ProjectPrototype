from sqlalchemy import select
from sqlalchemy.orm import Session
from .models import Outlet, GroupMembership, OutletMembership, GroupRole, OutletRole


def get_outlet_group_id(db: Session, outlet_id: int) -> int | None:
    return db.scalar(select(Outlet.group_id).where(Outlet.id == outlet_id))


def has_wide_access(db: Session, user_id: int, group_id: int) -> bool:
    role = db.scalar(
        select(GroupMembership.role).where(
            GroupMembership.user_id == user_id, GroupMembership.group_id == group_id
        )
    )
    return role in (GroupRole.GROUP_OWNER, GroupRole.GROUP_MANAGER)


def has_outlet_access(db: Session, user_id: int, outlet_id: int) -> bool:
    role = db.scalar(
        select(OutletMembership.role).where(
            OutletMembership.user_id == user_id, OutletMembership.outlet_id == outlet_id
        )
    )
    return role in (OutletRole.OUTLET_MANAGER, OutletRole.OUTLET_STAFF)


def can_access_outlet(db: Session, user_id: int, outlet_id: int) -> bool:
    group_id = get_outlet_group_id(db, outlet_id)
    if group_id is None:
        return False
    if has_wide_access(db, user_id, group_id):
        return True
    return has_outlet_access(db, user_id, outlet_id)
