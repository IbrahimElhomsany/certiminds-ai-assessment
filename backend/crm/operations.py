"""Nova Studio staff actions: everything that CHANGES data in the admin.

Each function checks the business rules, writes the change, adds a timeline entry where a customer
is involved, and writes an audit row. The routes in admin_api.py check the role permission first;
the rules here hold whoever calls them (tests call them directly too).

Rules that matter most:
- Feature 5, margin guardrail: a discount is auto-approved only when it is within the person's own
  limit AND leaves at least the company's minimum margin. Anything else waits for a manager. A manager
  cannot approve below the minimum margin; only the owner can, and only with a written reason.
- Feature 8, one batch per job: an order is confirmed only if EVERY line can be taken from a single
  batch (same shade). Stock is taken from that batch at confirmation, never before.
"""
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.auth import audit, has_permission
from backend.db.database import local_now
from backend.db.models import (LEAD_STAGES, LOST_REASONS, Activity, Batch, DiscountRequest, Lead, Order, OrderLine,
                               Product, Quote, StockMovement, Tenant, User)
from backend.db.numbering import next_number

MAX_DISCOUNT_PCT = Decimal(40)
QUOTE_VALID_DAYS = 14


class OpError(Exception):
    """A rule said no. status follows HTTP: 403 not allowed, 404 not found, 409 conflicts with the data."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def q2(x) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def note(db: Session, user: Optional[User], lead: Lead, kind: str, text: str) -> None:
    db.add(Activity(tenant_id=lead.tenant_id, lead_id=lead.id, user_id=user.id if user else None, type=kind,
                    text=text, created_at=local_now()))


# ------------------------------------------------------------------ who may touch what
def can_see_lead(user: User, lead: Optional[Lead]) -> bool:
    if lead is None or lead.tenant_id != user.tenant_id:
        return False
    return has_permission(user, "leads.view_all") or (has_permission(user, "leads.view_own") and lead.owner_id == user.id)


def can_see_quote(db: Session, user: User, quote: Optional[Quote]) -> bool:
    if quote is None or quote.tenant_id != user.tenant_id:
        return False
    if has_permission(user, "quotes.view_all"):
        return True
    lead = db.get(Lead, quote.lead_id) if quote.lead_id else None
    return has_permission(user, "quotes.view_own") and lead is not None and lead.owner_id == user.id


def can_see_order(db: Session, user: User, order: Optional[Order]) -> bool:
    if order is None or order.tenant_id != user.tenant_id:
        return False
    if has_permission(user, "orders.view_all"):
        return True
    quote = db.get(Quote, order.quote_id) if order.quote_id else None
    lead = db.get(Lead, quote.lead_id) if quote and quote.lead_id else None
    return has_permission(user, "orders.view_own") and lead is not None and lead.owner_id == user.id


def lead_of_quote(db: Session, quote: Quote) -> Optional[Lead]:
    return db.get(Lead, quote.lead_id) if quote.lead_id else None


# ------------------------------------------------------------------ leads
def change_stage(db: Session, user: User, lead: Lead, stage: str, lost_reason: Optional[str] = None,
                 comment: Optional[str] = None) -> Lead:
    if stage not in LEAD_STAGES:
        raise OpError(422, f"stage must be one of {', '.join(LEAD_STAGES)}")
    if stage == lead.stage:
        raise OpError(409, "The lead is already at that stage.")
    if stage == "won":
        raise OpError(409, "A lead is marked won when its order is confirmed. Confirm the order instead.")
    if lead.stage == "won":
        raise OpError(409, "This lead has a confirmed order. Cancel the order first.")
    if stage == "lost" and lost_reason not in LOST_REASONS:
        raise OpError(422, f"Pick why the lead was lost: {', '.join(LOST_REASONS)}")
    before = {"stage": lead.stage, "lost_reason": lead.lost_reason}
    lead.stage, lead.stage_changed_at = stage, local_now()
    lead.lost_reason = lost_reason if stage == "lost" else None
    label = stage.replace("_", " ").capitalize()
    text = f"Stage changed to {label}" + (f" ({lost_reason.replace('_', ' ')})" if stage == "lost" else "")
    note(db, user, lead, "stage_change", text + (f": {comment.strip()}" if comment and comment.strip() else ""))
    if stage == "lost":
        for q in db.scalars(select(Quote).where(Quote.lead_id == lead.id, Quote.status.in_(("draft", "sent")))):
            q.status = "rejected"
            for d in db.scalars(select(DiscountRequest).where(DiscountRequest.quote_id == q.id,
                                                              DiscountRequest.status == "pending")):
                d.status, d.decided_by, d.decided_at = "rejected", user.id, local_now()
                d.reason = "Closed: the lead was marked lost"
    audit(db, lead.tenant_id, "lead.stage", user.id, "lead", lead.number, before=before,
          after={"stage": stage, "lost_reason": lead.lost_reason})
    db.commit()
    return lead


def add_note(db: Session, user: User, lead: Lead, text: str, kind: str = "note") -> None:
    if kind not in ("note", "call"):
        raise OpError(422, "kind must be note or call")
    text = (text or "").strip()
    if not text:
        raise OpError(422, "Write something first.")
    note(db, user, lead, kind, text[:1000])
    db.commit()


def assign_lead(db: Session, user: User, lead: Lead, owner_id: int) -> Lead:
    owner = db.get(User, owner_id)
    if owner is None or owner.tenant_id != user.tenant_id or not owner.active \
            or owner.role not in ("salesperson", "sales_manager"):
        raise OpError(422, "Pick an active salesperson from your company.")
    if owner.id == lead.owner_id:
        raise OpError(409, f"{owner.name} already owns this lead.")
    before = lead.owner.name if lead.owner else None
    lead.owner_id = owner.id
    note(db, user, lead, "assign", f"Assigned to {owner.name}" + (f" (was {before})" if before else ""))
    audit(db, lead.tenant_id, "lead.assign", user.id, "lead", lead.number, before={"owner": before},
          after={"owner": owner.name})
    db.commit()
    return lead


# ------------------------------------------------------------------ discounts (feature 5)
def margin_after(quote: Quote, percent: Decimal) -> tuple[Decimal, Decimal]:
    """Total and gross margin % if `percent` is taken off the quote's subtotal."""
    total = q2(quote.subtotal * (1 - percent / 100))
    margin = q2((total - quote.cost_total) / total * 100) if total else Decimal(0)
    return total, margin


