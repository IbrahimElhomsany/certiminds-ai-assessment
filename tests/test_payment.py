"""Shop checkout end to end: "complete your project" suggestions, the Qatar address and delivery day, card payment
through Tap (mocked: no network) and the receipt / quotation the customer can download.

The rule that matters most: an order is only marked paid when Tap itself says the charge was CAPTURED for the
right amount, in QAR, for that order. Nothing the browser sends can mark it paid."""
import itertools
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend import admin_api, main, payments, shop_api
from backend.db.database import Base, SessionLocal, engine, init_db, local_now
from backend.db.models import Activity, Order, ShopCheckout
from backend.db.seed import DEMO_PASSWORD, seed_tenant

client = TestClient(main.app)
BUYER = {"name": "Ayesha Rahman", "phone": "+974 5555 1234", "email": "ayesha@example.com", "delivery_area": "Lusail"}
TILES = [{"sku": "CN001", "area_m2": 24}]


def address(**over) -> dict:
    return {"zone": "66", "street": "820", "building": "12", "unit": "Flat 4",
            "delivery_date": shop_api.delivery_days(local_now().date())[0].isoformat(), "payment": "on_delivery", **over}


@pytest.fixture(scope="module", autouse=True)
def demo_db():
    Base.metadata.drop_all(bind=engine)
    init_db()
    with SessionLocal() as db:
        seed_tenant(db, today=date(2026, 11, 16))
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def fresh_limits(monkeypatch):
    shop_api.submit_limiter._hits.clear()
    shop_api.price_limiter._hits.clear()
    admin_api.login_limiter._hits.clear()
    monkeypatch.delenv("TAP_SECRET_KEY", raising=False)


_ids = itertools.count(1)            # Tap's charge ids are unique across all orders


class FakeTap:
    """Stands in for Tap's API: remembers the charges we created and answers with whatever status we set."""
    def __init__(self):
        self.charges, self.status, self.amount, self.currency = {}, "INITIATED", None, "QAR"

    def create(self, amount, reference, description, name, phone, email, return_url, webhook_url=None):
        cid = f"chg_TEST{next(_ids):04d}"
        self.charges[cid] = {"amount": amount, "reference": reference, "return_url": return_url}
        return {"id": cid, "url": f"https://checkout.example/{cid}"}

    def fetch(self, cid):
        c = self.charges[cid]
        return {"id": cid, "status": self.status, "amount": self.amount if self.amount is not None else c["amount"],
                "currency": self.currency, "reference": c["reference"], "card": "VISA •••• 1111"}


@pytest.fixture
def tap(monkeypatch):
    monkeypatch.setenv("TAP_SECRET_KEY", "sk_test_dummy_for_tests")
    fake = FakeTap()
    monkeypatch.setattr(payments, "create_charge", fake.create)
    monkeypatch.setattr(payments, "fetch_charge", fake.fetch)
    return fake


def order(items=None, **over):
    return client.post("/api/shop/checkout", json={"type": "order", "items": items or TILES, **BUYER, **address(**over)})


# ------------------------------------------------------------------ options and validation
def test_options_without_a_key_offer_only_pay_on_delivery():
    o = client.get("/api/shop/options").json()
    assert o["card_payment"] is False and o["card_test_mode"] is False
    days = [date.fromisoformat(d) for d in o["delivery_dates"]]
    today = local_now().date()
    assert days[0] >= today + timedelta(days=2) and all(d.weekday() != 4 for d in days)     # never on a Friday


@pytest.mark.parametrize("over", [{"zone": None}, {"zone": "1234"}, {"street": None}, {"building": None},
                                  {"payment": None}, {"delivery_date": None}, {"delivery_date": "2020-01-01"}])
def test_an_order_needs_a_full_address_a_day_and_a_payment_choice(over):
    assert order(**over).status_code == 422


def test_a_friday_is_refused():
    friday = next(local_now().date() + timedelta(days=i) for i in range(2, 9) if (local_now().date() + timedelta(days=i)).weekday() == 4)
    assert order(delivery_date=friday.isoformat()).status_code == 422


def test_card_is_refused_when_not_set_up_and_nothing_is_saved():
    with SessionLocal() as db:
        before = db.scalar(select(Order.id).order_by(Order.id.desc()).limit(1))
    r = order(payment="card")
    assert r.status_code == 422 and "pay on delivery" in r.json()["detail"]
    with SessionLocal() as db:
        assert db.scalar(select(Order.id).order_by(Order.id.desc()).limit(1)) == before


