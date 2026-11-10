"""Public shop API (/api/shop/...). No login: customers browse, price a cart and send a
quotation request or an order with just their name and phone.

Rules:
- Prices, box rounding and totals are always calculated HERE from the database; the browser
  only sends SKUs and areas/quantities, so a customer can never change a price.
- Exact stock levels stay private; customers see "In stock", "Low stock" or "Out of stock".
- Every request or order becomes a customer + lead in Nova Studio, assigned to a salesperson.
"""
import os
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.auth import audit
from backend.db.database import get_db, local_now
from backend.db.models import (Activity, Batch, Customer, Lead, Order, OrderLine, Product, Quote, QuoteLine,
                               Tenant, User)
from backend.db.numbering import next_number
from backend.offers import project_offers
from backend.ratelimit import RateLimiter
from backend.tools import WASTAGE_BY_LAYOUT, compute_quantity

router = APIRouter(prefix="/api/shop", tags=["Public shop"])
SHOP_TENANT = os.getenv("SHOP_TENANT", "ceramica-nova")   # which company this shop front belongs to
submit_limiter = RateLimiter(max_requests=5, window_seconds=60)
price_limiter = RateLimiter(max_requests=60, window_seconds=60)
QUOTE_VALID_DAYS = 14
DELIVERY_AREAS = ["Doha", "Lusail", "Al Wakra", "Al Rayyan", "The Pearl", "West Bay", "Al Khor", "Umm Salal",
                  "Al Daayen", "Mesaieed"]
ROOMS = ["bathroom", "kitchen", "living room", "majlis", "bedroom", "outdoor", "office", "other"]


def q2(x) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def shop_tenant(db: Session = Depends(get_db)) -> Tenant:
    t = db.scalar(select(Tenant).where(Tenant.slug == SHOP_TENANT))
    if t is None:
        raise HTTPException(status_code=503, detail="The shop is not set up yet (run python -m backend.db.seed).")
    return t


def client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


# ------------------------------------------------------------------ products
def stock_left(p: Product) -> Decimal:
    return sum((b.qty_left for b in p.batches), Decimal(0))


def availability(p: Product) -> str:
    left = stock_left(p)
    if left <= 0:
        return "Out of stock"
    return "Low stock" if left < p.reorder_point else "In stock"


def product_out(p: Product, sales_rank: Optional[int] = None) -> dict:
    a = p.attributes or {}
    out = {"sku": p.sku, "name": p.name, "kind": p.kind, "category": p.category, "unit": p.unit,
           "price": float(p.price), "currency": "QAR", "image": p.image, "availability": availability(p),
           "best_seller_rank": sales_rank}
    if p.kind == "tile":
        out.update({"material": a.get("material"), "finish": a.get("finish"), "size_cm": a.get("size_cm"),
                    "slip_rating": a.get("slip_rating"), "surfaces": a.get("surfaces", []),
                    "locations": a.get("locations", []), "tiles_per_box": a.get("tiles_per_box"),
                    "box_m2": a.get("box_m2")})
    else:
        out.update({"finish": a.get("finish"), "includes": a.get("includes", [])})
    return out


def best_seller_ranks(db: Session, tenant_id: int, days: int = 90) -> dict[int, int]:
    """Rank products by sales value over the last `days` - real order data, not a hand-picked list."""
    since = local_now() - timedelta(days=days)
    rows = db.execute(select(OrderLine.product_id, func.sum(OrderLine.line_total).label("v"))
                      .join(Order, Order.id == OrderLine.order_id)
                      .where(Order.tenant_id == tenant_id, Order.created_at >= since, Order.status != "cancelled")
                      .group_by(OrderLine.product_id).order_by(func.sum(OrderLine.line_total).desc())).all()
    return {pid: i + 1 for i, (pid, _) in enumerate(rows)}