def why_waiting(db: Session, req: DiscountRequest) -> str:
    """Plain-language reason a discount needs a manager, worked out from the current numbers."""
    quote = db.get(Quote, req.quote_id)
    tenant = db.get(Tenant, req.tenant_id)
    _, margin = margin_after(quote, req.percent)
    who = db.get(User, req.requested_by) if req.requested_by else None
    why = []
    if who is None:
        why.append("Asked for in the Ceramica Nova AI chat, so a manager checks it")
    elif req.percent > who.discount_limit_pct:
        why.append(f"Over {who.name.split()[0]}'s {float(who.discount_limit_pct):g}% limit")
    if margin < tenant.min_margin_pct:
        why.append(f"margin {margin}% is below the {float(tenant.min_margin_pct):g}% minimum, so only the owner can approve")
    elif who is not None and req.percent > who.discount_limit_pct:
        why.append(f"margin stays above {float(tenant.min_margin_pct):g}%, so a manager can approve")
    return "; ".join(why) + "." if why else "Waiting for a manager."


def apply_discount(quote: Quote, percent: Decimal) -> None:
    total, _ = margin_after(quote, percent)
    quote.discount_pct, quote.discount_amount, quote.total = q2(percent), quote.subtotal - total, total


def request_discount(db: Session, user: User, quote: Quote, percent: float, reason: Optional[str] = None) -> DiscountRequest:
    percent = q2(percent)
    if percent <= 0 or percent > MAX_DISCOUNT_PCT:
        raise OpError(422, f"Discount must be between 0 and {float(MAX_DISCOUNT_PCT):g}%.")
    if quote.status not in ("draft", "sent"):
        raise OpError(409, f"Quotation {quote.number} is {quote.status}; only open quotations can be discounted.")
    if db.scalar(select(DiscountRequest).where(DiscountRequest.quote_id == quote.id, DiscountRequest.status == "pending")):
        raise OpError(409, "A discount on this quotation is already waiting for approval.")
    tenant = db.get(Tenant, quote.tenant_id)
    total, margin = margin_after(quote, percent)
    within_limit = percent <= user.discount_limit_pct
    margin_ok = margin >= tenant.min_margin_pct
    req = DiscountRequest(tenant_id=quote.tenant_id, quote_id=quote.id, requested_by=user.id, percent=percent,
                          margin_after_pct=margin, created_at=local_now())
    lead = lead_of_quote(db, quote)
    if within_limit and margin_ok:
        req.status, req.decided_by, req.decided_at = "auto_approved", None, req.created_at
        req.reason = f"Within {user.name}'s {float(user.discount_limit_pct):g}% limit and the {float(tenant.min_margin_pct):g}% minimum margin"
        apply_discount(quote, percent)
        text = f"{float(percent):g}% discount applied to {quote.number} (new total QAR {quote.total:,.2f}, margin {margin}%)"
    else:
        why = []
        if not within_limit:
            why.append(f"above {user.name}'s {float(user.discount_limit_pct):g}% limit")
        if not margin_ok:
            why.append(f"margin {margin}% is below the {float(tenant.min_margin_pct):g}% minimum")
        req.status = "pending"
        req.reason = (reason.strip()[:120] + ". " if reason and reason.strip() else "") + "Needs approval: " + " and ".join(why)
        text = f"{float(percent):g}% discount on {quote.number} sent for approval ({' and '.join(why)})"
    db.add(req)
    db.flush()
    if lead:
        note(db, user, lead, "discount", text)
    audit(db, quote.tenant_id, f"discount.{req.status}" if req.status == "pending" else "discount.auto_approve",
          user.id, "quote", quote.number, after={"percent": float(percent), "margin_after_pct": float(margin)})
    db.commit()
    return req


