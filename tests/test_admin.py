"""Nova Studio step 4: the working admin (dashboard, leads, quotes/orders, inventory, approvals).

Covers the two chosen rules end to end: feature 5 (margin guardrail on discounts) and feature 8
(an order is supplied from ONE batch), plus role and company isolation on every new route."""
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend import admin_api, catalog, crm_bridge, main
from backend.db.database import Base, SessionLocal, engine, init_db
from backend.db.models import AuditLog, Batch, DiscountRequest, Lead, Order, Product, Quote, StockMovement, User
from backend.db.seed import DEMO_PASSWORD, seed_tenant

client = TestClient(main.app)


@pytest.fixture(scope="module", autouse=True)
def demo_db():
    Base.metadata.drop_all(bind=engine)
    init_db()
    with SessionLocal() as db:
        seed_tenant(db, today=date(2026, 11, 10))
        seed_tenant(db, slug="other-tiles", name="Other Tiles", today=date(2026, 11, 10))
    crm_bridge.refresh_catalog(force=True)
    yield
    Base.metadata.drop_all(bind=engine)
    catalog.replace_catalog(catalog.load_catalog(), catalog.load_sanitary())


@pytest.fixture(autouse=True)
def fresh_limits():
    admin_api.login_limiter._hits.clear()


_tokens = {}


def H(who: str, company: str = "ceramica-nova") -> dict:
    key = (who, company)
    if key not in _tokens:
        r = client.post("/api/admin/login", json={"company": company, "email": f"{who}@ceramicanova.demo",
                                                  "password": DEMO_PASSWORD})
        assert r.status_code == 200, r.text
        _tokens[key] = r.json()["token"]
    return {"Authorization": f"Bearer {_tokens[key]}"}


def db():
    return SessionLocal()


def omar_id():
    with db() as s:
        return s.scalar(select(User).where(User.email == "omar@ceramicanova.demo")).id


def open_quote(owner_email="omar@ceramicanova.demo", status="sent"):
    """A sent quotation with no discount yet, belonging to one of `owner_email`'s open leads."""
    with db() as s:
        owner = s.scalar(select(User).where(User.email == owner_email))
        for q in s.scalars(select(Quote).join(Lead, Lead.id == Quote.lead_id).where(
                Lead.owner_id == owner.id, Quote.status == status, Quote.discount_pct == 0,
                Lead.stage.in_(("quote_sent", "negotiation"))).order_by(Quote.id)):
            if not s.scalar(select(DiscountRequest).where(DiscountRequest.quote_id == q.id)) and \
                    not s.scalar(select(Order).where(Order.quote_id == q.id)):
                return q.id
    raise AssertionError("no open quote in the seed")


# ------------------------------------------------------------------ dashboard
def test_dashboard_numbers_are_real():
    d = client.get("/api/admin/dashboard?days=30", headers=H("manager")).json()
    with db() as s:
        orders = s.scalars(select(Order).where(Order.tenant_id == 1, Order.status.in_(("confirmed", "delivered")))).all()
        start, end = d["period"]["start"], d["period"]["end"]
        expected = sum(float(o.total) for o in orders if start <= o.created_at.isoformat() < end)
    assert d["scope"] == "company" and abs(d["kpis"]["revenue"] - round(expected, 2)) < 0.05
    assert len(d["sales"]) in (30, 31) and abs(sum(x["revenue"] for x in d["sales"]) - d["kpis"]["revenue"]) < 0.5
    funnel = [x["count"] for x in d["pipeline"]["funnel"]]
    assert funnel == sorted(funnel, reverse=True)                  # a funnel only narrows
    assert d["top_products"] and all("why" in c for c in d["copilot"])


def test_salesperson_dashboard_is_their_own():
    mine = client.get("/api/admin/dashboard?days=90", headers=H("omar")).json()
    company = client.get("/api/admin/dashboard?days=90", headers=H("manager")).json()
    assert mine["scope"] == "my leads" and 0 < mine["kpis"]["revenue"] < company["kpis"]["revenue"]
    assert mine["approvals"] == []
    assert client.get("/api/admin/dashboard?days=12", headers=H("manager")).status_code == 422
    assert client.get("/api/admin/dashboard", headers=H("warehouse")).status_code == 403


def test_board_viewer_sees_company_figures_read_only():
    board = client.get("/api/admin/dashboard?days=90", headers=H("viewer")).json()
    company = client.get("/api/admin/dashboard?days=90", headers=H("manager")).json()
    assert board["scope"] == "company" and board["kpis"]["revenue"] == company["kpis"]["revenue"] > 0
    assert board["approvals"] == []


