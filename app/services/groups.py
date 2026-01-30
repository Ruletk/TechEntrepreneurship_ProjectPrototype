from sqlalchemy import select
from sqlalchemy.orm import Session
from ..models import Group, Outlet, GroupMembership, GroupRole


def create_group(db: Session, creator_user_id: int, name: str) -> Group:
    g = Group(name=name, created_by_user_id=creator_user_id)
    db.add(g)
    db.flush()  # чтобы появился id

    # создатель = wide-owner
    gm = GroupMembership(
        group_id=g.id, user_id=creator_user_id, role=GroupRole.GROUP_OWNER
    )
    db.add(gm)
    db.commit()
    db.refresh(g)
    return g


def user_groups(db: Session, user_id: int) -> list[Group]:
    group_ids = db.scalars(
        select(GroupMembership.group_id).where(GroupMembership.user_id == user_id)
    ).all()
    if not group_ids:
        return []
    return db.scalars(select(Group).where(Group.id.in_(group_ids))).all()


def create_outlet(
    db: Session, group_id: int, name: str, address: str | None = None
) -> Outlet:
    o = Outlet(group_id=group_id, name=name, address=address)
    db.add(o)
    db.commit()
    db.refresh(o)
    return o