@router.get("/products")
def list_products(kind: Optional[Literal["tile", "set"]] = None, location: Optional[Literal["indoor", "outdoor"]] = None,
                  surface: Optional[Literal["floor", "wall"]] = None, q: Optional[str] = Query(default=None, max_length=60),
                  sort: Literal["popular", "price_asc", "price_desc", "name"] = "popular",
                  tenant: Tenant = Depends(shop_tenant), db: Session = Depends(get_db)):
    ranks = best_seller_ranks(db, tenant.id)
    rows = db.scalars(select(Product).where(Product.tenant_id == tenant.id, Product.active.is_(True))).all()
    out = []
    for p in rows:
        a = p.attributes or {}
        if kind and p.kind != kind:
            continue
        if location and location not in a.get("locations", []):
            continue
        if surface and surface not in a.get("surfaces", []):
            continue
        if q and q.strip().lower() not in " ".join([p.name, p.sku, p.category, str(a.get("finish", "")),
                                                     str(a.get("material", ""))]).lower():
            continue
        out.append(product_out(p, ranks.get(p.id)))
    keys = {"popular": lambda x: (x["best_seller_rank"] or 999, x["name"]), "price_asc": lambda x: x["price"],
            "price_desc": lambda x: -x["price"], "name": lambda x: x["name"]}
    return {"currency": "QAR", "products": sorted(out, key=keys[sort])}


@router.get("/products/{sku}")
def product_detail(sku: str, tenant: Tenant = Depends(shop_tenant), db: Session = Depends(get_db)):
    p = db.scalar(select(Product).where(Product.tenant_id == tenant.id, Product.sku == sku.strip().upper(),
                                        Product.active.is_(True)))
    if p is None:
        raise HTTPException(status_code=404, detail="Product not found.")
    ranks = best_seller_ranks(db, tenant.id)
    others = db.scalars(select(Product).where(Product.tenant_id == tenant.id, Product.active.is_(True),
                                              Product.id != p.id, Product.kind == p.kind)).all()
    loc = set((p.attributes or {}).get("locations", []))
    related = sorted(others, key=lambda o: (not (loc & set((o.attributes or {}).get("locations", []))),
                                            o.category != p.category, ranks.get(o.id, 999)))[:4]
    return {**product_out(p, ranks.get(p.id)), "related": [product_out(o, ranks.get(o.id)) for o in related]}


@router.get("/options")
def options():
    return {"delivery_areas": DELIVERY_AREAS, "rooms": ROOMS}


# ------------------------------------------------------------------ cart pricing
class CartItem(BaseModel):
    sku: str = Field(min_length=3, max_length=20)
    area_m2: Optional[float] = Field(default=None, gt=0, le=5000)          # tiles: the area to cover
    layout: Literal["simple", "diagonal"] = "simple"                         # diagonal laying needs more spare
    quantity: Optional[int] = Field(default=None, ge=1, le=200)             # sets: how many

    @model_validator(mode="after")
    def one_amount(self):
        if (self.area_m2 is None) == (self.quantity is None):
            raise ValueError("give area_m2 for a tile or quantity for a set")
        return self


class CartRequest(BaseModel):
    items: list[CartItem] = Field(min_length=1, max_length=20)


def price_cart(db: Session, tenant: Tenant, items: list[CartItem]) -> dict:
    lines, subtotal, cost = [], Decimal(0), Decimal(0)
    for it in items:
        p = db.scalar(select(Product).where(Product.tenant_id == tenant.id, Product.sku == it.sku.strip().upper(),
                                            Product.active.is_(True)))
        if p is None:
            raise HTTPException(status_code=422, detail=f"Unknown product {it.sku}.")
        if p.kind == "tile":
            if it.area_m2 is None:
                raise HTTPException(status_code=422, detail=f"{p.name} is sold by area: give area_m2.")
            w, l = map(int, p.attributes["size_cm"].split("x"))
            calc = compute_quantity(it.area_m2, w, l, p.attributes["tiles_per_box"],
                                    float(WASTAGE_BY_LAYOUT[it.layout]), float(p.price))
            qty, boxes = Decimal(str(calc.total_purchasable_area_m2)), calc.box_count
            detail = {"area_m2": it.area_m2, "layout": it.layout, "wastage_percent": calc.wastage_percent,
                      "boxes": boxes, "quantity_m2": float(qty)}
        else:
            if it.quantity is None:
                raise HTTPException(status_code=422, detail=f"{p.name} is sold per set: give quantity.")
            qty, boxes, detail = Decimal(it.quantity), None, {"quantity": it.quantity}
        line_total = q2(qty * p.price)
        # Feature 8: can ONE production batch cover this line? (different batches can differ in shade)
        best_batch = max((b.qty_left for b in p.batches), default=Decimal(0))
        lines.append({"sku": p.sku, "name": p.name, "kind": p.kind, "image": p.image, "unit": p.unit,
                      "unit_price": float(p.price), "line_total": float(line_total), **detail,
                      "availability": availability(p), "one_batch": best_batch >= qty,
                      "_product": p, "_qty": qty, "_boxes": boxes})
        subtotal += line_total
        cost += q2(qty * p.cost_price)
    offers = project_offers(float(subtotal))
    return {"currency": "QAR", "lines": lines, "subtotal": float(subtotal), "_cost": cost, "offers": offers}


