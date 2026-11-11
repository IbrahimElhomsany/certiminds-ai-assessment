"""Nova Studio admin API (/api/admin/...). Staff only: every route needs a valid login token.

Two rules hold on every route:
1. Company isolation: queries always filter on the logged-in user's tenant_id.
2. Role permissions: checked here with require(...), never only in the screen.
"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.auth import (PERMISSIONS, authenticate, create_token, current_user, has_permission, require)
from backend.db.database import get_db
from backend.db.models import LEAD_STAGES, AuditLog, Lead, Tenant, User
from backend.ratelimit import RateLimiter

router = APIRouter(prefix="/api/admin", tags=["Nova Studio admin"])
login_limiter = RateLimiter(max_requests=10, window_seconds=60)   # slows down password guessing


class LoginRequest(BaseModel):
    company: str = Field(default="ceramica-nova", min_length=2, max_length=60)
    email: str = Field(min_length=3, max_length=120)
    password: str = Field(min_length=1, max_length=200)


class UserOut(BaseModel):
    id: int
    name: str
    email: str
    role: str
    branch: Optional[str]
    discount_limit_pct: float


def user_out(u: User) -> UserOut:
    return UserOut(id=u.id, name=u.name, email=u.email, role=u.role, branch=u.branch,
                   discount_limit_pct=float(u.discount_limit_pct))


@router.post("/login")
def login(req: LoginRequest, request: Request, db: Session = Depends(get_db)):
    client = request.client.host if request.client else "unknown"
    if not login_limiter.allow(f"{client}:{req.email.lower()}"):
        raise HTTPException(status_code=429, detail="Too many login attempts. Please wait a minute.")
    user = authenticate(db, req.company, req.email, req.password)
    if user is None:
        # Same message whether the email or the password is wrong: don't reveal which accounts exist.
        raise HTTPException(status_code=401, detail="Wrong email or password.")
    return {"token": create_token(user), "token_type": "bearer", "user": user_out(user)}


@router.get("/me")
def me(user: User = Depends(current_user), db: Session = Depends(get_db)):
    tenant = db.get(Tenant, user.tenant_id)
    return {"user": user_out(user),
            "company": {"name": tenant.name, "slug": tenant.slug, "currency": tenant.currency,
                        "is_demo": tenant.is_demo},
            "permissions": sorted(PERMISSIONS[user.role])}


@router.get("/users", response_model=list[UserOut])
def list_users(user: User = Depends(require("users.manage")), db: Session = Depends(get_db)):
    rows = db.scalars(select(User).where(User.tenant_id == user.tenant_id).order_by(User.id))
    return [user_out(u) for u in rows]


class LeadOut(BaseModel):
    id: int
    number: str
    customer: str
    owner: Optional[str]
    stage: str
    source: str
    room: Optional[str]
    area_m2: Optional[float]
    budget: Optional[float]
    summary: Optional[str]
    created_at: datetime


def visible_leads(user: User):
    """Base query for the leads this user may see: own company always; own leads only for salespeople."""
    q = select(Lead).where(Lead.tenant_id == user.tenant_id)
    if not has_permission(user, "leads.view_all"):
        q = q.where(Lead.owner_id == user.id)
    return q


@router.get("/leads", response_model=list[LeadOut])
def list_leads(stage: Optional[str] = None, limit: int = 50,
               user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not (has_permission(user, "leads.view_all") or has_permission(user, "leads.view_own")):
        raise HTTPException(status_code=403, detail="Your role does not allow this.")
    if stage is not None and stage not in LEAD_STAGES:
        raise HTTPException(status_code=422, detail=f"stage must be one of {', '.join(LEAD_STAGES)}")
    q = visible_leads(user)
    if stage:
        q = q.where(Lead.stage == stage)
    rows = db.scalars(q.order_by(Lead.created_at.desc()).limit(max(1, min(limit, 200))))
    return [LeadOut(id=l.id, number=l.number, customer=l.customer.name, owner=l.owner.name if l.owner else None,
                    stage=l.stage, source=l.source, room=l.room,
                    area_m2=float(l.area_m2) if l.area_m2 is not None else None,
                    budget=float(l.budget) if l.budget is not None else None,
                    summary=l.summary, created_at=l.created_at) for l in rows]


@router.get("/audit")
def audit_log(limit: int = 50, user: User = Depends(require("audit.view")), db: Session = Depends(get_db)):
    rows = db.scalars(select(AuditLog).where(AuditLog.tenant_id == user.tenant_id)
                      .order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(max(1, min(limit, 200))))
    names = {u.id: u.name for u in db.scalars(select(User).where(User.tenant_id == user.tenant_id))}
    return [{"at": a.created_at, "user": names.get(a.user_id), "action": a.action, "entity": a.entity,
             "entity_id": a.entity_id, "after": a.after} for a in rows]


# ================================================================== step 4: the working admin
from datetime import timedelta  # noqa: E402
from typing import Literal  # noqa: E402

from sqlalchemy import func, or_  # noqa: E402

from backend import crm_bridge  # noqa: E402
from backend.crm import analytics  # noqa: E402
from backend.crm import operations as ops  # noqa: E402
from backend.db.models import (Activity, Batch, ChatSession, Customer, DiscountRequest, Order,  # noqa: E402
                               Product, Quote, StockMovement)


def money(x) -> Optional[float]:
    return None if x is None else round(float(x), 2)


def run(fn, *args, **kwargs):
    """Turn a broken business rule into a clear HTTP answer."""
    try:
        return fn(*args, **kwargs)
    except ops.OpError as e:
        raise HTTPException(status_code=e.status, detail=e.message)


def need_any(user: User, *perms: str) -> None:
    if not any(has_permission(user, p) for p in perms):
        raise HTTPException(status_code=403, detail="Your role does not allow this.")


def names(db: Session, tenant_id: int) -> dict[int, str]:
    return {u.id: u.name for u in db.scalars(select(User).where(User.tenant_id == tenant_id))}


# ------------------------------------------------------------------ dashboard
@router.get("/dashboard")
def dashboard(days: int = 30, user: User = Depends(require("dashboard.view")), db: Session = Depends(get_db)):
    if days not in (7, 30, 90):
        raise HTTPException(status_code=422, detail="days must be 7, 30 or 90")
    return analytics.dashboard(db, user, days)


@router.get("/staff")
def staff(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """People a lead can be assigned to (for the assign menu and the owner filter)."""
    need_any(user, "leads.view_all", "leads.assign")
    rows = db.scalars(select(User).where(User.tenant_id == user.tenant_id, User.active.is_(True),
                                         User.role.in_(("salesperson", "sales_manager"))).order_by(User.name))
    return [{"id": u.id, "name": u.name, "role": u.role} for u in rows]


# ------------------------------------------------------------------ leads
def lead_row(l: Lead, now) -> dict:
    return {"id": l.id, "number": l.number, "customer": l.customer.name, "phone": l.customer.phone,
            "owner": l.owner.name if l.owner else None, "owner_id": l.owner_id, "stage": l.stage, "source": l.source,
            "room": l.room, "area_m2": money(l.area_m2), "budget": money(l.budget), "summary": l.summary,
            "lost_reason": l.lost_reason, "created_at": l.created_at, "stage_changed_at": l.stage_changed_at,
            "days_in_stage": (now - l.stage_changed_at).days}


@router.get("/leads/search")
def search_leads(stage: Optional[str] = None, source: Optional[str] = None, owner_id: Optional[int] = None,
                 q: Optional[str] = None, stalled: bool = False, offset: int = 0, limit: int = 25,
                 user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Leads list for the screen: filters, search and paging, with the total count."""
    need_any(user, "leads.view_all", "leads.view_own")
    if stage is not None and stage not in LEAD_STAGES:
        raise HTTPException(status_code=422, detail=f"stage must be one of {', '.join(LEAD_STAGES)}")
    query = visible_leads(user).join(Customer, Customer.id == Lead.customer_id)
    if stage:
        query = query.where(Lead.stage == stage)
    if source:
        query = query.where(Lead.source == source)
    if owner_id:
        query = query.where(Lead.owner_id == owner_id)
    if q and q.strip():
        like = f"%{q.strip()[:60]}%"
        query = query.where(or_(Lead.number.ilike(like), Customer.name.ilike(like), Customer.phone.ilike(like),
                                Lead.summary.ilike(like)))
    now = ops.local_now()
    if stalled:
        ids = [s["lead_id"] for s in analytics.stalled_quotes(db, user, now)]
        query = query.where(Lead.id.in_(ids))
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.scalars(query.order_by(Lead.created_at.desc()).offset(max(0, offset)).limit(max(1, min(limit, 100))))
    counts: dict[str, int] = {}
    for st, n in db.execute(select(Lead.stage, func.count(Lead.id)).where(
            Lead.id.in_(select(visible_leads(user).subquery().c.id))).group_by(Lead.stage)):
        counts[st] = n
    return {"total": total, "items": [lead_row(l, now) for l in rows], "stage_counts": counts}