def decide_discount(db: Session, user: User, req: DiscountRequest, approve: bool, reason: Optional[str] = None,
                    override: bool = False) -> DiscountRequest:
    if not has_permission(user, "discounts.approve"):
        raise OpError(403, "Your role does not allow this.")
    if req.status != "pending":
        raise OpError(409, f"This request was already {req.status.replace('_', '-')}.")
    quote = db.get(Quote, req.quote_id)
    tenant = db.get(Tenant, req.tenant_id)
    reason = (reason or "").strip()[:200]
    total, margin = margin_after(quote, req.percent)          # recomputed: prices may have changed since
    if approve:
        if quote.status not in ("draft", "sent"):
            raise OpError(409, f"Quotation {quote.number} is {quote.status}; it can't be discounted any more.")
        if margin < tenant.min_margin_pct:
            if user.role != "owner":
                raise OpError(409, f"Margin after this discount is {margin}%, below the {float(tenant.min_margin_pct):g}% "
                                   "minimum. Only the owner can approve it, with a reason.")
            if not override or len(reason) < 5:
                raise OpError(422, "Below the minimum margin: tick override and write the reason.")
        apply_discount(quote, req.percent)
        if quote.status == "draft":
            quote.status, quote.sent_at = "sent", local_now()
            quote.valid_until = quote.sent_at + timedelta(days=QUOTE_VALID_DAYS)
    elif not reason:
        raise OpError(422, "Write a short reason so the salesperson can tell the customer.")
    req.status = "approved" if approve else "rejected"
    req.decided_by, req.decided_at, req.margin_after_pct = user.id, local_now(), margin
    req.reason = reason or (f"Approved by {user.name}")
    lead = lead_of_quote(db, quote)
    if lead:
        note(db, user, lead, "discount",
             f"{float(req.percent):g}% discount on {quote.number} {req.status} by {user.name}"
             + (f" (new total QAR {quote.total:,.2f})" if approve else f": {reason}"))
    audit(db, req.tenant_id, "discount.approve" if approve else "discount.reject", user.id, "quote", quote.number,
          after={"percent": float(req.percent), "margin_after_pct": float(margin), "reason": req.reason,
                 "override": bool(approve and margin < tenant.min_margin_pct)})
    db.commit()
    return req


