"""The link between Ceramica Nova AI (the chat) and Nova Studio (the CRM database).

1. Products, prices and stock: the assistant's catalog is refreshed from the database, so a
   price or stock change made by staff reaches the chat within REFRESH_SECONDS.
2. Chat to lead (feature 1): once a visitor shares real project details, the conversation
   becomes a lead with a summary, assigned to a salesperson. Quotes made in the chat are saved
   in Quotations, and a discount asked for in the chat waits for a manager's approval.
3. Call-back: a visitor can leave a name and phone so the salesperson can call.

Safety rule: the CRM must never break the chat. Every function here catches its own errors,
logs them and returns None, and the assistant carries on exactly as before.
"""
import re
import time
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session as DB

from backend import catalog as cat
from backend.auth import audit
from backend.db.database import SessionLocal, local_now
from backend.db.models import (Activity, Batch, ChatSession, Customer, DiscountRequest, Lead, Product, Quote,
                               QuoteLine, Tenant)
from backend.db.numbering import next_number
from backend.observability import log_event

REFRESH_SECONDS = 30
_BASE_TILES = cat.load_catalog()          # the original files: used for fields the database doesn't hold
_BASE_SETS = cat.load_sanitary()
_last_refresh = 0.0


def q2(x) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _tenant(db: DB) -> Optional[Tenant]:
    from backend.shop_api import SHOP_TENANT
    return db.scalar(select(Tenant).where(Tenant.slug == SHOP_TENANT))


# ------------------------------------------------------------------ 1. catalog from the database
def refresh_catalog(force: bool = False) -> bool:
    """Rebuild the assistant's catalog from the database. Returns True when it was refreshed."""
    global _last_refresh
    if not force and time.monotonic() - _last_refresh < REFRESH_SECONDS:
        return False
    try:
        with SessionLocal() as db:
            t = _tenant(db)
            if t is None:
                return False
            tiles, sets = {}, {}
            for p in db.scalars(select(Product).where(Product.tenant_id == t.id, Product.active.is_(True))):
                batches = [b.qty_left for b in p.batches]
                stock = float(sum(batches, Decimal(0)))
                a = p.attributes or {}
                if p.kind == "tile":
                    base = _BASE_TILES.get(p.sku)
                    w, l = map(int, a["size_cm"].split("x"))
                    data = {**(base.model_dump() if base else {}),
                            "sku": p.sku, "name": p.name, "material": a.get("material", "porcelain"),
                            "tile_width_cm": w, "tile_length_cm": l, "surfaces": a.get("surfaces", ["floor"]),
                            "locations": a.get("locations", ["indoor"]), "finish": a.get("finish", ""),
                            "slip_rating": a.get("slip_rating"), "tiles_per_box": a["tiles_per_box"], "style": p.category,
                            "commercial": a.get("commercial", base.commercial if base else False),
                            "application": a.get("application", base.application if base else "Indoor floor"),
                            "price_per_m2": float(p.price), "currency": "QAR", "stock_m2": stock,
                            "largest_batch_m2": float(max(batches, default=Decimal(0)))}
                    tiles[p.sku] = cat.Product(**data)
                else:
                    base = _BASE_SETS.get(p.sku)
                    data = {**(base.model_dump() if base else {"description": p.name, "style": "Modern",
                                                               "rooms": ["bathroom"]}),
                            "sku": p.sku, "name": p.name, "category": p.category, "finish": a.get("finish", ""),
                            "includes": a.get("includes", []), "price": float(p.price), "currency": "QAR",
                            "unit": "set", "stock_units": int(stock)}
                    sets[p.sku] = cat.SanitaryProduct(**data)
        if tiles:
            cat.replace_catalog(tiles, sets)
        _last_refresh = time.monotonic()
        return bool(tiles)
    except Exception as e:                       # database missing or not seeded: keep the current catalog
        log_event("crm_catalog_refresh_failed", error=type(e).__name__)
        return False


# ------------------------------------------------------------------ 2. chat to lead
ROOM_WORDS = [("bath", "bathroom"), ("toilet", "bathroom"), ("shower", "bathroom"), ("kitchen", "kitchen"),
              ("majlis", "majlis"), ("living", "living room"), ("lounge", "living room"), ("bedroom", "bedroom"),
              ("balcon", "outdoor"), ("terrace", "outdoor"), ("garden", "outdoor"), ("pool", "outdoor"),
              ("patio", "outdoor"), ("roof", "outdoor"), ("office", "office"), ("restaurant", "office"),
              ("shop", "office"), ("cafe", "office")]


def room_of(req) -> Optional[str]:
    text = (req.application or "").lower()
    for word, room in ROOM_WORDS:
        if word in text:
            return room
    if req.indoor_outdoor == "outdoor":
        return "outdoor"
    return text[:40] or None


