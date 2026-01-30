from sqlalchemy import select
from sqlalchemy.orm import Session
from ..models import User


def get_or_create_user(db: Session, tg_user_id: int, name: str) -> User:
    user = db.scalar(select(User).where(User.tg_user_id == tg_user_id))
    if user:
        # обновим имя, если поменялось
        if name and user.name != name:
            user.name = name
            db.commit()
        return user

    user = User(tg_user_id=tg_user_id, name=name or "")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user
