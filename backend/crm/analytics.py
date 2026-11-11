"""Nova Studio dashboard numbers. Every figure is calculated from the database - nothing is typed in.

Definitions (also shown in the screen's tooltips):
- Revenue / gross profit / orders: orders confirmed or delivered in the period (pending online orders
  are not revenue until staff confirm them). Gross profit = order total - cost of the goods.
- New leads: leads created in the period. Conversion rate: orders / new leads in the period.
- Change: this period against the period of the same length just before it.
- Days of stock: stock left / average m² (or sets) sold per day over the last 30 days.
"""
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session

from backend.auth import has_permission
from backend.crm.operations import why_waiting
from backend.db.database import local_now
from backend.db.models import (Activity, Batch, Customer, DiscountRequest, Lead, Order, OrderLine, Product, Quote,
                               StockMovement, User)

REVENUE_STATUSES = ("confirmed", "delivered")
OPEN_STAGES = ("new", "qualified", "quote_sent", "negotiation")
STALLED_DAYS = 3
STOCK_WINDOW_DAYS = 30


def f(x) -> float:
    return round(float(x or 0), 2)


def pct_change(now: float, before: float) -> Optional[float]:
    return None if not before else round((now - before) / before * 100, 1)


def own_leads(user: User):
    """Salespeople see figures for their own leads only; managers, the owner and read-only board
    members (reports.view) see the whole company."""
    return None if has_permission(user, "leads.view_all") or has_permission(user, "reports.view") else user.id


def orders_query(user: User, start: datetime, end: datetime):
    q = (select(Order).where(Order.tenant_id == user.tenant_id, Order.status.in_(REVENUE_STATUSES),
                             Order.created_at >= start, Order.created_at < end))
    owner = own_leads(user)
    if owner:
        q = q.join(Quote, Quote.id == Order.quote_id).join(Lead, Lead.id == Quote.lead_id).where(Lead.owner_id == owner)
    return q


def leads_query(user: User):
    q = select(Lead).where(Lead.tenant_id == user.tenant_id)
    owner = own_leads(user)
    return q.where(Lead.owner_id == owner) if owner else q


# ------------------------------------------------------------------ KPIs
def kpis(db: Session, user: User, start: datetime, end: datetime) -> dict:
    span = end - start
    out = {}
    for label, (a, b) in {"now": (start, end), "before": (start - span, start)}.items():
        orders = db.scalars(orders_query(user, a, b)).all()
        revenue = sum((o.total for o in orders), Decimal(0))
        profit = sum((o.total - o.cost_total for o in orders), Decimal(0))
        new_leads = db.scalar(select(func.count()).select_from(
            leads_query(user).where(Lead.created_at >= a, Lead.created_at < b).subquery()))
        out[label] = {"revenue": f(revenue), "gross_profit": f(profit), "orders": len(orders), "new_leads": new_leads,
                      "conversion_pct": round(len(orders) / new_leads * 100, 1) if new_leads else 0.0,
                      "margin_pct": round(float(profit / revenue * 100), 1) if revenue else 0.0}
    n, b = out["now"], out["before"]
    return {**n, "change": {"revenue": pct_change(n["revenue"], b["revenue"]),
                            "gross_profit": pct_change(n["gross_profit"], b["gross_profit"]),
                            "orders": pct_change(n["orders"], b["orders"]),
                            "new_leads": pct_change(n["new_leads"], b["new_leads"]),
                            "conversion_pp": round(n["conversion_pct"] - b["conversion_pct"], 1)}}


def sales_series(db: Session, user: User, start: datetime, end: datetime) -> list[dict]:
    days = defaultdict(lambda: [Decimal(0), Decimal(0)])
    for o in db.scalars(orders_query(user, start, end)):
        d = days[o.created_at.date()]
        d[0] += o.total
        d[1] += o.total - o.cost_total
    out, day = [], start.date()
    while day <= (end - timedelta(seconds=1)).date():
        r, p = days.get(day, (0, 0))
        out.append({"date": day.isoformat(), "revenue": f(r), "gross_profit": f(p)})
        day += timedelta(days=1)
    return out