def public(cart: dict) -> dict:
    return {**{k: v for k, v in cart.items() if not k.startswith("_")},
            "lines": [{k: v for k, v in l.items() if not k.startswith("_")} for l in cart["lines"]]}


@router.post("/cart/price")
def cart_price(req: CartRequest, request: Request, tenant: Tenant = Depends(shop_tenant), db: Session = Depends(get_db)):
    if not price_limiter.allow(client_key(request)):
        raise HTTPException(status_code=429, detail="Too many requests, please slow down.")
    return public(price_cart(db, tenant, req.items))


# ------------------------------------------------------------------ quotation request / order
class CheckoutRequest(CartRequest):
    type: Literal["quote", "order"]
    name: str = Field(min_length=2, max_length=80)
    phone: str = Field(min_length=8, max_length=20)
    email: Optional[str] = Field(default=None, max_length=120)
    delivery_area: str
    room: Optional[str] = None
    notes: Optional[str] = Field(default=None, max_length=500)

    @field_validator("phone")
    @classmethod
    def phone_ok(cls, v: str) -> str:
        digits = "".join(c for c in v if c.isdigit())
        if not 8 <= len(digits) <= 15 or any(c not in "+0123456789 -()" for c in v):
            raise ValueError("enter a valid phone number")
        return v.strip()

    @field_validator("email")
    @classmethod
    def email_ok(cls, v):
        if v and ("@" not in v or " " in v.strip()):
            raise ValueError("enter a valid email or leave it empty")
        return v.strip().lower() if v else None

    @field_validator("delivery_area")
    @classmethod
    def area_ok(cls, v):
        if v not in DELIVERY_AREAS:
            raise ValueError("choose a delivery area from the list")
        return v

    @field_validator("room")
    @classmethod
    def room_ok(cls, v):
        if v and v not in ROOMS:
            raise ValueError("choose a room from the list")
        return v


def pick_salesperson(db: Session, tenant_id: int) -> Optional[User]:
    """New leads go to the active salesperson with the fewest open leads (fair, automatic)."""
    sellers = db.scalars(select(User).where(User.tenant_id == tenant_id, User.role == "salesperson",
                                            User.active.is_(True)).order_by(User.id)).all()
    if not sellers:
        return None
    open_counts = dict(db.execute(select(Lead.owner_id, func.count()).where(
        Lead.tenant_id == tenant_id, Lead.stage.not_in(["won", "lost"])).group_by(Lead.owner_id)).all())
    return min(sellers, key=lambda u: open_counts.get(u.id, 0))


def phone_key(phone: str) -> str:
    digits = "".join(c for c in phone if c.isdigit())
    return digits[-8:]                          # Qatar numbers are 8 digits; ignores +974 / 00974