# ------------------------------------------------------------------ orders (feature 8)
def pick_batch(product: Product, qty: Decimal, preferred: Optional[int] = None) -> Optional[Batch]:
    """The batch to supply a whole line from: the suggested one if it still covers it, else the oldest that does."""
    batches = [b for b in product.batches if b.qty_left >= qty]
    return next((b for b in batches if b.id == preferred), None) or (batches[0] if batches else None)


def confirm_order(db: Session, user: User, order: Order) -> Order:
    if order.status != "pending":
        raise OpError(409, f"Order {order.number} is {order.status}; only pending orders can be confirmed.")
    picks, short = [], []
    for line in order.lines:
        product = db.get(Product, line.product_id)
        batch = pick_batch(product, line.quantity, line.batch_id)
        if batch is None:
            biggest = max((b.qty_left for b in product.batches), default=Decimal(0))
            short.append(f"{product.name}: needs {float(line.quantity):g}, largest batch has {float(biggest):g}")
        picks.append((line, product, batch))
    if short:
        raise OpError(409, "No single batch covers: " + "; ".join(short)
                      + ". Receive a new batch or change the quantity, so the customer gets one shade.")
    now = local_now()
    for line, product, batch in picks:
        batch.qty_left -= line.quantity
        line.batch_id = batch.id
        db.add(StockMovement(tenant_id=order.tenant_id, product_id=product.id, batch_id=batch.id, qty=-line.quantity,
                             reason="sale", reference=order.number, user_id=user.id, created_at=now))
    order.status = "confirmed"
    order.created_by = order.created_by or user.id
    quote = db.get(Quote, order.quote_id) if order.quote_id else None
    lead = lead_of_quote(db, quote) if quote else None
    if quote:
        quote.status = "accepted"
    if lead:
        lead.stage, lead.stage_changed_at, lead.lost_reason = "won", now, None
        batches = ", ".join(f"{p.sku} from batch {b.code}" for _, p, b in picks)
        note(db, user, lead, "order", f"Order {order.number} confirmed (QAR {order.total:,.2f}). Stock taken: {batches}")
    audit(db, order.tenant_id, "order.confirm", user.id, "order", order.number,
          after={"batches": {p.sku: b.code for _, p, b in picks}, "total": float(order.total)})
    db.commit()
    return order


def order_from_quote(db: Session, user: User, quote: Quote) -> Order:
    """Customer accepted a quotation in the showroom: make the order and confirm it in one go."""
    if quote.status not in ("sent", "accepted"):
        raise OpError(409, f"Quotation {quote.number} is {quote.status}; only a sent quotation can become an order.")
    if db.scalar(select(DiscountRequest).where(DiscountRequest.quote_id == quote.id, DiscountRequest.status == "pending")):
        raise OpError(409, "A discount on this quotation is still waiting for approval.")
    if db.scalar(select(Order).where(Order.quote_id == quote.id, Order.status != "cancelled")):
        raise OpError(409, f"Quotation {quote.number} already has an order.")
    factor = quote.total / quote.subtotal if quote.subtotal else Decimal(1)
    order = Order(tenant_id=quote.tenant_id, number=next_number(db, Order, quote.tenant_id, "O"), quote_id=quote.id,
                  customer_id=quote.customer_id, created_by=user.id, status="pending", total=quote.total,
                  cost_total=quote.cost_total, created_at=local_now())
    for ql in quote.lines:
        order.lines.append(OrderLine(product_id=ql.product_id, batch_id=ql.batch_id, quantity=ql.quantity,
                                     unit_price=q2(ql.unit_price * factor), unit_cost=ql.unit_cost,
                                     line_total=q2(ql.line_total * factor)))
    db.add(order)
    db.flush()
    try:
        return confirm_order(db, user, order)
    except OpError:
        db.rollback()                         # no half-made order if the stock isn't there
        raise