# ------------------------------------------------------------------ pipeline + projects
def _reached(db: Session, leads: list[Lead]) -> dict[str, int]:
    ids = [l.id for l in leads]
    quoted = set(db.scalars(select(Quote.lead_id).where(Quote.lead_id.in_(ids)))) if ids else set()
    negotiated = set(db.scalars(select(Activity.lead_id).where(
        Activity.lead_id.in_(ids), Activity.type == "stage_change",
        or_(Activity.text.like("Customer asked%"), Activity.text.like("%→ Negotiation%"))))) if ids else set()
    counts = {"new": len(leads), "qualified": 0, "quote_sent": 0, "negotiation": 0, "won": 0}
    for l in leads:
        q = l.id in quoted or l.stage in ("quote_sent", "negotiation", "won")
        if q or l.stage == "qualified" or (l.stage == "lost" and l.lost_reason != "no_response"):
            counts["qualified"] += 1
        if q:
            counts["quote_sent"] += 1
        if l.stage == "negotiation" or l.id in negotiated:
            counts["negotiation"] += 1
        if l.stage == "won":
            counts["won"] += 1
    return counts


def pipeline(db: Session, user: User, start: datetime, end: datetime) -> dict:
    """Funnel of the leads created in the period: how many reached each stage."""
    leads = db.scalars(leads_query(user).where(Lead.created_at >= start, Lead.created_at < end)).all()
    funnel = _reached(db, leads)
    # Value of the open pipeline: latest quote of every open lead that has one.
    open_leads = db.scalars(leads_query(user).where(Lead.stage.in_(("quote_sent", "negotiation")))).all()
    value = Decimal(0)
    for l in open_leads:
        q = db.scalar(select(Quote).where(Quote.lead_id == l.id, Quote.status.in_(("sent", "draft")))
                      .order_by(Quote.created_at.desc()))
        value += q.total if q else 0
    return {"funnel": [{"stage": s, "count": funnel[s]} for s in ("new", "qualified", "quote_sent", "negotiation", "won")],
            "lost": sum(1 for l in leads if l.stage == "lost"), "open_value": f(value)}


def projects(db: Session, user: User, start: datetime) -> dict:
    """Project snapshot: where every live customer project is right now."""
    by_stage: dict[str, int] = {}
    for l in db.scalars(leads_query(user).where(Lead.stage.in_(OPEN_STAGES))):
        by_stage[l.stage] = by_stage.get(l.stage, 0) + 1
    oq = select(Order).where(Order.tenant_id == user.tenant_id)
    owner = own_leads(user)
    if owner:
        oq = oq.join(Quote, Quote.id == Order.quote_id).join(Lead, Lead.id == Quote.lead_id).where(Lead.owner_id == owner)
    orders = db.scalars(oq).all()
    in_progress = sum(1 for o in orders if o.status in ("pending", "confirmed"))
    completed = sum(1 for o in orders if o.status == "delivered" and o.delivered_at and o.delivered_at >= start)
    design = by_stage.get("new", 0) + by_stage.get("qualified", 0)
    quoted = by_stage.get("quote_sent", 0) + by_stage.get("negotiation", 0)
    return {"active": design + quoted + in_progress,
            "groups": [{"key": "design", "label": "In design", "count": design},
                       {"key": "quoted", "label": "Quoted", "count": quoted},
                       {"key": "in_progress", "label": "Order in progress", "count": in_progress},
                       {"key": "completed", "label": "Delivered (period)", "count": completed}]}


# ------------------------------------------------------------------ products + stock
def top_products(db: Session, user: User, start: datetime, end: datetime, limit: int = 5) -> list[dict]:
    span = end - start

    def totals(a, b):
        sub = orders_query(user, a, b).subquery()
        rows = db.execute(select(OrderLine.product_id, func.sum(OrderLine.quantity), func.sum(OrderLine.line_total))
                          .join(sub, sub.c.id == OrderLine.order_id).group_by(OrderLine.product_id)).all()
        return {pid: (q, v) for pid, q, v in rows}

    now, before = totals(start, end), totals(start - span, start)
    out = []
    for pid, (qty, value) in sorted(now.items(), key=lambda kv: -kv[1][1])[:limit]:
        p = db.get(Product, pid)
        out.append({"sku": p.sku, "name": p.name, "image": p.image, "unit": p.unit, "quantity": f(qty),
                    "revenue": f(value), "change_pct": pct_change(f(value), f(before.get(pid, (0, 0))[1]))})
    return out