# ------------------------------------------------------------------ leads
def test_lead_search_filters_and_isolation():
    r = client.get("/api/admin/leads/search?stage=negotiation&limit=5", headers=H("omar")).json()
    assert all(x["stage"] == "negotiation" and x["owner"] == "Omar Khalil" for x in r["items"])
    one = r["items"][0]
    hit = client.get(f"/api/admin/leads/search?q={one['number']}", headers=H("omar")).json()
    assert hit["total"] == 1
    # Lina's lead is invisible to Omar, and another company's lead id is invisible to everyone here
    with db() as s:
        lina_lead = s.scalar(select(Lead).join(User, User.id == Lead.owner_id).where(User.email == "lina@ceramicanova.demo"))
        other = s.scalar(select(Lead).where(Lead.tenant_id == 2))
    assert client.get(f"/api/admin/leads/{lina_lead.id}", headers=H("omar")).status_code == 404
    assert client.get(f"/api/admin/leads/{other.id}", headers=H("manager")).status_code == 404


def test_lead_stage_note_and_assign():
    lead = client.get("/api/admin/leads/search?stage=qualified&limit=1", headers=H("omar")).json()["items"][0]
    url = f"/api/admin/leads/{lead['id']}"
    assert client.post(url + "/stage", json={"stage": "lost"}, headers=H("omar")).status_code == 422   # reason needed
    assert client.post(url + "/stage", json={"stage": "won"}, headers=H("omar")).status_code == 409    # only via an order
    r = client.post(url + "/stage", json={"stage": "lost", "lost_reason": "price", "comment": "Went cheaper"},
                    headers=H("omar"))
    assert r.status_code == 200 and r.json()["lost_reason"] == "price"
    assert client.post(url + "/notes", json={"text": "Called, no answer", "kind": "call"}, headers=H("omar")).status_code == 200
    assert client.post(url + "/assign", json={"owner_id": omar_id()}, headers=H("omar")).status_code == 403
    with db() as s:
        lina = s.scalar(select(User).where(User.email == "lina@ceramicanova.demo"))
    assert client.post(url + "/assign", json={"owner_id": lina.id}, headers=H("manager")).status_code == 200
    detail = client.get(url, headers=H("manager")).json()
    texts = [t["text"] for t in detail["timeline"]]
    assert texts[0].startswith("Assigned to Lina Mansour") and "Called, no answer" in texts
    assert client.get(url, headers=H("omar")).status_code == 404               # no longer Omar's
    assert client.post(url + "/notes", json={"text": "x"}, headers=H("viewer")).status_code == 403


# ------------------------------------------------------------------ feature 5: margin guardrail
def test_small_discount_within_limit_is_applied_at_once():
    qid = open_quote()
    r = client.post(f"/api/admin/quotes/{qid}/discount", json={"percent": 4}, headers=H("omar"))
    assert r.status_code == 200 and r.json()["status"] == "auto_approved"
    q = r.json()["quote"]
    assert q["discount_pct"] == 4 and abs(q["total"] - round(q["subtotal"] * 0.96, 2)) < 0.02


def test_discount_above_limit_waits_and_manager_decides():
    qid = open_quote()
    r = client.post(f"/api/admin/quotes/{qid}/discount", json={"percent": 9, "reason": "Big order"}, headers=H("omar"))
    assert r.json()["status"] == "pending" and r.json()["quote"]["discount_pct"] == 0
    assert client.post(f"/api/admin/quotes/{qid}/discount", json={"percent": 3}, headers=H("omar")).status_code == 409
    pending = client.get("/api/admin/approvals", headers=H("manager")).json()["pending"]
    req = next(p for p in pending if p["quote_id"] == qid)
    assert client.get("/api/admin/approvals", headers=H("omar")).status_code == 403
    assert client.post(f"/api/admin/approvals/{req['id']}", json={"decision": "reject"}, headers=H("manager")).status_code == 422
    ok = client.post(f"/api/admin/approvals/{req['id']}", json={"decision": "approve"}, headers=H("manager"))
    assert ok.status_code == 200 and ok.json()["quote"]["discount_pct"] == 9
    again = client.post(f"/api/admin/approvals/{req['id']}", json={"decision": "approve"}, headers=H("manager"))
    assert again.status_code == 409
    with db() as s:
        assert s.scalar(select(AuditLog).where(AuditLog.action == "discount.approve", AuditLog.entity_id == req["quote"],
                                               AuditLog.user_id.is_not(None)))


