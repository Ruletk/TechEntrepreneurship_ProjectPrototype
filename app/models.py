import enum
from datetime import datetime
from sqlalchemy import (
    String,
    Integer,
    DateTime,
    ForeignKey,
    Enum,
    Numeric,
    UniqueConstraint,
    Boolean,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .db import Base


class GroupRole(str, enum.Enum):
    GROUP_OWNER = "GROUP_OWNER"
    GROUP_MANAGER = "GROUP_MANAGER"


class OutletRole(str, enum.Enum):
    OUTLET_MANAGER = "OUTLET_MANAGER"
    OUTLET_STAFF = "OUTLET_STAFF"


class TxType(str, enum.Enum):
    IN_ = "IN"
    OUT = "OUT"
    ADJUST = "ADJUST"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tg_user_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    active_outlet_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    active_group_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_by = relationship("User")

    outlets = relationship("Outlet", back_populates="group")


class Outlet(Base):
    __tablename__ = "outlets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    group = relationship("Group", back_populates="outlets")


class GroupMembership(Base):
    __tablename__ = "group_memberships"
    __table_args__ = (UniqueConstraint("group_id", "user_id", name="uq_group_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[GroupRole] = mapped_column(Enum(GroupRole), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class OutletMembership(Base):
    __tablename__ = "outlet_memberships"
    __table_args__ = (UniqueConstraint("outlet_id", "user_id", name="uq_outlet_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    outlet_id: Mapped[int] = mapped_column(ForeignKey("outlets.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[OutletRole] = mapped_column(Enum(OutletRole), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


from datetime import datetime
from sqlalchemy import DateTime


class Item(Base):
    __tablename__ = "items"
    __table_args__ = (
        UniqueConstraint("outlet_id", "name", name="uq_outlet_item_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    outlet_id: Mapped[int] = mapped_column(ForeignKey("outlets.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    unit: Mapped[str] = mapped_column(String(16), default="pcs")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, index=True
    )


class StockBalance(Base):
    __tablename__ = "stock_balances"
    __table_args__ = (UniqueConstraint("outlet_id", "item_id", name="uq_outlet_item"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    outlet_id: Mapped[int] = mapped_column(ForeignKey("outlets.id"), index=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), index=True)
    quantity: Mapped[float] = mapped_column(Numeric(12, 3), default=0)


class StockTransaction(Base):
    __tablename__ = "stock_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    outlet_id: Mapped[int] = mapped_column(ForeignKey("outlets.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    type: Mapped[TxType] = mapped_column(Enum(TxType), index=True)
    comment: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class StockTransactionLine(Base):
    __tablename__ = "stock_transaction_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    transaction_id: Mapped[int] = mapped_column(
        ForeignKey("stock_transactions.id"), index=True
    )
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), index=True)
    delta_quantity: Mapped[float] = mapped_column(Numeric(12, 3))


class AuditAction(str, enum.Enum):
    GROUP_CREATED = "GROUP_CREATED"
    OUTLET_CREATED = "OUTLET_CREATED"

    ITEM_CREATED = "ITEM_CREATED"
    ITEM_RENAMED = "ITEM_RENAMED"
    ITEM_UNIT_CHANGED = "ITEM_UNIT_CHANGED"
    ITEM_DELETED = "ITEM_DELETED"

    QTY_SET = "QTY_SET"
    QTY_DELTA = "QTY_DELTA"


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, index=True
    )

    # кто
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)

    # где (группа/точка)
    group_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    outlet_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    # что
    action: Mapped[AuditAction] = mapped_column(Enum(AuditAction), index=True)

    # над чем
    entity_type: Mapped[str] = mapped_column(
        String(32)
    )  # "group" / "outlet" / "item" / "balance"
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # детали (коротко, человекочитаемо)
    details: Mapped[str | None] = mapped_column(String(255), nullable=True)