@router.post("/checkout")
def checkout(req: CheckoutRequest, request: Request, tenant: Tenant = Depends(shop_tenant),
             db: Session = Depends(get_db)):
    if not submit_limiter.allow(client_key(request)):
        raise HTTPException(status_code=429, detail="Too many requests, please try again in a minute.")
    cart = price_cart(db, tenant, req.items)
    now = local_now()

    # Returning customer? Match on the phone number within this company only.
    customer = next((c for c in db.scalars(select(Customer).where(Customer.tenant_id == tenant.id))
                     if c.phone and phone_key(c.phone) == phone_key(req.phone)), None)
    if customer is None:
        customer = Customer(tenant_id=tenant.id, name=req.name.strip(), phone=req.phone, email=req.email,
                            type="retail", area=req.delivery_area, created_at=now)
        db.add(customer)
        db.flush()

    owner = pick_salesperson(db, tenant.id)
    total_m2 = sum(l["_qty"] for l in cart["lines"] if l["kind"] == "tile")
    what = "order" if req.type == "order" else "quotation request"
    summary = (f"Online {what}: {len(cart['lines'])} item(s), QAR {cart['subtotal']:,.2f}"
               + (f", {total_m2:g} m² of tiles" if total_m2 else "")
               + (f", for a {req.room}" if req.room else "") + f". Delivery to {req.delivery_area}."
               + (f" Note: {req.notes.strip()}" if req.notes and req.notes.strip() else ""))
    lead = Lead(tenant_id=tenant.id, number=next_number(db, Lead, tenant.id, "L"), customer_id=customer.id,
                owner_id=owner.id if owner else None, stage="quote_sent", source="shop", room=req.room,
                area_m2=q2(total_m2) if total_m2 else None, budget=None, summary=summary,
                created_at=now, stage_changed_at=now)
    db.add(lead)
    db.flush()

    quote = Quote(tenant_id=tenant.id, number=next_number(db, Quote, tenant.id, "Q"), lead_id=lead.id,
                  customer_id=customer.id, created_by=None, status="accepted" if req.type == "order" else "sent",
                  subtotal=q2(cart["subtotal"]), discount_pct=Decimal(0), discount_amount=Decimal(0),
                  total=q2(cart["subtotal"]), cost_total=q2(cart["_cost"]), created_at=now, sent_at=now,
                  valid_until=now + timedelta(days=QUOTE_VALID_DAYS))
    for l in cart["lines"]:
        p = l["_product"]
        quote.lines.append(QuoteLine(product_id=p.id, quantity=l["_qty"], boxes=l["_boxes"], unit_price=p.price,
                                     unit_cost=p.cost_price, line_total=q2(l["line_total"])))
    db.add(quote)
    db.flush()

    order = None
    if req.type == "order":
        # No online payment: the order waits until a salesperson calls to confirm delivery and payment.
        order = Order(tenant_id=tenant.id, number=next_number(db, Order, tenant.id, "O"), quote_id=quote.id,
                      customer_id=customer.id, created_by=None, status="pending", total=quote.total,
                      cost_total=quote.cost_total, created_at=now)
        for l in cart["lines"]:
            p = l["_product"]
            # Feature 8: suggest the oldest batch that covers the whole line alone (stock is taken on confirmation).
            batch = next((b for b in p.batches if b.qty_left >= l["_qty"]), None)
            order.lines.append(OrderLine(product_id=p.id, batch_id=batch.id if batch else None, quantity=l["_qty"],
                                         unit_price=p.price, unit_cost=p.cost_price, line_total=q2(l["line_total"])))
        db.add(order)
        db.flush()

    split = [l["name"] for l in cart["lines"] if not l["one_batch"]]
    text = (f"Customer placed order {order.number} online (QAR {order.total:,.2f}). Call to confirm delivery and payment."
            if order else f"Customer requested quotation {quote.number} online (QAR {quote.total:,.2f}).")
    if split:
        text += f" Check shade: no single batch covers {', '.join(split)}."
    db.add(Activity(tenant_id=tenant.id, lead_id=lead.id, user_id=None, type="created", text=text, created_at=now))
    audit(db, tenant.id, f"shop.{req.type}", entity="lead", entity_id=lead.number,
          after={"quote": quote.number, "order": order.number if order else None, "total": float(quote.total)})
    db.commit()

    return {"type": req.type, "reference": order.number if order else quote.number, "quote_number": quote.number,
            "order_number": order.number if order else None, "valid_until": quote.valid_until.date().isoformat(),
            "cart": public(cart), "assigned_to": owner.name.split()[0] if owner else None}