def has_project_details(session) -> bool:
    """A chat becomes a lead once the visitor shares something a salesperson can act on."""
    r = session.requirements
    described = bool(r.application or r.floor_or_wall or r.indoor_outdoor)
    return bool(session.quote_ids) or bool(r.area_m2 and described) or bool(r.budget and described)


def summary_of(session, new_quote: Optional[str] = None) -> str:
    r = session.requirements
    bits = []
    place = " ".join(x for x in [r.indoor_outdoor, r.floor_or_wall] if x)
    if r.application or place:
        bits.append(f"{(r.application or '').strip()} {'(' + place + ')' if place else ''}".strip().capitalize())
    if r.area_m2:
        bits.append(f"about {r.area_m2:g} m²")
    if r.budget:
        bits.append(f"budget QAR {r.budget:,.0f}")
    if r.preferred_style or r.preferred_color:
        bits.append("likes " + " ".join(x for x in [r.preferred_color, r.preferred_style] if x))
    if r.slip_requirement == "high":
        bits.append("needs high slip resistance")
    text = ", ".join(b for b in bits if b) or "Project details still being collected"
    shown = [cat.CATALOG[s].name for s in session.last_recommended if s in cat.CATALOG][:3]
    if shown:
        text += f". Shown: {', '.join(shown)}"
    latest = new_quote or (session.quote_ids[-1] if session.quote_ids else None)
    if latest:
        text += f". Quotation {latest}"
    return "Ceramica Nova AI chat: " + text + "."


def _chat_row(db: DB, t: Tenant, session) -> ChatSession:
    row = db.scalar(select(ChatSession).where(ChatSession.tenant_id == t.id,
                                              ChatSession.session_id == session.session_id))
    if row is None:
        now = local_now()
        row = ChatSession(tenant_id=t.id, session_id=session.session_id, visitor_id=session.user_id,
                          messages=0, created_at=now, last_message_at=now)
        db.add(row)
        db.flush()
    return row


def _ensure_lead(db: DB, t: Tenant, row: ChatSession, session) -> Lead:
    from backend.shop_api import pick_salesperson
    if row.lead_id:
        return db.get(Lead, row.lead_id)
    now = local_now()
    if row.customer_id is None:
        c = Customer(tenant_id=t.id, name=f"Chat visitor {session.session_id[:4].upper()}", type="retail",
                     created_at=now)
        db.add(c)
        db.flush()
        row.customer_id = c.id
    owner = pick_salesperson(db, t.id)
    lead = Lead(tenant_id=t.id, number=next_number(db, Lead, t.id, "L"), customer_id=row.customer_id,
                owner_id=owner.id if owner else None, stage="new", source="showroom_chat",
                created_at=now, stage_changed_at=now)
    db.add(lead)
    db.flush()
    row.lead_id = lead.id
    db.add(Activity(tenant_id=t.id, lead_id=lead.id, type="created", created_at=now,
                    text="Lead created from a Ceramica Nova AI chat"))
    audit(db, t.id, "chat.lead", entity="lead", entity_id=lead.number, after={"session": session.session_id})
    return lead


def _update_lead(db: DB, lead: Lead, session) -> None:
    r = session.requirements
    lead.room = room_of(r) or lead.room
    lead.area_m2 = q2(r.area_m2) if r.area_m2 else lead.area_m2
    lead.budget = q2(r.budget) if r.budget else lead.budget
    lead.style = (r.preferred_style or lead.style or None)
    lead.summary = summary_of(session)
    if lead.stage == "new" and r.area_m2 and (r.application or r.floor_or_wall):
        lead.stage, lead.stage_changed_at = "qualified", local_now()
        db.add(Activity(tenant_id=lead.tenant_id, lead_id=lead.id, type="stage_change", created_at=lead.stage_changed_at,
                        text="Qualified in chat: room and area known"))


def record_chat_turn(session) -> Optional[str]:
    """Called after every reply. Returns the lead number once the chat is a lead."""
    try:
        with SessionLocal() as db:
            t = _tenant(db)
            if t is None:
                return None
            row = _chat_row(db, t, session)
            row.messages += 1
            row.last_message_at = local_now()
            lead = None
            if row.lead_id or has_project_details(session):
                lead = _ensure_lead(db, t, row, session)
                _update_lead(db, lead, session)
            db.commit()
            return lead.number if lead else None
    except Exception as e:
        log_event("crm_chat_record_failed", error=type(e).__name__)
        return None