def stock_levels(db: Session, tenant_id: int, now: Optional[datetime] = None) -> list[dict]:
    now = now or local_now()
    since = now - timedelta(days=STOCK_WINDOW_DAYS)
    sold = dict(db.execute(select(StockMovement.product_id, func.sum(StockMovement.qty))
                           .where(StockMovement.tenant_id == tenant_id, StockMovement.reason == "sale",
                                  StockMovement.created_at >= since).group_by(StockMovement.product_id)).all())
    out = []
    for p in db.scalars(select(Product).where(Product.tenant_id == tenant_id, Product.active.is_(True))):
        left = sum((b.qty_left for b in p.batches), Decimal(0))
        per_day = -(sold.get(p.id) or 0) / STOCK_WINDOW_DAYS
        days = round(float(left / Decimal(per_day)), 0) if per_day else None
        largest = max((b.qty_left for b in p.batches), default=Decimal(0))
        status = ("out" if left <= 0 else "critical" if (days is not None and days < 14) or left < p.reorder_point
                  else "low" if days is not None and days < 30 else "ok")
        out.append({"sku": p.sku, "name": p.name, "kind": p.kind, "unit": p.unit, "image": p.image,
                    "price": f(p.price), "cost_price": f(p.cost_price), "stock": f(left), "largest_batch": f(largest),
                    "batches": sum(1 for b in p.batches if b.qty_left > 0), "sold_per_day": round(float(per_day), 2),
                    "days_of_stock": days, "reorder_point": f(p.reorder_point), "status": status})
    rank = {"out": 0, "critical": 1, "low": 2, "ok": 3}
    return sorted(out, key=lambda r: (rank[r["status"]], r["days_of_stock"] if r["days_of_stock"] is not None else 9999))


# ------------------------------------------------------------------ follow-ups, approvals, activity
def stalled_quotes(db: Session, user: User, now: datetime) -> list[dict]:
    """Quotes sent more than STALLED_DAYS ago, lead still open, and nobody has touched the lead since."""
    cutoff = now - timedelta(days=STALLED_DAYS)
    q = (select(Quote, Lead).join(Lead, Lead.id == Quote.lead_id)
         .where(Quote.tenant_id == user.tenant_id, Quote.status == "sent", Quote.sent_at < cutoff,
                Lead.stage.in_(("quote_sent", "negotiation")),
                ~exists().where(and_(Activity.lead_id == Lead.id, Activity.created_at >= cutoff))))
    owner = own_leads(user)
    if owner:
        q = q.where(Lead.owner_id == owner)
    out = []
    for quote, lead in db.execute(q.order_by(Quote.total.desc())).all():
        out.append({"quote": quote.number, "quote_id": quote.id, "lead_id": lead.id, "lead": lead.number,
                    "customer": lead.customer.name, "owner": lead.owner.name if lead.owner else None,
                    "total": f(quote.total), "days_waiting": (now - quote.sent_at).days})
    return out


def pending_discounts(db: Session, user: User) -> list[dict]:
    t_min = None
    out = []
    rows = db.execute(select(DiscountRequest, Quote).join(Quote, Quote.id == DiscountRequest.quote_id)
                      .where(DiscountRequest.tenant_id == user.tenant_id, DiscountRequest.status == "pending")
                      .order_by(DiscountRequest.created_at)).all()
    for d, q in rows:
        lead = db.get(Lead, q.lead_id) if q.lead_id else None
        who = db.get(User, d.requested_by).name if d.requested_by else "Ceramica Nova AI chat"
        out.append({"id": d.id, "quote": q.number, "quote_id": q.id, "lead": lead.number if lead else None,
                    "lead_id": lead.id if lead else None, "customer": db.get(Customer, q.customer_id).name,
                    "percent": f(d.percent), "margin_after_pct": f(d.margin_after_pct), "subtotal": f(q.subtotal),
                    "total_after": f(q.subtotal * (1 - d.percent / 100)), "requested_by": who,
                    "created_at": d.created_at, "reason": d.reason, "why": why_waiting(db, d)})
    return out


def recent_activity(db: Session, user: User, limit: int = 10) -> list[dict]:
    q = (select(Activity, Lead).join(Lead, Lead.id == Activity.lead_id)
         .where(Activity.tenant_id == user.tenant_id))
    owner = own_leads(user)
    if owner:
        q = q.where(Lead.owner_id == owner)
    rows = db.execute(q.order_by(Activity.created_at.desc(), Activity.id.desc()).limit(limit)).all()
    return [{"at": a.created_at, "type": a.type, "text": a.text, "lead": l.number, "lead_id": l.id,
             "customer": l.customer.name} for a, l in rows]


