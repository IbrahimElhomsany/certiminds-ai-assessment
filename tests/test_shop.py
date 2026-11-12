"""Nova Studio step 2: the public shop API - browsing, server-side pricing, quotation requests and orders."""
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend import admin_api, main, shop_api
from backend.db.database import Base, SessionLocal, engine, init_db
from backend.db.models import Activity, AuditLog, Batch, Customer, Lead, Order, Product, Quote, Tenant, User
from backend.db.seed import DEMO_PASSWORD, seed_tenant
from backend.tools import calculate_tile_quantity

client = TestClient(main.app)
BUYER = {"name": "Ayesha Rahman", "phone": "+974 5555 1234", "email": "ayesha@example.com",
         "delivery_area": "Lusail", "room": "bathroom", "notes": "Weekend delivery please"}


@pytest.fixture(scope="module", autouse=True)
def demo_db():
    Base.metadata.drop_all(bind=engine)
    init_db()
    with SessionLocal() as db:
        seed_tenant(db, today=date(2026, 11, 10))
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def fresh_limits():
    shop_api.submit_limiter._hits.clear()
    shop_api.price_limiter._hits.clear()
    admin_api.login_limiter._hits.clear()


@pytest.fixture
def db():
    with SessionLocal() as s:
        yield s


def checkout(type_="quote", items=None, **over):
    return client.post("/api/shop/checkout", json={"type": type_, "items": items or [{"sku": "CN001", "area_m2": 24}],
                                                   **BUYER, **over})


# ------------------------------------------------------------------ pages
def test_shop_is_the_home_page_and_assistant_moved():
    assert "Ceramica Nova" in client.get("/").text and "/api/shop/products" in client.get("/").text
    assert "/assets/assistant.js" in client.get("/assistant").text
    assert "/api/v1/chat" in client.get("/assets/assistant.js").text


# ------------------------------------------------------------------ browsing
def test_products_hide_cost_and_exact_stock():
    body = client.get("/api/shop/products").json()
    assert body["currency"] == "QAR" and len(body["products"]) == 18
    text = str(body)
    assert "cost" not in text and "qty_left" not in text and "stock_m2" not in text
    assert {p["availability"] for p in body["products"]} <= {"In stock", "Low stock", "Out of stock"}


def test_filters():
    outdoor = client.get("/api/shop/products?location=outdoor").json()["products"]
    assert {p["sku"] for p in outdoor} == {"CN004", "CN009", "CN014"}
    sets = client.get("/api/shop/products?kind=set").json()["products"]
    assert {p["sku"] for p in sets} == {"SN001", "SN002", "SN003"}
    walls = client.get("/api/shop/products?kind=tile&surface=wall").json()["products"]
    assert walls and all("wall" in p["surfaces"] for p in walls)
    assert {p["sku"] for p in client.get("/api/shop/products?q=marble").json()["products"]} >= {"CN001"}
    assert client.get("/api/shop/products?location=space").status_code == 422


def test_best_sellers_come_from_real_orders(db):
    prods = client.get("/api/shop/products?sort=popular").json()["products"]
    top = prods[0]
    assert top["best_seller_rank"] == 1
    ranks = shop_api.best_seller_ranks(db, db.scalar(select(Tenant.id)))
    assert db.get(Product, min(ranks, key=ranks.get)).sku == top["sku"]
    prices = [p["price"] for p in client.get("/api/shop/products?sort=price_asc").json()["products"]]
    assert prices == sorted(prices)


def test_product_page():
    p = client.get("/api/shop/products/cn004").json()
    assert p["sku"] == "CN004" and p["slip_rating"] and p["box_m2"] > 0
    assert 0 < len(p["related"]) <= 4 and "CN004" not in {r["sku"] for r in p["related"]}
    assert "outdoor" in p["related"][0]["locations"]          # closest matches first
    assert client.get("/api/shop/products/NOPE").status_code == 404


# ------------------------------------------------------------------ pricing
def test_cart_price_matches_the_assistants_calculation():
    """The shop and Ceramica Nova AI must give the same boxes and price for the same area."""
    r = client.post("/api/shop/cart/price", json={"items": [{"sku": "CN001", "area_m2": 24, "price": 1}]}).json()
    line, ref = r["lines"][0], calculate_tile_quantity("CN001", 24, "simple")
    assert line["boxes"] == ref.box_count and line["quantity_m2"] == ref.total_purchasable_area_m2
    assert line["line_total"] == ref.material_subtotal and line["unit_price"] != 1     # browser can't set a price
    diag = client.post("/api/shop/cart/price", json={"items": [{"sku": "CN001", "area_m2": 24, "layout": "diagonal"}]}).json()
    assert diag["lines"][0]["boxes"] >= line["boxes"]


def test_cart_with_a_set_and_offers():
    r = client.post("/api/shop/cart/price", json={"items": [{"sku": "SN001", "quantity": 2}]}).json()
    assert r["subtotal"] == 2500.0 and r["offers"]["free_delivery"]
    small = client.post("/api/shop/cart/price", json={"items": [{"sku": "CN010", "area_m2": 1}]}).json()
    assert small["offers"]["delivery_fee_qar"] == 50.0