# ------------------------------------------------------------------ complete your project
def test_suggests_grout_adhesive_and_spacers_for_the_tiles():
    s = client.post("/api/shop/cart/suggest", json={"items": TILES}).json()["suggestions"]
    groups = {x["reason"]: x for x in s}
    assert {"grout", "adhesive", "spacers"} <= set(groups)
    g = groups["grout"]
    assert g["sku"].startswith("GR") and g["suggest_quantity"] >= 1 and "kg" in g["why"] and len(g["alternatives"]) == 3
    assert groups["spacers"]["suggest_quantity"] == 3                      # 24 m2: one bag per 10 m2, rounded up
    assert all(x["sku"] != "CN001" for x in s)                             # nothing already in the cart
    # grout already chosen: not suggested again
    s2 = client.post("/api/shop/cart/suggest", json={"items": TILES + [{"sku": "GR02", "quantity": 1}]}).json()["suggestions"]
    assert not any(x["reason"] == "grout" for x in s2)


def test_accessories_stay_out_of_the_main_product_list_but_can_be_bought():
    assert not any(p["kind"] == "acc" for p in client.get("/api/shop/products").json()["products"])
    assert len(client.get("/api/shop/products?kind=acc").json()["products"]) == 6
    cart = client.post("/api/shop/cart/price", json={"items": TILES + [{"sku": "AD01", "quantity": 2}]}).json()
    assert cart["lines"][1]["unit"] == "bag" and cart["lines"][1]["line_total"] == 96.0


# ------------------------------------------------------------------ pay on delivery and the receipt
def test_order_on_delivery_gives_a_receipt_with_address_and_totals():
    r = order(items=TILES + [{"sku": "GR01", "quantity": 2}])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token"] and "payment_url" not in body
    d = client.get(f"/api/shop/receipt/{body['token']}").json()
    assert d["kind"] == "order" and d["number"] == body["order_number"]
    assert d["address"] == {"area": "Lusail", "zone": "66", "street": "820", "building": "12", "unit": "Flat 4"}
    assert d["payment"]["method"] == "on_delivery" and d["payment"]["status"] == "unpaid"
    assert d["total"] == pytest.approx(d["subtotal"] + d["delivery_fee"])
    assert [l["sku"] for l in d["lines"]] == ["CN001", "GR01"]
    pdf = client.get(f"/api/shop/receipt/{body['token']}/pdf")
    assert pdf.status_code == 200 and pdf.content[:5] == b"%PDF-" and pdf.headers["content-type"] == "application/pdf"
    assert body["order_number"] in pdf.headers["content-disposition"]
    with SessionLocal() as db:                                              # the salesperson sees where and when
        o = db.scalar(select(Order).where(Order.number == body["order_number"]))
        info = admin_api.shop_info(db, o)
    assert info["address"].startswith("Zone 66, Street 820, Building 12") and info["payment_method"] == "on_delivery"


def test_quotation_gets_a_downloadable_pdf_too():
    r = client.post("/api/shop/checkout", json={"type": "quote", "items": TILES, **BUYER}).json()
    d = client.get(f"/api/shop/receipt/{r['token']}").json()
    assert d["kind"] == "quote" and d["number"] == r["quote_number"] and d["payment"]["can_pay_card"] is False
    assert client.get(f"/api/shop/receipt/{r['token']}/pdf").content[:5] == b"%PDF-"


def test_unknown_or_guessed_receipt_tokens_are_not_found():
    for t in ("nope", "x" * 32, "../../etc/passwd"):
        assert client.get(f"/api/shop/receipt/{t}").status_code == 404