# ------------------------------------------------------------------ copilot cards (rules, not AI)
def copilot_cards(db: Session, user: User, start: datetime, end: datetime, stalled, approvals, stock) -> list[dict]:
    """Suggested actions found by simple, explainable rules over the data. Each card says WHY it appeared."""
    cards = []
    if stalled:
        total = sum(s["total"] for s in stalled)
        cards.append({"kind": "follow_up", "title": f"{len(stalled)} quotation{'s' if len(stalled) > 1 else ''} waiting for a reply",
                      "detail": f"QAR {total:,.0f} in total. The largest is {stalled[0]['quote']} for {stalled[0]['customer']}.",
                      "why": f"Sent more than {STALLED_DAYS} days ago with no activity on the lead since.",
                      "action": {"label": "Draft follow-ups", "view": "followups"}})
    if approvals and has_permission(user, "discounts.approve"):
        ok = [a for a in approvals if a["margin_after_pct"] >= float(user_min_margin(db, user))]
        a = (ok or approvals)[0]
        cards.append({"kind": "approve", "title": f"{len(approvals)} discount{'s' if len(approvals) > 1 else ''} waiting for approval",
                      "detail": f"{a['percent']:g}% on {a['quote']} ({a['customer']}) leaves a {a['margin_after_pct']:.1f}% margin.",
                      "why": "Above the requester's discount limit, or below the minimum margin.",
                      "action": {"label": "Review", "view": "approvals"}})
    low = [s for s in stock if s["status"] in ("out", "critical") and s["kind"] == "tile"]
    if low and has_permission(user, "inventory.view"):
        s = low[0]
        when = f"about {s['days_of_stock']:.0f} days" if s["days_of_stock"] is not None else "an unknown time"
        cards.append({"kind": "restock", "title": f"{s['name']} ({s['sku']}) is running low",
                      "detail": f"{s['stock']:,.0f} m² left, {when} at the current sales rate.",
                      "why": f"Sells {s['sold_per_day']:.1f} m² a day over the last {STOCK_WINDOW_DAYS} days.",
                      "action": {"label": "Restock", "view": "inventory", "sku": s["sku"]}})
    pending_orders = db.scalar(select(func.count()).select_from(Order).where(
        Order.tenant_id == user.tenant_id, Order.status == "pending"))
    if pending_orders and has_permission(user, "orders.create"):
        cards.append({"kind": "confirm", "title": f"{pending_orders} online order{'s' if pending_orders > 1 else ''} to confirm",
                      "detail": "Call the customer to confirm delivery and payment, then confirm the order.",
                      "why": "Placed in the shop; stock is only taken when staff confirm.",
                      "action": {"label": "Open orders", "view": "orders", "filter": "pending"}})
    trend = category_trend(db, user, start, end)
    if trend:
        cards.append({"kind": "trend", "title": f"{trend['label']} demand up {trend['change_pct']:.0f}%",
                      "detail": f"{trend['now']:,.0f} m² sold this period against {trend['before']:,.0f} m² before.",
                      "why": "Compared with the period of the same length just before.",
                      "action": {"label": "View products", "view": "inventory"}})
    return cards


def user_min_margin(db: Session, user: User) -> Decimal:
    from backend.db.models import Tenant
    return db.get(Tenant, user.tenant_id).min_margin_pct


def category_trend(db: Session, user: User, start: datetime, end: datetime) -> Optional[dict]:
    span = end - start

    def m2(a, b):
        sub = orders_query(user, a, b).subquery()
        rows = db.execute(select(Product.attributes, OrderLine.quantity).join(sub, sub.c.id == OrderLine.order_id)
                          .join(Product, Product.id == OrderLine.product_id).where(Product.kind == "tile")).all()
        out = defaultdict(float)
        for attrs, qty in rows:
            out["Outdoor tile" if "outdoor" in (attrs or {}).get("locations", []) else
                "Wall tile" if (attrs or {}).get("surfaces") == ["wall"] else "Indoor floor tile"] += float(qty)
        return out

    now, before = m2(start, end), m2(start - span, start)
    best = None
    for label, v in now.items():
        b = before.get(label, 0)
        ch = pct_change(v, b)
        if ch is not None and ch >= 15 and v >= 50 and (best is None or ch > best["change_pct"]):
            best = {"label": label, "change_pct": ch, "now": v, "before": b}
    return best


# ------------------------------------------------------------------ the whole dashboard
def dashboard(db: Session, user: User, days: int, now: Optional[datetime] = None) -> dict:
    end = now or local_now()
    start = end - timedelta(days=days)
    stock = stock_levels(db, user.tenant_id, end)
    stalled = stalled_quotes(db, user, end)
    approvals = pending_discounts(db, user) if has_permission(user, "discounts.approve") else []
    return {"period": {"days": days, "start": start, "end": end},
            "scope": "company" if own_leads(user) is None else "my leads",
            "kpis": kpis(db, user, start, end),
            "sales": sales_series(db, user, start, end),
            "projects": projects(db, user, start),
            "pipeline": pipeline(db, user, start, end),
            "top_products": top_products(db, user, start, end),
            "inventory_alerts": [s for s in stock if s["status"] != "ok"][:4] or stock[:3],
            "follow_ups": stalled[:8], "approvals": approvals[:8],
            "activity": recent_activity(db, user),
            "copilot": copilot_cards(db, user, start, end, stalled, approvals, stock)}
