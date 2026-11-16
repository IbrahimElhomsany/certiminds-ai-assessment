"""Public shop API (/api/shop/...). No login: customers browse, price a cart and send a
quotation request or an order with just their name and phone.

Rules:
- Prices, box rounding and totals are always calculated HERE from the database; the browser
  only sends SKUs and areas/quantities, so a customer can never change a price.
- Exact stock levels stay private; customers see "In stock", "Low stock" or "Out of stock".
- Every request or order becomes a customer + lead in Nova Studio, assigned to a salesperson.
"""
import os
import logging
import math
import secrets
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend import crm_bridge
from backend.auth import audit
from backend.db.database import get_db, local_now
from backend import payments
from backend.db.models import (Activity, Batch, Customer, Lead, Order, OrderLine, Product, Quote, QuoteLine,
                               ShopCheckout, Tenant, User)
from backend.db.numbering import next_number
from backend.offers import project_offers
from backend.ratelimit import RateLimiter
from backend.tools import WASTAGE_BY_LAYOUT, compute_quantity

log = logging.getLogger("nova.shop")
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
    elif p.kind == "acc":
        out.update({"group": p.category, "colour": a.get("colour"), "hex": a.get("hex"), "bag_kg": a.get("bag_kg")})
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
def list_products(kind: Optional[Literal["tile", "set", "acc"]] = None, location: Optional[Literal["indoor", "outdoor"]] = None,
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


@router.get("/products/{sku}/estimate")
def accessory_estimate(sku: str, area_m2: float = Query(gt=0, le=5000), size_cm: str = Query(pattern=r"^\d{2,3}x\d{2,3}$"),
                       tenant: Tenant = Depends(shop_tenant), db: Session = Depends(get_db)):
    """How many bags of a grout, adhesive or spacers for an area of tiles of a given size (same rules as checkout)."""
    from backend import accessories
    p = db.scalar(select(Product).where(Product.tenant_id == tenant.id, Product.sku == sku.strip().upper(),
                                        Product.kind == "acc", Product.active.is_(True)))
    if p is None or p.sku not in accessories.BY_SKU:
        raise HTTPException(status_code=404, detail="Product not found.")
    w, l = map(int, size_cm.split("x"))
    a = accessories.BY_SKU[p.sku]
    if a["group"] == "grout":
        kg = accessories.grout_kg_per_m2(w, l) * area_m2
        n = accessories.bags(kg, a["bag_kg"])
        why = (f"About {kg:.1f} kg for {area_m2:g} m² of {size_cm} cm tiles with {accessories.JOINT_MM} mm joints, "
               f"so {n} bag{'s' if n > 1 else ''} with spare.")
    elif a["group"] == "adhesive":
        per = accessories.adhesive_kg_per_m2(w, l)
        n = accessories.bags(per * area_m2, a["bag_kg"])
        why = f"About {per:g} kg per m² for {size_cm} cm tiles, so {per * area_m2:.0f} kg for {area_m2:g} m²: {n} bag{'s' if n > 1 else ''}."
    else:
        n = max(1, math.ceil(area_m2 / 10))
        why = f"About one bag per 10 m², so {n} bag{'s' if n > 1 else ''} for {area_m2:g} m²."
    return {"sku": p.sku, "quantity": n, "why": why, "line_total": float(q2(p.price * n))}


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
    from backend.crm import picks                       # D3: learned from quotations and the visualiser
    learned = picks.learned(db, tenant.id)["pairs"].get(p.sku, [])
    by_sku = {o.sku: o for o in others}
    by_sku.update({o.sku: o for o in db.scalars(select(Product).where(Product.tenant_id == tenant.id, Product.active.is_(True),
                                                                      Product.sku.in_([x["sku"] for x in learned])))})
    often = [{**product_out(by_sku[x["sku"]], ranks.get(by_sku[x["sku"]].id)), "together": x["together"]}
             for x in learned if x["sku"] in by_sku]
    shown = {o["sku"] for o in often}
    return {**product_out(p, ranks.get(p.id)), "often_with": often,
            "related": [product_out(o, ranks.get(o.id)) for o in related if o.sku not in shown]}


@router.get("/options")
def options():
    today = local_now().date()
    return {"delivery_areas": DELIVERY_AREAS, "rooms": ROOMS,
            "card_payment": payments.enabled(), "card_test_mode": payments.test_mode(),
            "delivery_dates": [d.isoformat() for d in delivery_days(today)]}


def delivery_days(today: date) -> list[date]:
    """We deliver Saturday to Thursday, from the day after tomorrow, up to 30 days ahead."""
    return [d for d in (today + timedelta(days=i) for i in range(2, 31)) if d.weekday() != 4]


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


@router.post("/cart/suggest")
def cart_suggest(req: CartRequest, request: Request, tenant: Tenant = Depends(shop_tenant), db: Session = Depends(get_db)):
    """'Complete your project': grout, adhesive and spacers worked out from the tiles in the cart, plus tiles
    customers often choose with them (learned, D3). Things already in the cart are left out."""
    if not price_limiter.allow(client_key(request)):
        raise HTTPException(status_code=429, detail="Too many requests, please slow down.")
    from backend import accessories
    from backend.crm import picks
    cart = price_cart(db, tenant, req.items)
    in_cart = {l["sku"] for l in cart["lines"]}
    tiles = []
    for l in cart["lines"]:
        if l["kind"] == "tile":
            p = l["_product"]
            w, ln = map(int, p.attributes["size_cm"].split("x"))
            tiles.append({"sku": p.sku, "name": p.name, "style": p.category, "width_cm": w, "length_cm": ln, "m2": l["area_m2"]})
    prods = {p.sku: p for p in db.scalars(select(Product).where(Product.tenant_id == tenant.id, Product.active.is_(True)))}
    out = []
    for n in accessories.needs_for(tiles):
        p = prods.get(n["sku"])
        if p is None or (n["group"] != "grout" and n["sku"] in in_cart) or (n["group"] == "grout" and
                                                                             any(s.startswith("GR") for s in in_cart)):
            continue
        out.append({**product_out(p), "suggest_quantity": n["quantity"], "why": n["why"], "reason": n["group"],
                    "alternatives": [product_out(prods[a["sku"]]) for a in accessories.ACCESSORIES
                                     if a["group"] == n["group"] and a["sku"] != p.sku and a["sku"] in prods]
                                    if n["group"] == "grout" else []})
    learned = picks.learned(db, tenant.id)["pairs"]
    seen = set()
    for l in cart["lines"]:
        for x in learned.get(l["sku"], []):
            p = prods.get(x["sku"])
            if p is None or p.sku in in_cart or p.sku in seen or p.kind == "acc":
                continue
            seen.add(p.sku)
            out.append({**product_out(p), "suggest_quantity": None if p.kind == "tile" else 1, "reason": "pair",
                         "why": f"Customers who chose {l['name']} often add this ({x['together']} projects so far)."
                                + (" Enter the area to see the price." if p.kind == "tile" else "")})
            if len(seen) >= 3:
                break
    return {"currency": "QAR", "suggestions": out}


# ------------------------------------------------------------------ quotation request / order
class CheckoutRequest(CartRequest):
    type: Literal["quote", "order"]
    name: str = Field(min_length=2, max_length=80)
    phone: str = Field(min_length=8, max_length=20)
    email: Optional[str] = Field(default=None, max_length=120)
    delivery_area: str
    room: Optional[str] = None
    notes: Optional[str] = Field(default=None, max_length=500)
    chat_session_id: Optional[str] = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{4,64}$")   # came from the AI chat
    chat_visitor_id: Optional[str] = Field(default=None, pattern=r"^USR-\d{3,10}$")
    # an order also needs the Qatar address (zone / street / building), a delivery day and how to pay
    zone: Optional[str] = Field(default=None, pattern=r"^\d{1,3}$")
    street: Optional[str] = Field(default=None, min_length=1, max_length=60)
    building: Optional[str] = Field(default=None, min_length=1, max_length=20)
    unit: Optional[str] = Field(default=None, max_length=30)
    delivery_date: Optional[date] = None
    payment: Optional[Literal["card", "on_delivery"]] = None

    @model_validator(mode="after")
    def order_needs_address(self):
        if self.type == "order":
            if not (self.zone and self.street and self.building):
                raise ValueError("enter the delivery address: zone, street and building number")
            if self.delivery_date is None or self.delivery_date not in delivery_days(local_now().date()):
                raise ValueError("choose a delivery day from the list (Saturday to Thursday, from the day after tomorrow)")
            if self.payment is None:
                raise ValueError("choose how you would like to pay")
        return self

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
    if req.type == "order" and req.payment == "card" and not payments.enabled():
        raise HTTPException(status_code=422, detail="Card payment is not available right now. Please choose pay on delivery.")
    cart = price_cart(db, tenant, req.items)
    now = local_now()

    total_m2 = sum(l["_qty"] for l in cart["lines"] if l["kind"] == "tile")
    what = "order" if req.type == "order" else "quotation request"
    summary = (f"Online {what}: {len(cart['lines'])} item(s), QAR {cart['subtotal']:,.2f}"
               + (f", {total_m2:g} m² of tiles" if total_m2 else "")
               + (f", for a {req.room}" if req.room else "") + f". Delivery to {req.delivery_area}."
               + (f" Note: {req.notes.strip()}" if req.notes and req.notes.strip() else ""))

    # Came from a Ceramica Nova AI chat? Continue THAT lead, so staff see one journey, chat to order.
    lead = None
    if req.chat_session_id and req.chat_visitor_id:
        row, lead = crm_bridge.chat_lead_for(db, tenant, req.chat_session_id, req.chat_visitor_id)
        if lead is not None:
            customer = crm_bridge.link_customer(db, tenant, row, lead, req.name.strip(), req.phone, req.email,
                                                req.delivery_area)
            if lead.stage not in ("won", "lost"):
                lead.stage, lead.stage_changed_at = "quote_sent", now
            lead.room = lead.room or req.room
            lead.summary = f"{lead.summary or ''} Then: {summary}".strip()
            owner = lead.owner

    if lead is None:
        # Returning customer? Match on the phone number within this company only.
        customer = next((c for c in db.scalars(select(Customer).where(Customer.tenant_id == tenant.id))
                         if c.phone and phone_key(c.phone) == phone_key(req.phone)), None)
        if customer is None:
            customer = Customer(tenant_id=tenant.id, name=req.name.strip(), phone=req.phone, email=req.email,
                                type="retail", area=req.delivery_area, created_at=now)
            db.add(customer)
            db.flush()
        owner = pick_salesperson(db, tenant.id)
        lead = Lead(tenant_id=tenant.id, number=next_number(db, Lead, tenant.id, "L"), customer_id=customer.id,
                    owner_id=owner.id if owner else None, stage="quote_sent", source="shop", room=req.room,
                    area_m2=q2(total_m2) if total_m2 else None, budget=None, summary=summary,
                    created_at=now, stage_changed_at=now)
        db.add(lead)
        db.flush()

    quote = Quote(tenant_id=tenant.id, number=next_number(db, Quote, tenant.id, "Q"), lead_id=lead.id,
                  customer_id=customer.id, created_by=None, channel="shop",
                  status="accepted" if req.type == "order" else "sent",
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
        # The order is saved first; a card payment (Tap) is started after, and the salesperson still calls to confirm.
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
    fee = Decimal(str(cart["offers"]["delivery_fee_qar"]))
    chk = ShopCheckout(tenant_id=tenant.id, token=secrets.token_urlsafe(24), kind=req.type, quote_id=quote.id,
                       order_id=order.id if order else None, name=req.name.strip(), phone=req.phone, email=req.email,
                       delivery_area=req.delivery_area, zone=req.zone, street=req.street, building=req.building,
                       unit=(req.unit or "").strip() or None,
                       delivery_date=datetime.combine(req.delivery_date, time(9)) if req.delivery_date else None,
                       delivery_fee=q2(fee), amount_due=q2(quote.total + fee), payment_method=req.payment,
                       payment_status="unpaid", created_at=now)
    db.add(chk)
    if order:
        text += (f" Deliver on {req.delivery_date:%a %d %b} to zone {req.zone}, street {req.street}, building {req.building}"
                 f"{', ' + chk.unit if chk.unit else ''} ({req.delivery_area}). "
                 + ("Paying online by card." if req.payment == "card" else "Pays on delivery."))
    db.add(Activity(tenant_id=tenant.id, lead_id=lead.id, user_id=None, type="created", text=text, created_at=now))
    audit(db, tenant.id, f"shop.{req.type}", entity="lead", entity_id=lead.number,
          after={"quote": quote.number, "order": order.number if order else None, "total": float(quote.total)})
    db.commit()

    pay = start_card_payment(db, chk, request) if order and req.payment == "card" else {}
    return {"type": req.type, "reference": order.number if order else quote.number, "quote_number": quote.number,
            "order_number": order.number if order else None, "valid_until": quote.valid_until.date().isoformat(),
            "cart": public(cart), "assigned_to": owner.name.split()[0] if owner else None,
            "lead_from_chat": lead.source == "showroom_chat", "token": chk.token, **pay}


# ------------------------------------------------------------------ card payment (Tap) and the receipt
def _base(request: Request) -> str:
    return (os.getenv("PUBLIC_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or str(request.base_url)).rstrip("/")


def _checkout(db: Session, token: str) -> ShopCheckout:
    chk = db.scalar(select(ShopCheckout).where(ShopCheckout.token == token)) if 16 <= len(token) <= 48 else None
    if chk is None:
        raise HTTPException(status_code=404, detail="Not found.")
    return chk


def start_card_payment(db: Session, chk: ShopCheckout, request: Request) -> dict:
    """Ask Tap for a payment page. The order is already saved, so if this fails the customer can try again or
    choose to pay on delivery from the receipt page."""
    order = db.get(Order, chk.order_id)
    base = _base(request)
    try:
        ch = payments.create_charge(chk.amount_due, order.number, f"Ceramica Nova order {order.number}", chk.name,
                                    chk.phone, chk.email, f"{base}/api/shop/pay/return/{chk.token}",
                                    f"{base}/api/shop/pay/tap/webhook" if base.startswith("https://") else None)
    except payments.PaymentError as e:
        chk.payment_status = "failed"
        db.commit()
        return {"payment_error": str(e)}
    chk.provider, chk.provider_ref, chk.payment_status, chk.payment_method = "tap", ch["id"], "pending", "card"
    db.commit()
    return {"payment_url": ch["url"]}


def sync_payment(db: Session, chk: ShopCheckout) -> Optional[dict]:
    """Ask Tap itself what happened to the charge. Only Tap's answer, for the right amount and order, marks it paid.
    Returns Tap's charge (status, response code and message) so the receipt can say why a payment failed."""
    if chk.payment_status == "paid" or chk.provider != "tap" or not chk.provider_ref:
        return None
    try:
        c = payments.fetch_charge(chk.provider_ref)
    except payments.PaymentError:
        return None
    order = db.get(Order, chk.order_id)
    if c["status"] == payments.PAID and c["amount"] == chk.amount_due and c["currency"] == "QAR" \
            and c["reference"] in (None, order.number):
        chk.payment_status, chk.paid_at, chk.card = "paid", local_now(), c["card"]
        quote = db.get(Quote, chk.quote_id)
        db.add(Activity(tenant_id=chk.tenant_id, lead_id=quote.lead_id, user_id=None, type="order",
                        text=f"Paid online by card: QAR {chk.amount_due:,.2f} for {order.number} (Tap {chk.provider_ref}).",
                        created_at=local_now()))
        audit(db, chk.tenant_id, "shop.paid", entity="order", entity_id=order.number,
              after={"amount": float(chk.amount_due), "provider": "tap", "charge": chk.provider_ref})
    elif c["status"] == payments.PAID:
        log.warning("tap charge %s captured but does not match order %s (amount %s %s, ref %s; expected %s QAR)",
                    chk.provider_ref, order.number, c["amount"], c["currency"], c["reference"], chk.amount_due)
    elif c["status"] in payments.FAILED:
        chk.payment_status = "failed"
    db.commit()
    return c


@router.get("/pay/return/{token}", include_in_schema=False)
def pay_return(token: str, tap_id: Optional[str] = None, db: Session = Depends(get_db)):
    """Tap sends the customer back here. We check with Tap, then show the receipt."""
    chk = _checkout(db, token)
    if tap_id and tap_id != chk.provider_ref:
        tap_id = None                            # a charge id that is not this order's is ignored
    sync_payment(db, chk)
    return RedirectResponse(f"/#/receipt/{chk.token}", status_code=303)


@router.post("/pay/tap/webhook", include_in_schema=False)
async def tap_webhook(request: Request, db: Session = Depends(get_db)):
    """Tap's server-to-server notice. The body is not trusted: we only take the charge id and ask Tap again."""
    try:
        cid = str((await request.json()).get("id", ""))[:80]
    except Exception:
        return {"ok": False}
    chk = db.scalar(select(ShopCheckout).where(ShopCheckout.provider_ref == cid)) if cid.startswith("chg_") else None
    if chk:
        sync_payment(db, chk)
    return {"ok": True}


@router.post("/receipt/{token}/pay")
def pay_again(token: str, request: Request, db: Session = Depends(get_db)):
    chk = _checkout(db, token)
    if chk.kind != "order" or chk.payment_status == "paid":
        raise HTTPException(status_code=409, detail="Nothing to pay.")
    if db.get(Order, chk.order_id).status == "cancelled":
        raise HTTPException(status_code=409, detail="This order was cancelled.")
    if not payments.enabled():
        raise HTTPException(status_code=422, detail="Card payment is not available right now.")
    if not submit_limiter.allow(client_key(request)):
        raise HTTPException(status_code=429, detail="Too many requests, please try again in a minute.")
    out = start_card_payment(db, chk, request)
    if "payment_error" in out:
        raise HTTPException(status_code=502, detail=out["payment_error"])
    return out


@router.post("/receipt/{token}/on-delivery")
def pay_on_delivery(token: str, db: Session = Depends(get_db)):
    chk = _checkout(db, token)
    if chk.kind != "order" or chk.payment_status == "paid":
        raise HTTPException(status_code=409, detail="Nothing to change.")
    sync_payment(db, chk)
    if chk.payment_status == "paid":
        return receipt_data(db, chk)
    chk.payment_method, chk.payment_status = "on_delivery", "unpaid"
    db.commit()
    return receipt_data(db, chk)


def receipt_data(db: Session, chk: ShopCheckout) -> dict:
    quote = db.get(Quote, chk.quote_id)
    order = db.get(Order, chk.order_id) if chk.order_id else None
    lead = db.get(Lead, quote.lead_id) if quote.lead_id else None
    tenant = db.get(Tenant, chk.tenant_id)
    lines = []
    for ql in quote.lines:
        p = ql.product
        a = p.attributes or {}
        lines.append({"sku": p.sku, "name": p.name, "kind": p.kind, "unit": p.unit, "size_cm": a.get("size_cm"),
                      "boxes": ql.boxes, "quantity": float(ql.quantity), "unit_price": float(ql.unit_price),
                      "line_total": float(ql.line_total), "image": p.image})
    return {"kind": chk.kind, "company": {"name": tenant.name, "demo": tenant.is_demo},
            "number": order.number if order else quote.number, "quote_number": quote.number,
            "order_status": order.status if order else None, "created_at": chk.created_at,
            "valid_until": quote.valid_until, "salesperson": lead.owner.name.split()[0] if lead and lead.owner else None,
            "customer": {"name": chk.name, "phone": chk.phone, "email": chk.email},
            "address": {"area": chk.delivery_area, "zone": chk.zone, "street": chk.street, "building": chk.building,
                        "unit": chk.unit},
            "delivery_date": chk.delivery_date.date().isoformat() if chk.delivery_date else None,
            "lines": lines, "subtotal": float(quote.total), "delivery_fee": float(chk.delivery_fee),
            "total": float(chk.amount_due),
            "payment": {"method": chk.payment_method, "status": chk.payment_status, "card": chk.card,
                        "paid_at": chk.paid_at, "reference": chk.provider_ref, "test_mode": payments.test_mode(),
                        "can_pay_card": payments.enabled() and chk.kind == "order" and chk.payment_status != "paid"
                                        and (order is None or order.status != "cancelled")}}


@router.get("/receipt/{token}")
def receipt(token: str, db: Session = Depends(get_db)):
    chk = _checkout(db, token)
    c = sync_payment(db, chk) if chk.payment_status in ("pending", "failed") else None
    d = receipt_data(db, chk)
    if c and chk.payment_status != "paid" and (c.get("message") or c.get("status")):
        # Tap's own reason, e.g. "Declined, Insufficient Funds (505)", so the customer and staff know what happened
        d["payment"]["reason"] = f"{c.get('message') or c['status'].title()}" + (f" (code {c['code']})" if c.get("code") else "")
    return d


@router.get("/receipt/{token}/pdf")
def receipt_pdf(token: str, db: Session = Depends(get_db)):
    from backend.receipt_pdf import build_receipt_pdf
    chk = _checkout(db, token)
    d = receipt_data(db, chk)
    name = f"Ceramica-Nova-{d['number']}.pdf"
    return Response(build_receipt_pdf(d), media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})