def test_below_minimum_margin_only_owner_with_reason():
    qid = open_quote()
    with db() as s:            # make the margin thin so 10% goes under the 25% minimum
        q = s.get(Quote, qid)
        q.cost_total = (q.subtotal * Decimal("0.70")).quantize(Decimal("0.01"))
        s.commit()
    r = client.post(f"/api/admin/quotes/{qid}/discount", json={"percent": 10}, headers=H("manager"))
    assert r.json()["status"] == "pending" and "below" in r.json()["reason"]   # within Sara's 10% limit, but margin too low
    rid = client.get("/api/admin/approvals", headers=H("manager")).json()["pending"]
    rid = next(p["id"] for p in rid if p["quote_id"] == qid)
    blocked = client.post(f"/api/admin/approvals/{rid}", json={"decision": "approve"}, headers=H("manager"))
    assert blocked.status_code == 409 and "Only the owner" in blocked.json()["detail"]
    assert client.post(f"/api/admin/approvals/{rid}", json={"decision": "approve"}, headers=H("owner")).status_code == 422
    ok = client.post(f"/api/admin/approvals/{rid}", json={"decision": "approve", "override": True,
                                                         "reason": "Contractor, repeat business"}, headers=H("owner"))
    assert ok.status_code == 200
    with db() as s:
        a = s.scalars(select(AuditLog).where(AuditLog.action == "discount.approve").order_by(AuditLog.id.desc())).first()
        assert a.after["override"] is True and a.after["reason"] == "Contractor, repeat business"


# ------------------------------------------------------------------ feature 8: one batch per order
def make_pending_order(sku: str, qty: str) -> int:
    """A shop order for `qty` of `sku`, the way the public checkout creates it."""
    r = client.post("/api/shop/checkout", json={"type": "order", "items": [{"sku": sku, "area_m2": float(qty)}],
                                                "name": "Test Buyer", "phone": "+974 5512 3456", "delivery_area": "Doha"})
    assert r.status_code == 200, r.text
    with db() as s:
        return s.scalar(select(Order).where(Order.number == r.json()["order_number"])).id


def test_confirm_takes_stock_from_one_batch_and_wins_the_lead():
    from backend.shop_api import submit_limiter
    submit_limiter._hits.clear()
    oid = make_pending_order("CN006", "10")
    with db() as s:
        o = s.get(Order, oid)
        line = o.lines[0]
        p = s.get(Product, line.product_id)
        before = {b.id: b.qty_left for b in p.batches}
        lead_owner = s.get(Lead, s.get(Quote, o.quote_id).lead_id).owner
    who = "owner" if lead_owner is None else lead_owner.email.split("@")[0]
    detail = client.get(f"/api/admin/orders/{oid}", headers=H(who)).json()
    assert detail["can"]["confirm"] and detail["lines"][0]["one_batch_ok"]
    assert client.post(f"/api/admin/orders/{oid}/confirm", headers=H("warehouse")).status_code == 403
    r = client.post(f"/api/admin/orders/{oid}/confirm", headers=H(who))
    assert r.status_code == 200 and r.json()["status"] == "confirmed"
    with db() as s:
        o = s.get(Order, oid)
        b = s.get(Batch, o.lines[0].batch_id)
        assert before[b.id] - b.qty_left == o.lines[0].quantity                       # taken from ONE batch
        assert sum(before.values()) - sum(x.qty_left for x in s.get(Product, b.product_id).batches) == o.lines[0].quantity
        assert s.scalar(select(StockMovement).where(StockMovement.reference == o.number, StockMovement.reason == "sale"))
        assert s.get(Lead, s.get(Quote, o.quote_id).lead_id).stage == "won"
    # deliver is the warehouse's job; then it can't be cancelled
    assert client.post(f"/api/admin/orders/{oid}/deliver", headers=H("warehouse")).json()["status"] == "delivered"
    assert client.post(f"/api/admin/orders/{oid}/cancel", json={"reason": "test"}, headers=H("owner")).status_code == 409


def test_confirm_refuses_when_no_single_batch_covers_the_line():
    from backend.shop_api import submit_limiter
    submit_limiter._hits.clear()
    with db() as s:
        p = s.scalar(select(Product).where(Product.sku == "CN002"))
        biggest = max(b.qty_left for b in p.batches)
        total = sum(b.qty_left for b in p.batches)
    if total <= biggest + 1:
        pytest.skip("this product has only one batch with stock")
    oid = make_pending_order("CN002", str(min(total, biggest + 1)))
    r = client.post(f"/api/admin/orders/{oid}/confirm", headers=H("owner"))
    assert r.status_code == 409 and "No single batch" in r.json()["detail"]
    with db() as s:
        assert s.get(Order, oid).status == "pending"
        assert sum(b.qty_left for b in s.scalar(select(Product).where(Product.sku == "CN002")).batches) == total
    # receiving a big enough batch fixes it
    assert client.post("/api/admin/inventory/CN002/receive", json={"qty": 500, "shade": "Warm"},
                       headers=H("warehouse")).status_code == 200
    assert client.post(f"/api/admin/orders/{oid}/confirm", headers=H("owner")).status_code == 200