# ------------------------------------------------------------------ card payment through Tap (mocked)
def test_card_payment_is_only_marked_paid_when_tap_confirms_the_right_amount(tap):
    assert client.get("/api/shop/options").json()["card_payment"] is True
    r = order(payment="card").json()
    assert r["payment_url"].startswith("https://checkout.example/chg_TEST")
    token = r["token"]
    # back from Tap, but the charge is not captured yet
    cid = r["payment_url"].rsplit("/", 1)[1]
    back = client.get(f"/api/shop/pay/return/{token}?tap_id={cid}", follow_redirects=False)
    assert back.status_code == 303 and back.headers["location"] == f"/#/receipt/{token}"
    assert client.get(f"/api/shop/receipt/{token}").json()["payment"]["status"] == "pending"
    # captured, but for a smaller amount: not paid
    tap.status, tap.amount = "CAPTURED", Decimal("1.00")
    assert client.get(f"/api/shop/receipt/{token}").json()["payment"]["status"] == "pending"
    # captured in another currency: not paid
    tap.amount, tap.currency = None, "USD"
    assert client.get(f"/api/shop/receipt/{token}").json()["payment"]["status"] == "pending"
    # captured, right amount, QAR: paid
    tap.currency = "QAR"
    d = client.get(f"/api/shop/receipt/{token}").json()
    assert d["payment"]["status"] == "paid" and d["payment"]["card"] == "VISA •••• 1111" and d["payment"]["test_mode"]
    with SessionLocal() as db:
        chk = db.scalar(select(ShopCheckout).where(ShopCheckout.token == token))
        acts = db.scalars(select(Activity).where(Activity.text.like(f"Paid online by card%{r['order_number']}%"))).all()
    assert chk.paid_at and len(acts) == 1
    # paid orders can't be paid again or switched to cash
    assert client.post(f"/api/shop/receipt/{token}/pay").status_code == 409
    assert client.post(f"/api/shop/receipt/{token}/on-delivery").status_code == 409
    pdf = client.get(f"/api/shop/receipt/{token}/pdf")
    assert pdf.status_code == 200 and pdf.content[:5] == b"%PDF-"


def test_declined_card_can_retry_or_switch_to_pay_on_delivery(tap):
    token = order(payment="card").json()["token"]
    tap.status = "DECLINED"
    d = client.get(f"/api/shop/receipt/{token}").json()
    assert d["payment"]["status"] == "failed" and d["payment"]["can_pay_card"]
    first = set(tap.charges)
    again = client.post(f"/api/shop/receipt/{token}/pay").json()
    assert again["payment_url"].rsplit("/", 1)[1] in set(tap.charges) - first          # a new charge, not the old one
    d = client.post(f"/api/shop/receipt/{token}/on-delivery").json()
    assert d["payment"]["method"] == "on_delivery" and d["payment"]["status"] == "unpaid"


def test_webhook_trusts_only_tap_not_the_body(tap):
    token = order(payment="card").json()["token"]
    with SessionLocal() as db:
        cid = db.scalar(select(ShopCheckout.provider_ref).where(ShopCheckout.token == token))
    # a forged notice that claims "CAPTURED" changes nothing while Tap itself says it is not paid
    assert client.post("/api/shop/pay/tap/webhook", json={"id": cid, "status": "CAPTURED"}).json() == {"ok": True}
    assert client.get(f"/api/shop/receipt/{token}").json()["payment"]["status"] == "pending"
    tap.status = "CAPTURED"
    client.post("/api/shop/pay/tap/webhook", json={"id": cid})
    with SessionLocal() as db:
        assert db.scalar(select(ShopCheckout.payment_status).where(ShopCheckout.token == token)) == "paid"
    assert client.post("/api/shop/pay/tap/webhook", content=b"not json").json() == {"ok": False}


def test_payment_provider_down_keeps_the_order(monkeypatch):
    monkeypatch.setenv("TAP_SECRET_KEY", "sk_test_dummy_for_tests")

    def down(*a, **k):
        raise payments.PaymentError("Could not reach the payment provider (ConnectError).")
    monkeypatch.setattr(payments, "create_charge", down)
    r = order(payment="card").json()
    assert r["order_number"] and "payment_error" in r
    d = client.get(f"/api/shop/receipt/{r['token']}").json()
    assert d["payment"]["status"] == "failed" and d["payment"]["can_pay_card"]


def test_admin_order_shows_delivery_and_payment():
    num = order().json()["order_number"]
    tok = client.post("/api/admin/login", json={"company": "ceramica-nova", "email": "owner@ceramicanova.demo",
                                                 "password": DEMO_PASSWORD}).json()["token"]
    with SessionLocal() as db:
        oid = db.scalar(select(Order.id).where(Order.number == num))
    o = client.get(f"/api/admin/orders/{oid}", headers={"Authorization": f"Bearer {tok}"}).json()
    shop = o.get("shop") or o.get("order", {}).get("shop")
    assert shop and shop["payment_method"] == "on_delivery" and "Zone 66" in shop["address"]
