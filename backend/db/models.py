"""Nova Studio data model.

Every business table carries tenant_id: the company the row belongs to. All queries go through
helpers that filter on it, so one company can never see another company's data.
Money is stored as Numeric(12, 2) in QAR; quantities as Numeric(12, 2) (m2 for tiles, units for sets).
"""
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db.database import Base

Money = Numeric(12, 2)
Qty = Numeric(12, 2)

ROLES = ("owner", "sales_manager", "salesperson", "warehouse", "viewer")
LEAD_STAGES = ("new", "qualified", "quote_sent", "negotiation", "won", "lost")
LEAD_SOURCES = ("showroom_chat", "shop", "walk_in", "phone", "referral")
LOST_REASONS = ("price", "stock", "competitor", "timing", "no_response")
QUOTE_STATUSES = ("draft", "sent", "accepted", "rejected", "expired")
ORDER_STATUSES = ("confirmed", "delivered", "cancelled")
DISCOUNT_STATUSES = ("auto_approved", "pending", "approved", "rejected")


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(60), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    currency: Mapped[str] = mapped_column(String(3), default="QAR")
    min_margin_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("25"))
    logo_url: Mapped[Optional[str]] = mapped_column(String(255))
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)   # drives the "Demo data" badge
    created_at: Mapped[datetime] = mapped_column(DateTime)


class TenantMixin:
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)


class User(TenantMixin, Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("tenant_id", "email"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(120))
    name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(20))                      # one of ROLES
    password_hash: Mapped[str] = mapped_column(String(100))            # bcrypt, never the password
    discount_limit_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("0"))
    branch: Mapped[Optional[str]] = mapped_column(String(60))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class Customer(TenantMixin, Base):
    __tablename__ = "customers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[Optional[str]] = mapped_column(String(30))
    email: Mapped[Optional[str]] = mapped_column(String(120))
    type: Mapped[str] = mapped_column(String(20), default="retail")    # retail | contractor | designer
    area: Mapped[Optional[str]] = mapped_column(String(60))            # e.g. Lusail, Al Wakra
    created_at: Mapped[datetime] = mapped_column(DateTime)


class Lead(TenantMixin, Base):
    __tablename__ = "leads"
    __table_args__ = (UniqueConstraint("tenant_id", "number"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(20))                    # L-0001, per company
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    owner_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), index=True)  # salesperson
    stage: Mapped[str] = mapped_column(String(20), default="new", index=True)
    source: Mapped[str] = mapped_column(String(20))
    room: Mapped[Optional[str]] = mapped_column(String(40))
    area_m2: Mapped[Optional[Decimal]] = mapped_column(Qty)
    budget: Mapped[Optional[Decimal]] = mapped_column(Money)
    style: Mapped[Optional[str]] = mapped_column(String(40))
    summary: Mapped[Optional[str]] = mapped_column(Text)
    lost_reason: Mapped[Optional[str]] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    stage_changed_at: Mapped[datetime] = mapped_column(DateTime)

    customer: Mapped[Customer] = relationship()
    owner: Mapped[Optional[User]] = relationship()


class Activity(TenantMixin, Base):
    __tablename__ = "activities"
    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), index=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    type: Mapped[str] = mapped_column(String(30))       # created | stage_change | quote_sent | note | call | order
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class Product(TenantMixin, Base):
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("tenant_id", "sku"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(10))                      # tile | set
    category: Mapped[str] = mapped_column(String(40))                  # tile style, or basin/shower/wc
    unit: Mapped[str] = mapped_column(String(5))                       # m2 | set
    price: Mapped[Decimal] = mapped_column(Money)                      # selling price, QAR per unit
    cost_price: Mapped[Decimal] = mapped_column(Money)                 # what the shop pays: makes profit real
    reorder_point: Mapped[Decimal] = mapped_column(Qty, default=Decimal("0"))
    image: Mapped[Optional[str]] = mapped_column(String(120))
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)       # size, finish, slip rating, box size...
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    batches: Mapped[list["Batch"]] = relationship(back_populates="product", order_by="Batch.received_at")