@pytest.mark.parametrize("item", [{"sku": "CN001", "area_m2": 5, "quantity": 1}, {"sku": "CN001"},
                                  {"sku": "CN001", "quantity": 3}, {"sku": "SN001", "area_m2": 4},
                                  {"sku": "XX999", "area_m2": 4}, {"sku": "CN001", "area_m2": -2}])
def test_bad_cart_items_are_rejected(item):
    assert client.post("/api/shop/cart/price", json={"items": [item]}).status_code == 422


def test_one_batch_check(db):
    """Feature 8: the cart says when no single production batch can cover the quantity."""
    big = client.post("/api/shop/cart/price", json={"items": [{"sku": "CN006", "area_m2": 1000}]}).json()["lines"][0]
    small = client.post("/api/shop/cart/price", json={"items": [{"sku": "CN006", "area_m2": 10}]}).json()["lines"][0]
    assert big["one_batch"] is False and small["one_batch"] is True


# ------------------------------------------------------------------ checkout
def test_quote_request_becomes_a_lead_in_the_crm(db):
    before = db.scalar(select(func.count()).select_from(Lead))
    r = checkout("quote", items=[{"sku": "CN001", "area_m2": 24}, {"sku": "SN002", "quantity": 1}])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reference"] == body["quote_number"] and body["order_number"] is None
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Lead)) == before + 1
    q = db.scalar(select(Quote).where(Quote.number == body["quote_number"]))
    lead = db.get(Lead, q.lead_id)
    assert q.status == "sent" and q.total == q.subtotal == sum(l.line_total for l in q.lines)
    assert float(q.total) == body["cart"]["subtotal"] and q.cost_total < q.total
    assert lead.source == "shop" and lead.stage == "quote_sent" and lead.room == "bathroom"
    assert db.get(User, lead.owner_id).role == "salesperson"
    assert "Online quotation request" in lead.summary and "Lusail" in lead.summary
    assert db.scalar(select(Activity).where(Activity.lead_id == lead.id))
    assert db.scalar(select(AuditLog).where(AuditLog.action == "shop.quote", AuditLog.entity_id == lead.number))
    # ...and the sales manager sees it in Nova Studio
    tok = client.post("/api/admin/login", json={"email": "manager@ceramicanova.demo", "password": DEMO_PASSWORD}).json()["token"]
    leads = client.get("/api/admin/leads?stage=quote_sent&limit=200", headers={"Authorization": f"Bearer {tok}"}).json()
    assert lead.number in {l["number"] for l in leads}


def test_order_waits_for_staff_and_suggests_one_batch(db):
    stock_before = db.scalar(select(func.sum(Batch.qty_left)))
    r = checkout("order", items=[{"sku": "CN004", "area_m2": 30}])
    assert r.status_code == 200, r.text
    db.expire_all()
    o = db.scalar(select(Order).where(Order.number == r.json()["order_number"]))
    assert o.status == "pending" and o.created_by is None
    line = o.lines[0]
    batch = db.get(Batch, line.batch_id)
    assert batch.product_id == line.product_id and batch.qty_left >= line.quantity
    assert db.scalar(select(func.sum(Batch.qty_left))) == stock_before      # stock moves only when staff confirm
    assert db.get(Quote, o.quote_id).status == "accepted"


def test_returning_customer_is_matched_by_phone(db):
    a = checkout("quote").json()
    b = checkout("quote", phone="0097455551234", name="Ayesha R.").json()
    qa, qb = (db.scalar(select(Quote).where(Quote.number == n)) for n in (a["quote_number"], b["quote_number"]))
    assert qa.customer_id == qb.customer_id
    assert db.scalar(select(func.count()).select_from(Customer).where(Customer.phone.like("%5555%1234%"))) == 1


def test_new_leads_go_to_the_least_busy_salesperson(db):
    tid = db.scalar(select(Tenant.id))
    expected = shop_api.pick_salesperson(db, tid)
    r = checkout("quote", phone="+974 3333 7777").json()
    db.expire_all()
    lead = db.get(Lead, db.scalar(select(Quote.lead_id).where(Quote.number == r["quote_number"])))
    assert lead.owner_id == expected.id and r["assigned_to"] == expected.name.split()[0]


@pytest.mark.parametrize("over", [{"phone": "123"}, {"phone": "call me maybe"}, {"delivery_area": "Mars"},
                                  {"name": ""}, {"email": "not-an-email"}, {"room": "garage"}, {"type": "steal"}])
def test_checkout_validation(over):
    assert checkout(**{"type_": over.pop("type")} if "type" in over else {}, **over).status_code == 422


def test_checkout_is_rate_limited():
    codes = [checkout("quote").status_code for _ in range(6)]
    assert codes[:5] == [200] * 5 and codes[5] == 429


def test_document_numbers_continue_the_sequence(db):
    last = db.scalar(select(func.max(Quote.number)))
    r = checkout("quote").json()
    assert int(r["quote_number"][2:]) == int(last[2:]) + 1