def save_quote(session, quote, channel: str = "chat") -> Optional[str]:
    """Store a quote made by the assistant (or its PDF button) in Quotations.
    Returns the CRM quote number, which then becomes the number the customer sees."""
    try:
        with SessionLocal() as db:
            t = _tenant(db)
            if t is None:
                return None
            row = _chat_row(db, t, session)
            lead = _ensure_lead(db, t, row, session)
            number = next_number(db, Quote, t.id, "Q")
            now = local_now()
            products = {p.sku: p for p in db.scalars(select(Product).where(Product.tenant_id == t.id))}
            lines, cost = [], Decimal(0)
            for l in quote.products:
                p = products[l.sku]
                qty = Decimal(str(l.quantity_m2 if l.unit == "m2" else l.quantity_units))
                lines.append(QuoteLine(product_id=p.id, quantity=qty, boxes=l.quantity_boxes, unit_price=p.price,
                                       unit_cost=p.cost_price, line_total=q2(l.line_subtotal)))
                cost += q2(qty * p.cost_price)
            cost_preview = sum((q2(Decimal(str(l.quantity_m2 if l.unit == "m2" else l.quantity_units))
                                   * products[l.sku].cost_price) for l in quote.products), Decimal(0))
            margin = (q2(quote.total) - cost_preview) / q2(quote.total) * 100 if quote.total else Decimal(0)
            # Feature 5 groundwork: a chat discount is flagged for a manager when it is above the assistant's
            # limit (BR-07) OR leaves less than the company's minimum margin.
            pending = quote.approval_required or (quote.discount_percent > 0 and margin < t.min_margin_pct)
            q = Quote(tenant_id=t.id, number=number, lead_id=lead.id, customer_id=lead.customer_id, created_by=None,
                      channel=channel, status="draft" if pending else "sent", subtotal=q2(quote.subtotal),
                      discount_pct=q2(quote.discount_percent), discount_amount=q2(quote.discount_amount),
                      total=q2(quote.total), cost_total=q2(cost), created_at=now, sent_at=None if pending else now,
                      valid_until=None, lines=lines)
            db.add(q)
            db.flush()
            note = f"Quotation {number} made in the chat (QAR {quote.total:,.2f})"
            if quote.discount_percent:
                db.add(DiscountRequest(tenant_id=t.id, quote_id=q.id, requested_by=None, percent=q2(quote.discount_percent),
                                       margin_after_pct=q2(margin), status="pending" if pending else "auto_approved",
                                       reason="Asked for in the Ceramica Nova AI chat", created_at=now))
                if pending:
                    note += f"; {quote.discount_percent:g}% discount waits for manager approval"
            if lead.stage in ("new", "qualified"):
                lead.stage, lead.stage_changed_at = "quote_sent", now
            lead.summary = summary_of(session, new_quote=number)
            db.add(Activity(tenant_id=t.id, lead_id=lead.id, type="quote_sent", text=note, created_at=now))
            audit(db, t.id, "chat.quote", entity="quote", entity_id=number, after={"total": float(quote.total)})
            db.commit()
            return number
    except Exception as e:
        log_event("crm_quote_save_failed", error=type(e).__name__)
        return None


# ------------------------------------------------------------------ 3. call-back request
def phone_key(phone: str) -> str:
    return re.sub(r"\D", "", phone)[-8:]


def link_customer(db: DB, t: Tenant, row: ChatSession, lead: Lead, name: str, phone: str,
                  email: Optional[str] = None, area: Optional[str] = None) -> Customer:
    """Give the chat's anonymous customer a name and phone. If that phone already belongs to a
    customer, the chat (lead and its quotes) is moved onto the existing customer instead."""
    existing = next((c for c in db.scalars(select(Customer).where(Customer.tenant_id == t.id))
                     if c.phone and c.id != row.customer_id and phone_key(c.phone) == phone_key(phone)), None)
    if existing:
        lead.customer_id = row.customer_id = existing.id
        for qt in db.scalars(select(Quote).where(Quote.lead_id == lead.id)):
            qt.customer_id = existing.id
        return existing
    c = db.get(Customer, row.customer_id)
    c.name, c.phone = name, phone
    c.email = email or c.email
    c.area = area or c.area
    return c


def chat_lead_for(db: DB, t: Tenant, session_id: str, visitor_id: str) -> tuple[Optional[ChatSession], Optional[Lead]]:
    """The lead behind a chat, but only for the visitor who owns that chat."""
    row = db.scalar(select(ChatSession).where(ChatSession.tenant_id == t.id, ChatSession.session_id == session_id,
                                              ChatSession.visitor_id == visitor_id))
    return (row, db.get(Lead, row.lead_id)) if row and row.lead_id else (row, None)


def request_callback(session, name: str, phone: str, email: Optional[str] = None) -> Optional[dict]:
    try:
        with SessionLocal() as db:
            t = _tenant(db)
            if t is None:
                return None
            row = _chat_row(db, t, session)
            lead = _ensure_lead(db, t, row, session)
            _update_lead(db, lead, session)
            link_customer(db, t, row, lead, name, phone, email)
            now = local_now()
            db.add(Activity(tenant_id=t.id, lead_id=lead.id, type="call", created_at=now,
                            text=f"{name} asked for a call back on {phone} from the chat"))
            audit(db, t.id, "chat.callback", entity="lead", entity_id=lead.number)
            db.commit()
            owner = lead.owner.name.split()[0] if lead.owner else None
            return {"lead": lead.number, "salesperson": owner}
    except Exception as e:
        log_event("crm_callback_failed", error=type(e).__name__)
        return None