class Batch(TenantMixin, Base):
    """Stock from one production run. Tiles from different runs can differ in shade, so one job
    must be supplied from ONE batch (feature 8)."""
    __tablename__ = "batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    code: Mapped[str] = mapped_column(String(30))
    shade: Mapped[Optional[str]] = mapped_column(String(20))
    received_at: Mapped[datetime] = mapped_column(DateTime)
    received_qty: Mapped[Decimal] = mapped_column(Qty)
    qty_left: Mapped[Decimal] = mapped_column(Qty)

    product: Mapped[Product] = relationship(back_populates="batches")


class StockMovement(TenantMixin, Base):
    __tablename__ = "stock_movements"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    batch_id: Mapped[Optional[int]] = mapped_column(ForeignKey("batches.id"))
    qty: Mapped[Decimal] = mapped_column(Qty)                          # + received, - sold
    reason: Mapped[str] = mapped_column(String(20))                    # receive | sale | adjust
    reference: Mapped[Optional[str]] = mapped_column(String(30))       # order number etc.
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class Quote(TenantMixin, Base):
    __tablename__ = "quotes"
    __table_args__ = (UniqueConstraint("tenant_id", "number"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(20))                    # Q-0001
    lead_id: Mapped[Optional[int]] = mapped_column(ForeignKey("leads.id"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    subtotal: Mapped[Decimal] = mapped_column(Money)
    discount_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("0"))
    discount_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    total: Mapped[Decimal] = mapped_column(Money)
    cost_total: Mapped[Decimal] = mapped_column(Money)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    valid_until: Mapped[Optional[datetime]] = mapped_column(DateTime)

    lines: Mapped[list["QuoteLine"]] = relationship(cascade="all, delete-orphan")


class QuoteLine(Base):
    __tablename__ = "quote_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    quote_id: Mapped[int] = mapped_column(ForeignKey("quotes.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    batch_id: Mapped[Optional[int]] = mapped_column(ForeignKey("batches.id"))
    quantity: Mapped[Decimal] = mapped_column(Qty)                     # m2 (full boxes) or units
    boxes: Mapped[Optional[int]] = mapped_column(Integer)
    unit_price: Mapped[Decimal] = mapped_column(Money)
    unit_cost: Mapped[Decimal] = mapped_column(Money)
    line_total: Mapped[Decimal] = mapped_column(Money)

    product: Mapped[Product] = relationship()


class Order(TenantMixin, Base):
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("tenant_id", "number"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(20))                    # O-0001
    quote_id: Mapped[Optional[int]] = mapped_column(ForeignKey("quotes.id"))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(20), default="confirmed", index=True)
    total: Mapped[Decimal] = mapped_column(Money)
    cost_total: Mapped[Decimal] = mapped_column(Money)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    lines: Mapped[list["OrderLine"]] = relationship(cascade="all, delete-orphan")


class OrderLine(Base):
    __tablename__ = "order_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    batch_id: Mapped[Optional[int]] = mapped_column(ForeignKey("batches.id"))
    quantity: Mapped[Decimal] = mapped_column(Qty)
    unit_price: Mapped[Decimal] = mapped_column(Money)
    unit_cost: Mapped[Decimal] = mapped_column(Money)
    line_total: Mapped[Decimal] = mapped_column(Money)


class DiscountRequest(TenantMixin, Base):
    """Feature 5: every discount is recorded with the margin left after it and who decided."""
    __tablename__ = "discount_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    quote_id: Mapped[int] = mapped_column(ForeignKey("quotes.id"), index=True)
    requested_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    percent: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    margin_after_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    status: Mapped[str] = mapped_column(String(20), index=True)
    decided_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    reason: Mapped[Optional[str]] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime)


class AuditLog(TenantMixin, Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(40), index=True)       # login | discount.approve | price.change ...
    entity: Mapped[Optional[str]] = mapped_column(String(30))
    entity_id: Mapped[Optional[str]] = mapped_column(String(30))
    before: Mapped[Optional[dict]] = mapped_column(JSON)
    after: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)
