"""Nova Studio step 3: Ceramica Nova AI connected to the CRM database.

Products/prices/stock come from the database; a chat becomes a lead (feature 1); chat quotes are
stored in Quotations; a visitor can ask for a call back; a shop checkout continues the chat's lead."""
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend import catalog, crm_bridge, main, shop_api
from backend.db.database import Base, SessionLocal, engine, init_db
from backend.db.models import Activity, ChatSession, Customer, DiscountRequest, Lead, Product, Quote, User
from backend.db.seed import seed_tenant
from backend.tools import check_inventory

client = TestClient(main.app)


@pytest.fixture(scope="module", autouse=True)
def demo_db():
    Base.metadata.drop_all(bind=engine)
    init_db()
    with SessionLocal() as db:
        seed_tenant(db, today=date(2026, 11, 10))
    crm_bridge.refresh_catalog(force=True)
    yield
    Base.metadata.drop_all(bind=engine)
    catalog.replace_catalog(catalog.load_catalog(), catalog.load_sanitary())   # leave the JSON catalog for other tests


@pytest.fixture(autouse=True)
def fresh_limits():
    main.rate_limiter._hits.clear()
    shop_api.submit_limiter._hits.clear()


@pytest.fixture
def db():
    with SessionLocal() as s:
        yield s


def chat(text, sid, user="USR-5550001"):
    r = client.post("/api/v1/chat", json={"user_id": user, "message": text, "session_id": sid})
    assert r.status_code == 200, r.text
    return r.json()


def lead_of(db, sid):
    db.expire_all()
    row = db.scalar(select(ChatSession).where(ChatSession.session_id == sid))
    return row, (db.get(Lead, row.lead_id) if row and row.lead_id else None)


# ------------------------------------------------------------------ products from the database
def test_price_change_in_the_crm_reaches_the_assistant(db):
    p = db.scalar(select(Product).where(Product.sku == "CN004"))
    old = p.price
    p.price = Decimal("150.00")
    db.commit()
    try:
        assert crm_bridge.refresh_catalog(force=True)
        assert catalog.CATALOG["CN004"].price_per_m2 == 150.0
        shown = {x["sku"]: x["price"] for x in client.get("/api/v1/catalog").json()["products"]}
        assert shown["CN004"] == 150.0
    finally:
        p.price = old
        db.commit()
        crm_bridge.refresh_catalog(force=True)
    assert catalog.CATALOG["CN004"].price_per_m2 == float(old)


def test_stock_and_batches_come_from_the_database(db):
    p = db.scalar(select(Product).where(Product.sku == "CN006"))
    assert catalog.CATALOG["CN006"].stock_m2 == float(sum(b.qty_left for b in p.batches))
    biggest = max(b.qty_left for b in p.batches)
    assert check_inventory("CN006", float(biggest) - 1).single_batch_available is True
    assert check_inventory("CN006", float(biggest) + 1).single_batch_available is False


def test_inactive_products_disappear_from_the_assistant(db):
    p = db.scalar(select(Product).where(Product.sku == "CN013"))
    p.active = False
    db.commit()
    try:
        crm_bridge.refresh_catalog(force=True)
        assert "CN013" not in catalog.CATALOG
    finally:
        p.active = True
        db.commit()
        crm_bridge.refresh_catalog(force=True)


# ------------------------------------------------------------------ chat to lead
def test_small_talk_does_not_create_a_lead(db):
    chat("Hi there", "crm-hello")
    row, lead = lead_of(db, "crm-hello")
    assert row is not None and row.messages == 1 and lead is None


def test_project_details_create_a_qualified_lead(db):
    chat("Hi", "crm-bath")
    chat("Bathroom floor, 24 m2, budget around QAR 4000", "crm-bath")
    row, lead = lead_of(db, "crm-bath")
    assert lead is not None and row.messages == 2
    assert lead.source == "showroom_chat" and lead.stage == "qualified" and lead.room == "bathroom"
    assert lead.area_m2 == Decimal("24.00")
    assert db.get(User, lead.owner_id).role == "salesperson"
    assert lead.summary.startswith("Ceramica Nova AI chat:") and "24 m²" in lead.summary
    assert db.get(Customer, lead.customer_id).name.startswith("Chat visitor")
    assert db.scalar(select(func.count()).select_from(Lead).where(Lead.id == lead.id)) == 1   # one lead per chat