def get_lead(db: Session, user: User, lead_id: int) -> Lead:
    lead = db.get(Lead, lead_id)
    if not ops.can_see_lead(user, lead):
        raise HTTPException(status_code=404, detail="Lead not found.")
    return lead


@router.get("/leads/{lead_id}")
def lead_detail(lead_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    need_any(user, "leads.view_all", "leads.view_own")
    lead = get_lead(db, user, lead_id)
    who = names(db, user.tenant_id)
    c = lead.customer
    quotes = db.scalars(select(Quote).where(Quote.lead_id == lead.id).order_by(Quote.created_at.desc())).all()
    orders = db.scalars(select(Order).where(Order.quote_id.in_([q.id for q in quotes]))).all() if quotes else []
    acts = db.scalars(select(Activity).where(Activity.lead_id == lead.id)
                      .order_by(Activity.created_at.desc(), Activity.id.desc())).all()
    chat = db.scalar(select(ChatSession).where(ChatSession.lead_id == lead.id))
    return {"lead": lead_row(lead, ops.local_now()),
            "customer": {"name": c.name, "phone": c.phone, "email": c.email, "type": c.type, "area": c.area},
            "chat": {"messages": chat.messages, "last_message_at": chat.last_message_at} if chat else None,
            "quotes": [quote_row(db, q) for q in quotes],
            "orders": [order_row(db, o) for o in orders],
            "timeline": [{"at": a.created_at, "type": a.type, "text": a.text,
                          "by": who.get(a.user_id) or ("Ceramica Nova AI" if a.type in ("created", "quote_sent", "stage_change")
                                                       and lead.source == "showroom_chat" else "System")}
                         for a in acts],
            "can": {"edit": has_permission(user, "leads.edit"), "assign": has_permission(user, "leads.assign"),
                    "discount": has_permission(user, "quotes.edit"), "order": has_permission(user, "orders.create")}}


class StageRequest(BaseModel):
    stage: str
    lost_reason: Optional[str] = None
    comment: Optional[str] = Field(default=None, max_length=300)


@router.post("/leads/{lead_id}/stage")
def lead_stage(lead_id: int, req: StageRequest, user: User = Depends(require("leads.edit")), db: Session = Depends(get_db)):
    lead = run(ops.change_stage, db, user, get_lead(db, user, lead_id), req.stage, req.lost_reason, req.comment)
    return lead_row(lead, ops.local_now())


class NoteRequest(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    kind: Literal["note", "call"] = "note"


@router.post("/leads/{lead_id}/notes")
def lead_note(lead_id: int, req: NoteRequest, user: User = Depends(require("leads.edit")), db: Session = Depends(get_db)):
    run(ops.add_note, db, user, get_lead(db, user, lead_id), req.text, req.kind)
    return {"ok": True}


class AssignRequest(BaseModel):
    owner_id: int


@router.post("/leads/{lead_id}/assign")
def lead_assign(lead_id: int, req: AssignRequest, user: User = Depends(require("leads.assign")),
                db: Session = Depends(get_db)):
    lead = run(ops.assign_lead, db, user, get_lead(db, user, lead_id), req.owner_id)
    return lead_row(lead, ops.local_now())


# ------------------------------------------------------------------ quotations
def quote_row(db: Session, q: Quote) -> dict:
    lead = db.get(Lead, q.lead_id) if q.lead_id else None
    pending = db.scalar(select(DiscountRequest).where(DiscountRequest.quote_id == q.id, DiscountRequest.status == "pending"))
    order = db.scalar(select(Order).where(Order.quote_id == q.id, Order.status != "cancelled"))
    return {"id": q.id, "number": q.number, "lead": lead.number if lead else None, "lead_id": q.lead_id,
            "customer": db.get(Customer, q.customer_id).name, "owner": lead.owner.name if lead and lead.owner else None,
            "channel": q.channel, "status": q.status, "subtotal": money(q.subtotal), "discount_pct": money(q.discount_pct),
            "total": money(q.total), "margin_pct": round(float((q.total - q.cost_total) / q.total * 100), 1) if q.total else 0,
            "created_at": q.created_at, "sent_at": q.sent_at, "valid_until": q.valid_until,
            "pending_discount": money(pending.percent) if pending else None, "order": order.number if order else None}


def visible_quotes(user: User):
    q = select(Quote).where(Quote.tenant_id == user.tenant_id)
    if not has_permission(user, "quotes.view_all"):
        q = q.join(Lead, Lead.id == Quote.lead_id).where(Lead.owner_id == user.id)
    return q


@router.get("/quotes")
def list_quotes(status: Optional[str] = None, q: Optional[str] = None, offset: int = 0, limit: int = 25,
                user: User = Depends(current_user), db: Session = Depends(get_db)):
    need_any(user, "quotes.view_all", "quotes.view_own")
    query = visible_quotes(user).join(Customer, Customer.id == Quote.customer_id)
    if status:
        query = query.where(Quote.status == status)
    if q and q.strip():
        like = f"%{q.strip()[:60]}%"
        query = query.where(or_(Quote.number.ilike(like), Customer.name.ilike(like)))
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.scalars(query.order_by(Quote.created_at.desc()).offset(max(0, offset)).limit(max(1, min(limit, 100))))
    return {"total": total, "items": [quote_row(db, x) for x in rows]}


def get_quote(db: Session, user: User, quote_id: int) -> Quote:
    quote = db.get(Quote, quote_id)
    if not ops.can_see_quote(db, user, quote):
        raise HTTPException(status_code=404, detail="Quotation not found.")
    return quote


@router.get("/quotes/{quote_id}")
def quote_detail(quote_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    need_any(user, "quotes.view_all", "quotes.view_own")
    quote = get_quote(db, user, quote_id)
    tenant = db.get(Tenant, user.tenant_id)
    who = names(db, user.tenant_id)
    lines = []
    for l in quote.lines:
        p = l.product
        lines.append({"sku": p.sku, "name": p.name, "unit": p.unit, "quantity": money(l.quantity), "boxes": l.boxes,
                      "unit_price": money(l.unit_price), "line_total": money(l.line_total),
                      "one_batch": any(b.qty_left >= l.quantity for b in p.batches)})
    reqs = db.scalars(select(DiscountRequest).where(DiscountRequest.quote_id == quote.id).order_by(DiscountRequest.created_at))
    sees_cost = has_permission(user, "prices.edit") or has_permission(user, "reports.view")
    return {**quote_row(db, quote), "lines": lines, "cost_total": money(quote.cost_total) if sees_cost else None,
            "min_margin_pct": money(tenant.min_margin_pct), "your_discount_limit_pct": money(user.discount_limit_pct),
            "discounts": [{"id": d.id, "percent": money(d.percent), "margin_after_pct": money(d.margin_after_pct),
                           "status": d.status, "requested_by": who.get(d.requested_by, "Ceramica Nova AI chat"),
                           "decided_by": who.get(d.decided_by), "reason": d.reason, "created_at": d.created_at,
                           "why": ops.why_waiting(db, d) if d.status == "pending" else None}
                          for d in reqs]}


class DiscountRequestIn(BaseModel):
    percent: float = Field(gt=0, le=40)
    reason: Optional[str] = Field(default=None, max_length=200)


@router.post("/quotes/{quote_id}/discount")
def quote_discount(quote_id: int, req: DiscountRequestIn, user: User = Depends(require("quotes.edit")),
                   db: Session = Depends(get_db)):
    d = run(ops.request_discount, db, user, get_quote(db, user, quote_id), req.percent, req.reason)
    return {"status": d.status, "percent": money(d.percent), "margin_after_pct": money(d.margin_after_pct),
            "reason": d.reason, "quote": quote_row(db, db.get(Quote, d.quote_id))}


@router.post("/quotes/{quote_id}/order")
def quote_to_order(quote_id: int, user: User = Depends(require("orders.create")), db: Session = Depends(get_db)):
    order = run(ops.order_from_quote, db, user, get_quote(db, user, quote_id))
    crm_bridge.refresh_catalog(force=True)
    return order_row(db, order)


# ------------------------------------------------------------------ orders
def order_row(db: Session, o: Order) -> dict:
    quote = db.get(Quote, o.quote_id) if o.quote_id else None
    lead = db.get(Lead, quote.lead_id) if quote and quote.lead_id else None
    c = db.get(Customer, o.customer_id)
    return {"id": o.id, "number": o.number, "quote": quote.number if quote else None, "quote_id": o.quote_id,
            "lead": lead.number if lead else None, "lead_id": lead.id if lead else None, "customer": c.name,
            "phone": c.phone, "area": c.area, "owner": lead.owner.name if lead and lead.owner else None,
            "channel": quote.channel if quote else "staff", "status": o.status, "total": money(o.total),
            "created_at": o.created_at, "delivered_at": o.delivered_at}


def visible_orders(user: User):
    q = select(Order).where(Order.tenant_id == user.tenant_id)
    if not has_permission(user, "orders.view_all"):
        q = q.join(Quote, Quote.id == Order.quote_id).join(Lead, Lead.id == Quote.lead_id).where(Lead.owner_id == user.id)
    return q


@router.get("/orders")
def list_orders(status: Optional[str] = None, q: Optional[str] = None, offset: int = 0, limit: int = 25,
                user: User = Depends(current_user), db: Session = Depends(get_db)):
    need_any(user, "orders.view_all", "orders.view_own")
    query = visible_orders(user).join(Customer, Customer.id == Order.customer_id)
    if status:
        query = query.where(Order.status == status)
    if q and q.strip():
        like = f"%{q.strip()[:60]}%"
        query = query.where(or_(Order.number.ilike(like), Customer.name.ilike(like)))
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.scalars(query.order_by(Order.created_at.desc()).offset(max(0, offset)).limit(max(1, min(limit, 100))))
    counts = {s: n for s, n in db.execute(select(Order.status, func.count(Order.id)).where(
        Order.id.in_(select(visible_orders(user).subquery().c.id))).group_by(Order.status))}
    return {"total": total, "items": [order_row(db, o) for o in rows], "status_counts": counts}


def get_order(db: Session, user: User, order_id: int) -> Order:
    order = db.get(Order, order_id)
    if not ops.can_see_order(db, user, order):
        raise HTTPException(status_code=404, detail="Order not found.")
    return order


@router.get("/orders/{order_id}")
def order_detail(order_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    need_any(user, "orders.view_all", "orders.view_own")
    order = get_order(db, user, order_id)
    lines = []
    for l in order.lines:
        p = db.get(Product, l.product_id)
        batch = db.get(Batch, l.batch_id) if l.batch_id else None
        suggested = ops.pick_batch(p, l.quantity, l.batch_id) if order.status == "pending" else batch
        lines.append({"sku": p.sku, "name": p.name, "unit": p.unit, "quantity": money(l.quantity),
                      "unit_price": money(l.unit_price), "line_total": money(l.line_total),
                      "batch": suggested.code if suggested else None, "shade": suggested.shade if suggested else None,
                      "batch_left": money(suggested.qty_left) if suggested else None,
                      "one_batch_ok": suggested is not None})
    return {**order_row(db, order), "lines": lines,
            "can": {"confirm": order.status == "pending" and has_permission(user, "orders.create"),
                    "deliver": order.status == "confirmed" and has_permission(user, "orders.deliver"),
                    "cancel": order.status in ("pending", "confirmed") and has_permission(user, "orders.create")}}


class CancelRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=200)


@router.post("/orders/{order_id}/confirm")
def order_confirm(order_id: int, user: User = Depends(require("orders.create")), db: Session = Depends(get_db)):
    order = run(ops.confirm_order, db, user, get_order(db, user, order_id))
    crm_bridge.refresh_catalog(force=True)
    return order_row(db, order)


@router.post("/orders/{order_id}/deliver")
def order_deliver(order_id: int, user: User = Depends(require("orders.deliver")), db: Session = Depends(get_db)):
    return order_row(db, run(ops.deliver_order, db, user, get_order(db, user, order_id)))


@router.post("/orders/{order_id}/cancel")
def order_cancel(order_id: int, req: CancelRequest, user: User = Depends(require("orders.create")),
                 db: Session = Depends(get_db)):
    order = run(ops.cancel_order, db, user, get_order(db, user, order_id), req.reason)
    crm_bridge.refresh_catalog(force=True)
    return order_row(db, order)


# ------------------------------------------------------------------ inventory
@router.get("/inventory")
def inventory(user: User = Depends(require("inventory.view")), db: Session = Depends(get_db)):
    rows = analytics.stock_levels(db, user.tenant_id)
    if not has_permission(user, "prices.edit") and not has_permission(user, "reports.view"):
        for r in rows:
            r.pop("cost_price", None)                 # salespeople and warehouse don't need the cost price
    return {"items": rows, "can": {"receive": has_permission(user, "inventory.edit"),
                                   "price": has_permission(user, "prices.edit")}}


def get_product(db: Session, user: User, sku: str) -> Product:
    p = db.scalar(select(Product).where(Product.tenant_id == user.tenant_id, Product.sku == sku))
    if p is None:
        raise HTTPException(status_code=404, detail="Product not found.")
    return p


@router.get("/inventory/{sku}")
def product_stock(sku: str, user: User = Depends(require("inventory.view")), db: Session = Depends(get_db)):
    p = get_product(db, user, sku)
    who = names(db, user.tenant_id)
    moves = db.scalars(select(StockMovement).where(StockMovement.product_id == p.id)
                       .order_by(StockMovement.created_at.desc(), StockMovement.id.desc()).limit(30))
    codes = {b.id: b.code for b in p.batches}
    row = next(r for r in analytics.stock_levels(db, user.tenant_id) if r["sku"] == sku) if p.active else None
    return {"sku": p.sku, "name": p.name, "unit": p.unit, "kind": p.kind, "image": p.image,
            "price": money(p.price), "cost_price": money(p.cost_price) if has_permission(user, "prices.edit") else None,
            "attributes": p.attributes, "level": row,
            "batches": [{"code": b.code, "shade": b.shade, "received_at": b.received_at, "received": money(b.received_qty),
                         "left": money(b.qty_left)} for b in sorted(p.batches, key=lambda b: b.received_at, reverse=True)],
            "movements": [{"at": m.created_at, "qty": money(m.qty), "reason": m.reason, "reference": m.reference,
                           "batch": codes.get(m.batch_id), "by": who.get(m.user_id)} for m in moves]}


class ReceiveRequest(BaseModel):
    qty: float = Field(gt=0, le=100000)
    shade: Optional[str] = Field(default=None, max_length=20)
    code: Optional[str] = Field(default=None, max_length=30)


@router.post("/inventory/{sku}/receive")
def receive(sku: str, req: ReceiveRequest, user: User = Depends(require("inventory.edit")), db: Session = Depends(get_db)):
    b = run(ops.receive_stock, db, user, get_product(db, user, sku), req.qty, req.shade, req.code)
    crm_bridge.refresh_catalog(force=True)
    return {"code": b.code, "qty": money(b.received_qty), "shade": b.shade}


class PriceRequest(BaseModel):
    price: float = Field(gt=0, le=100000)
    cost_price: Optional[float] = Field(default=None, gt=0, le=100000)


@router.post("/products/{sku}/price")
def price(sku: str, req: PriceRequest, user: User = Depends(require("prices.edit")), db: Session = Depends(get_db)):
    p = run(ops.change_price, db, user, get_product(db, user, sku), req.price, req.cost_price)
    crm_bridge.refresh_catalog(force=True)
    return {"sku": p.sku, "price": money(p.price), "cost_price": money(p.cost_price)}


# ------------------------------------------------------------------ approvals (feature 5)
@router.get("/approvals")
def approvals(user: User = Depends(require("discounts.approve")), db: Session = Depends(get_db)):
    tenant = db.get(Tenant, user.tenant_id)
    who = names(db, user.tenant_id)
    since = ops.local_now() - timedelta(days=30)
    done = db.execute(select(DiscountRequest, Quote).join(Quote, Quote.id == DiscountRequest.quote_id)
                      .where(DiscountRequest.tenant_id == user.tenant_id,
                             DiscountRequest.status.in_(("approved", "rejected", "auto_approved")),
                             DiscountRequest.created_at >= since)
                      .order_by(DiscountRequest.created_at.desc()).limit(40)).all()
    return {"min_margin_pct": money(tenant.min_margin_pct), "is_owner": user.role == "owner",
            "pending": analytics.pending_discounts(db, user),
            "history": [{"id": d.id, "quote": q.number, "quote_id": q.id, "percent": money(d.percent),
                         "margin_after_pct": money(d.margin_after_pct), "status": d.status,
                         "requested_by": who.get(d.requested_by, "Ceramica Nova AI chat"),
                         "decided_by": who.get(d.decided_by), "reason": d.reason, "at": d.decided_at or d.created_at}
                        for d, q in done]}


class DecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    reason: Optional[str] = Field(default=None, max_length=200)
    override: bool = False


@router.post("/approvals/{request_id}")
def decide(request_id: int, req: DecisionRequest, user: User = Depends(require("discounts.approve")),
           db: Session = Depends(get_db)):
    d = db.get(DiscountRequest, request_id)
    if d is None or d.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="Request not found.")
    d = run(ops.decide_discount, db, user, d, req.decision == "approve", req.reason, req.override)
    return {"status": d.status, "margin_after_pct": money(d.margin_after_pct), "quote": quote_row(db, db.get(Quote, d.quote_id))}


# ================================================================== step 5: AI features (feature 11 + feature 3)
import os  # noqa: E402

from backend.crm import ask as ask_data  # noqa: E402
from backend.crm import followups  # noqa: E402

ai_limiter = RateLimiter(max_requests=12, window_seconds=60)   # per user: keeps API costs predictable
_ai_client = None


def ai_client():
    """Anthropic client when a key is configured, else None (the features then run in offline/template mode)."""
    global _ai_client
    if not os.getenv("ANTHROPIC_API_KEY") or os.getenv("LLM_PROVIDER", "anthropic") != "anthropic":
        return None
    if _ai_client is None:
        import anthropic
        from backend.deploy import BudgetedClient
        _ai_client = BudgetedClient(anthropic.Anthropic(max_retries=1, timeout=45))
    return _ai_client


def ai_allowed(user: User) -> None:
    if not ai_limiter.allow(f"{user.tenant_id}:{user.id}"):
        raise HTTPException(status_code=429, detail="Too many AI requests. Please wait a minute.")


@router.get("/ask")
def ask_info(user: User = Depends(require("dashboard.view"))):
    return {"ai_available": ai_client() is not None, "suggestions": ask_data.SUGGESTIONS,
            "scope": "company" if analytics.own_leads(user) is None else "my leads"}


class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=ask_data.MAX_QUESTION)


@router.post("/ask")
def ask(req: AskRequest, user: User = Depends(require("dashboard.view")), db: Session = Depends(get_db)):
    ai_allowed(user)
    try:
        return ask_data.ask(db, user, req.question, client=ai_client())
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.get("/followups")
def followups_list(user: User = Depends(require("quotes.edit")), db: Session = Depends(get_db)):
    return {"ai_available": ai_client() is not None, "items": followups.waiting(db, user)}


class DraftRequest(BaseModel):
    channel: Literal["whatsapp", "email"] = "whatsapp"


@router.post("/followups/{quote_id}/draft")
def followup_draft(quote_id: int, req: DraftRequest, user: User = Depends(require("quotes.edit")),
                   db: Session = Depends(get_db)):
    ai_allowed(user)
    return run(followups.draft, db, user, get_quote(db, user, quote_id), req.channel, ai_client())


class SentRequest(BaseModel):
    channel: Literal["whatsapp", "email"] = "whatsapp"
    text: str = Field(min_length=10, max_length=2000)


@router.post("/followups/{quote_id}/sent")
def followup_sent(quote_id: int, req: SentRequest, user: User = Depends(require("quotes.edit")),
                  db: Session = Depends(get_db)):
    run(followups.mark_sent, db, user, get_quote(db, user, quote_id), req.text, req.channel)
    return {"ok": True}
