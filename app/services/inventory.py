from datetime import datetime
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..models import Item, StockBalance

SORT_ALPHA = "alpha"
SORT_CREATED = "created"
SORT_UPDATED = "updated"


def list_balances(db: Session, outlet_id: int) -> list[tuple[str, str, float]]:
    rows = db.execute(
        select(Item.name, Item.unit, StockBalance.quantity)
        .join(StockBalance, StockBalance.item_id == Item.id, isouter=True)
        .where(Item.outlet_id == outlet_id, Item.is_active == True)
        .order_by(Item.name.asc())
    ).all()

    result = []
    for name, unit, qty in rows:
        q = float(qty) if qty is not None else 0.0
        result.append((name, unit, q))
    return result


def list_items(db: Session, outlet_id: int, sort: str = SORT_ALPHA) -> list[Item]:
    q = select(Item).where(Item.outlet_id == outlet_id, Item.is_active == True)

    if sort == SORT_CREATED:
        q = q.order_by(Item.created_at.desc(), Item.id.desc())
    elif sort == SORT_UPDATED:
        q = q.order_by(Item.updated_at.desc(), Item.id.desc())
    else:
        q = q.order_by(Item.name.asc())

    return db.scalars(q).all()


def create_item(db: Session, outlet_id: int, name: str, unit: str) -> Item:
    now = datetime.utcnow()
    item = Item(
        outlet_id=outlet_id,
        name=name.strip(),
        unit=unit.strip(),
        created_at=now,
        updated_at=now,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def update_item(
    db: Session, outlet_id: int, item_id: int, name: str, unit: str
) -> Item | None:
    item = db.scalar(
        select(Item).where(
            Item.id == item_id, Item.outlet_id == outlet_id, Item.is_active == True
        )
    )
    if not item:
        return None
    item.name = name.strip()
    item.unit = unit.strip()
    item.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(item)
    return item


def delete_item(db: Session, outlet_id: int, item_id: int) -> bool:
    item = db.scalar(
        select(Item).where(
            Item.id == item_id, Item.outlet_id == outlet_id, Item.is_active == True
        )
    )
    if not item:
        return False
    item.is_active = False
    item.updated_at = datetime.utcnow()
    db.commit()
    return True


def get_or_create_balance(db: Session, outlet_id: int, item_id: int) -> StockBalance:
    bal = db.scalar(
        select(StockBalance).where(
            StockBalance.outlet_id == outlet_id, StockBalance.item_id == item_id
        )
    )
    if bal:
        return bal
    bal = StockBalance(outlet_id=outlet_id, item_id=item_id, quantity=0)
    db.add(bal)
    db.commit()
    db.refresh(bal)
    return bal


def set_quantity(db: Session, outlet_id: int, item_id: int, qty: float) -> float:
    bal = get_or_create_balance(db, outlet_id, item_id)
    bal.quantity = qty
    db.commit()
    return float(bal.quantity)


def add_delta(db: Session, outlet_id: int, item_id: int, delta: float) -> float:
    bal = get_or_create_balance(db, outlet_id, item_id)
    bal.quantity = float(bal.quantity) + delta
    db.commit()
    return float(bal.quantity)


def list_items_with_qty(
    db: Session, outlet_id: int, sort: str = "alpha"
) -> list[tuple[Item, float]]:
    # сортировка как у тебя: alpha/created/updated (если поля есть)
    q = select(Item).where(Item.outlet_id == outlet_id, Item.is_active == True)
    if sort == "created" and hasattr(Item, "created_at"):
        q = q.order_by(Item.created_at.desc(), Item.id.desc())
    elif sort == "updated" and hasattr(Item, "updated_at"):
        q = q.order_by(Item.updated_at.desc(), Item.id.desc())
    else:
        q = q.order_by(Item.name.asc())

    items = db.scalars(q).all()
    result = []
    for it in items:
        bal = db.scalar(
            select(StockBalance.quantity).where(
                StockBalance.outlet_id == outlet_id, StockBalance.item_id == it.id
            )
        )
        qty = float(bal) if bal is not None else 0.0
        result.append((it, qty))
    return result