def test_chat_quote_is_stored_with_the_same_number(db):
    sid = "crm-quote"
    chat("It's a balcony floor, about 25 square meters, nothing slippery", sid)
    chat("Terra Rustic sounds good", sid)
    r = chat("Yes please", sid)
    number = r["quote"]["quotation_id"]
    assert number.startswith("Q-") and number[2:].isdigit()          # the CRM number, not a random id
    db.expire_all()
    q = db.scalar(select(Quote).where(Quote.number == number))
    _, lead = lead_of(db, sid)
    assert q.channel == "chat" and q.lead_id == lead.id and lead.stage == "quote_sent"
    assert float(q.total) == r["quote"]["total"] and q.cost_total < q.total
    assert number in lead.summary
    # the customer can still open their own quote and its PDF; nobody else can
    assert client.get(f"/api/v1/quotes/{number}", headers={"X-User-Id": "USR-5550001"}).status_code == 200
    assert client.get(f"/api/v1/quotes/{number}/pdf", headers={"X-User-Id": "USR-5550001"}).status_code == 200
    assert client.get(f"/api/v1/quotes/{number}", headers={"X-User-Id": "USR-5550002"}).status_code == 403


def test_large_chat_discount_waits_for_a_manager(db):
    sid = "crm-disc"
    chat("Balcony floor, 25 m2, nothing slippery", sid)
    chat("Terra Rustic sounds good", sid)
    r = chat("Yes, quote it with a 20% discount", sid)
    assert r["quote"]["approval_required"] is True
    db.expire_all()
    q = db.scalar(select(Quote).where(Quote.number == r["quote"]["quotation_id"]))
    d = db.scalar(select(DiscountRequest).where(DiscountRequest.quote_id == q.id))
    assert q.status == "draft" and d.status == "pending" and d.requested_by is None and d.percent == 20


def test_pdf_button_quote_joins_the_chat_lead(db):
    sid = "crm-pdf"
    chat("Kitchen floor, 18 m2", sid)
    r = client.post("/api/v1/quotes", headers={"X-User-Id": "USR-5550001"},
                    json={"items": [{"sku": "CN005", "area_m2": 18}], "session_id": sid})
    assert r.status_code == 200
    db.expire_all()
    q = db.scalar(select(Quote).where(Quote.number == r.json()["quote"]["quotation_id"]))
    _, lead = lead_of(db, sid)
    assert q.lead_id == lead.id
    other = client.post("/api/v1/quotes", headers={"X-User-Id": "USR-9999999"},
                        json={"items": [{"sku": "CN005", "area_m2": 18}], "session_id": sid})
    assert other.status_code == 403                                   # someone else's chat


def test_call_back_request_adds_contact_details(db):
    sid = "crm-call"
    chat("Outdoor terrace floor, 40 m2", sid)
    r = client.post("/api/v1/chat/callback", json={"user_id": "USR-5550001", "session_id": sid,
                                                   "name": "Mariam Saleh", "phone": "+974 6600 1122"})
    assert r.status_code == 200 and r.json()["salesperson"]
    _, lead = lead_of(db, sid)
    c = db.get(Customer, lead.customer_id)
    assert (c.name, c.phone) == ("Mariam Saleh", "+974 6600 1122")
    assert db.scalar(select(Activity).where(Activity.lead_id == lead.id, Activity.type == "call"))
    bad = client.post("/api/v1/chat/callback", json={"user_id": "USR-5550009", "session_id": sid,
                                                     "name": "X Y", "phone": "+974 6600 1122"})
    assert bad.status_code == 404                                     # not this visitor's chat
    assert client.post("/api/v1/chat/callback", json={"user_id": "USR-5550001", "session_id": sid,
                                                      "name": "Mariam", "phone": "abc"}).status_code == 422


def test_shop_checkout_continues_the_chat_lead(db):
    sid = "crm-journey"
    chat("Bathroom wall, 30 m2", sid)
    _, lead = lead_of(db, sid)
    leads_before = db.scalar(select(func.count()).select_from(Lead))
    r = client.post("/api/shop/checkout", json={
        "type": "order", "items": [{"sku": "CN010", "area_m2": 30}], "name": "Khalid Nasser", "phone": "+974 7711 2233",
        "delivery_area": "Doha", "chat_session_id": sid, "chat_visitor_id": "USR-5550001"})
    assert r.status_code == 200 and r.json()["lead_from_chat"] is True
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Lead)) == leads_before    # same lead, no duplicate
    lead = db.get(Lead, lead.id)
    assert lead.stage == "quote_sent" and "Then: Online order" in lead.summary
    assert db.get(Customer, lead.customer_id).phone == "+974 7711 2233"
    # a different visitor can't attach an order to someone else's chat
    r2 = client.post("/api/shop/checkout", json={
        "type": "quote", "items": [{"sku": "CN010", "area_m2": 5}], "name": "Someone Else", "phone": "+974 7000 0000",
        "delivery_area": "Doha", "chat_session_id": sid, "chat_visitor_id": "USR-1234567"})
    assert r2.json()["lead_from_chat"] is False


def test_crm_problems_never_break_the_chat(monkeypatch):
    def broken():
        raise RuntimeError("database down")
    monkeypatch.setattr(crm_bridge, "SessionLocal", broken)
    r = chat("Bathroom floor, 10 m2", "crm-broken")
    assert r["reply"] and crm_bridge.record_chat_turn(None) is None