def deliver_order(db: Session, user: User, order: Order) -> Order:
    if order.status != "confirmed":
        raise OpError(409, f"Order {order.number} is {order.status}; only confirmed orders can be delivered.")
    order.status, order.delivered_at = "delivered", local_now()
    quote = db.get(Quote, order.quote_id) if order.quote_id else None
    lead = lead_of_quote(db, quote) if quote else None
    if lead:
        note(db, user, lead, "order", f"Order {order.number} delivered")
    audit(db, order.tenant_id, "order.deliver", user.id, "order", order.number)
    db.commit()
    return order


def cancel_order(db: Session, user: User, order: Order, reason: str) -> Order:
    if order.status not in ("pending", "confirmed"):
        raise OpError(409, f"Order {order.number} is {order.status} and can't be cancelled.")
    reason = (reason or "").strip()
    if len(reason) < 3:
        raise OpError(422, "Write why the order is cancelled.")
    returned = []
    if order.status == "confirmed":                  # stock was taken: put it back in the same batch
        now = local_now()
        for line in order.lines:
            batch = db.get(Batch, line.batch_id) if line.batch_id else None
            if batch:
                batch.qty_left += line.quantity
                returned.append(batch.code)
                db.add(StockMovement(tenant_id=order.tenant_id, product_id=line.product_id, batch_id=batch.id,
                                     qty=line.quantity, reason="adjust", reference=f"{order.number} cancelled",
                                     user_id=user.id, created_at=now))
    before = order.status
    order.status = "cancelled"
    quote = db.get(Quote, order.quote_id) if order.quote_id else None
    lead = lead_of_quote(db, quote) if quote else None
    if quote and quote.status == "accepted":
        quote.status = "sent"
    if lead:
        if lead.stage == "won":
            lead.stage, lead.stage_changed_at = "negotiation", local_now()
        note(db, user, lead, "order", f"Order {order.number} cancelled: {reason[:200]}"
             + (f". Stock returned to {', '.join(returned)}" if returned else ""))
    audit(db, order.tenant_id, "order.cancel", user.id, "order", order.number, before={"status": before},
          after={"status": "cancelled", "reason": reason[:200], "stock_returned_to": returned})
    db.commit()
    return order


# ------------------------------------------------------------------ inventory
def receive_stock(db: Session, user: User, product: Product, qty: float, shade: Optional[str] = None,
                  code: Optional[str] = None) -> Batch:
    qty = q2(qty)
    if qty <= 0 or qty > 100000:
        raise OpError(422, "Quantity must be more than 0.")
    now = local_now()
    n = len(product.batches)
    code = (code or "").strip()[:30]
    if code and db.scalar(select(Batch).where(Batch.tenant_id == product.tenant_id, Batch.code == code)):
        raise OpError(409, f"Batch code {code} already exists.")
    while not code or db.scalar(select(Batch).where(Batch.tenant_id == product.tenant_id, Batch.code == code)):
        code = f"{product.sku}-{now.strftime('%y%m%d')}-{chr(65 + n % 26)}"
        n += 1
    batch = Batch(tenant_id=product.tenant_id, product_id=product.id, code=code,
                  shade=(shade or "").strip()[:20] or None, received_at=now, received_qty=qty, qty_left=qty)
    db.add(batch)
    db.flush()
    db.add(StockMovement(tenant_id=product.tenant_id, product_id=product.id, batch_id=batch.id, qty=qty,
                         reason="receive", reference=code, user_id=user.id, created_at=now))
    audit(db, product.tenant_id, "stock.receive", user.id, "product", product.sku,
          after={"batch": code, "qty": float(qty), "shade": batch.shade})
    db.commit()
    db.refresh(product)
    return batch


def change_price(db: Session, user: User, product: Product, price: float, cost_price: Optional[float] = None) -> Product:
    price = q2(price)
    cost = q2(cost_price) if cost_price is not None else product.cost_price
    if price <= 0 or cost <= 0:
        raise OpError(422, "Prices must be more than 0.")
    if price <= cost:
        raise OpError(422, f"Selling price must be above the cost price (QAR {cost:,.2f}).")
    before = {"price": float(product.price), "cost_price": float(product.cost_price)}
    product.price, product.cost_price = price, cost
    audit(db, product.tenant_id, "price.change", user.id, "product", product.sku, before=before,
          after={"price": float(price), "cost_price": float(cost)})
    db.commit()
    return product