def test_cancel_returns_stock_to_the_same_batch():
    qid = open_quote("lina@ceramicanova.demo")
    r = client.post(f"/api/admin/quotes/{qid}/order", headers=H("lina"))
    if r.status_code == 409:
        pytest.skip(r.json()["detail"])
    oid = r.json()["id"]
    with db() as s:
        o = s.get(Order, oid)
        taken = {l.batch_id: l.quantity for l in o.lines}
        left = {bid: s.get(Batch, bid).qty_left for bid in taken}
    assert client.post(f"/api/admin/quotes/{qid}/order", headers=H("lina")).status_code == 409   # one order per quote
    c = client.post(f"/api/admin/orders/{oid}/cancel", json={"reason": "Customer changed mind"}, headers=H("lina"))
    assert c.status_code == 200 and c.json()["status"] == "cancelled"
    with db() as s:
        for bid, qty in taken.items():
            assert s.get(Batch, bid).qty_left == left[bid] + qty
        assert s.get(Quote, qid).status == "sent"


# ------------------------------------------------------------------ inventory + prices
def test_inventory_roles_and_price_change_audited():
    inv = client.get("/api/admin/inventory", headers=H("omar")).json()
    assert inv["items"] and "cost_price" not in inv["items"][0] and inv["can"] == {"receive": False, "price": False}
    assert client.post("/api/admin/inventory/CN004/receive", json={"qty": 50}, headers=H("omar")).status_code == 403
    assert client.post("/api/admin/products/CN004/price", json={"price": 140}, headers=H("manager")).status_code == 403
    assert client.post("/api/admin/products/CN004/price", json={"price": 1}, headers=H("owner")).status_code == 422  # below cost
    r = client.post("/api/admin/products/CN004/price", json={"price": 139.5}, headers=H("owner"))
    assert r.status_code == 200 and catalog.CATALOG["CN004"].price_per_m2 == 139.5     # shop + AI see it at once
    with db() as s:
        a = s.scalars(select(AuditLog).where(AuditLog.action == "price.change")).first()
        assert a.before["price"] != a.after["price"] == 139.5
    detail = client.get("/api/admin/inventory/CN004", headers=H("warehouse")).json()
    assert detail["batches"] and detail["cost_price"] is None and detail["movements"]
    assert client.get("/api/admin/inventory/NOPE", headers=H("warehouse")).status_code == 404


def test_other_company_cannot_touch_our_records():
    with db() as s:
        q = s.scalar(select(Quote).where(Quote.tenant_id == 1))
        o = s.scalar(select(Order).where(Order.tenant_id == 1))
        d = s.scalar(select(DiscountRequest).where(DiscountRequest.tenant_id == 1))
    other = H("owner", "other-tiles")
    assert client.get(f"/api/admin/quotes/{q.id}", headers=other).status_code == 404
    assert client.post(f"/api/admin/orders/{o.id}/cancel", json={"reason": "nope"}, headers=other).status_code == 404
    assert client.post(f"/api/admin/approvals/{d.id}", json={"decision": "reject", "reason": "x"},
                       headers=other).status_code == 404


def test_admin_page_is_served():
    r = client.get("/admin")
    assert r.status_code == 200 and "Nova Studio" in r.text
    photo = client.get("/admin/showroom.jpg")
    assert photo.status_code == 200 and photo.headers["content-type"] == "image/jpeg"


def test_waiting_discount_says_why():
    qid = open_quote("yusuf@ceramicanova.demo")
    client.post(f"/api/admin/quotes/{qid}/discount", json={"percent": 7}, headers=H("yusuf"))
    q = client.get(f"/api/admin/quotes/{qid}", headers=H("manager")).json()
    why = next(d["why"] for d in q["discounts"] if d["status"] == "pending")
    assert why.startswith("Over Yusuf's 5% limit") and "a manager can approve" in why
    pending = client.get("/api/admin/approvals", headers=H("manager")).json()["pending"]
    assert all(p["why"] for p in pending)
