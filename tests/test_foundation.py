"""Nova Studio step 1: database, demo data, staff login, roles and company isolation."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from backend import admin_api, auth, main
from backend.catalog import CATALOG, SANITARY
from backend.db.database import Base, SessionLocal, engine, init_db, make_engine
from backend.db.models import (AuditLog, Batch, DiscountRequest, Lead, Order, OrderLine, Product, Quote, Tenant,
                               User)
from backend.db.seed import DEMO_PASSWORD, seed_tenant

TODAY = date(2026, 11, 10)
client = TestClient(main.app)


@pytest.fixture(scope="module", autouse=True)
def demo_db():
    Base.metadata.drop_all(bind=engine)
    init_db()
    with SessionLocal() as db:
        seed_tenant(db, today=TODAY)
        seed_tenant(db, slug="other-tiles", name="Other Tiles Co", today=TODAY, seed=7)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def fresh_login_limiter():
    admin_api.login_limiter._hits.clear()


@pytest.fixture
def db():
    with SessionLocal() as s:
        yield s


def login(email, password=DEMO_PASSWORD, company="ceramica-nova"):
    return client.post("/api/admin/login", json={"company": company, "email": email, "password": password})


def token_for(email, company="ceramica-nova"):
    r = login(email, company=company)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def nova(db) -> Tenant:
    return db.scalar(select(Tenant).where(Tenant.slug == "ceramica-nova"))


# ------------------------------------------------------------------ demo data
def test_seed_is_repeatable():
    """Same seed + same date = identical data, so a demo always shows the same numbers."""
    totals = []
    for _ in range(2):
        eng = make_engine("sqlite://")
        Base.metadata.create_all(eng)
        with sessionmaker(bind=eng)() as s:
            t = seed_tenant(s, today=TODAY)
            totals.append((s.scalar(select(func.count()).select_from(Lead).where(Lead.tenant_id == t.id)),
                           s.scalar(select(func.sum(Order.total)).where(Order.tenant_id == t.id))))
    assert totals[0] == totals[1]
    assert totals[0][0] > 250                      # ~90 days of leads


def test_demo_company_is_flagged_and_has_every_role(db):
    t = nova(db)
    assert t.is_demo and t.currency == "QAR"
    roles = {u.role for u in db.scalars(select(User).where(User.tenant_id == t.id))}
    assert roles == {"owner", "sales_manager", "salesperson", "warehouse", "viewer"}


def test_no_activity_after_today(db):
    end = datetime(2026, 11, 10, 18, 0)
    t = nova(db)
    assert db.scalar(select(func.max(Lead.created_at)).where(Lead.tenant_id == t.id)) <= end
    assert db.scalar(select(func.max(Order.created_at)).where(Order.tenant_id == t.id)) <= end


def test_stock_matches_catalog_and_never_negative(db):
    t = nova(db)
    for p in db.scalars(select(Product).where(Product.tenant_id == t.id)):
        expected = CATALOG[p.sku].stock_m2 if p.kind == "tile" else SANITARY[p.sku].stock_units
        assert sum(b.qty_left for b in p.batches) == Decimal(str(expected)), p.sku
        assert all(b.qty_left >= 0 and b.received_qty >= b.qty_left for b in p.batches)


def test_every_order_line_comes_from_one_batch_of_the_same_product(db):
    """Feature 8 groundwork: a line is supplied from ONE batch so shades match."""
    lines = db.execute(select(OrderLine, Batch).join(Batch, OrderLine.batch_id == Batch.id)).all()
    assert lines and len(lines) == db.scalar(select(func.count()).select_from(OrderLine))
    assert all(line.product_id == batch.product_id for line, batch in lines)


def test_money_adds_up(db):
    t = nova(db)
    for q in db.scalars(select(Quote).where(Quote.tenant_id == t.id)):
        assert q.subtotal == sum(l.line_total for l in q.lines)
        assert q.total == q.subtotal - q.discount_amount
        assert q.cost_total < q.total                    # demo prices always leave a profit
    for o in db.scalars(select(Order).where(Order.tenant_id == t.id)):
        assert abs(o.total - sum(l.line_total for l in o.lines)) <= Decimal("0.05")   # cent rounding per line


def test_tile_quantities_are_whole_boxes(db):
    t = nova(db)
    for q in db.scalars(select(Quote).where(Quote.tenant_id == t.id).limit(40)):
        for l in q.lines:
            p = db.get(Product, l.product_id)
            if p.kind == "tile":
                assert l.boxes and abs(float(l.quantity) - l.boxes * p.attributes["box_m2"]) < 0.02


def test_discounts_follow_the_margin_rule(db):
    """Feature 5 groundwork: nothing approved below the minimum margin; auto-approval only within the limit."""
    t = nova(db)
    reqs = db.scalars(select(DiscountRequest).where(DiscountRequest.tenant_id == t.id)).all()
    assert {r.status for r in reqs} >= {"auto_approved", "approved"}
    for r in reqs:
        if r.status in ("approved", "auto_approved"):
            assert r.margin_after_pct >= t.min_margin_pct
        if r.status == "auto_approved":
            assert r.percent <= db.get(User, r.requested_by).discount_limit_pct
        if r.status in ("approved", "rejected"):
            assert db.get(User, r.decided_by).role == "sales_manager"
            assert db.scalar(select(AuditLog).where(AuditLog.entity == "quote", AuditLog.tenant_id == t.id,
                                                    AuditLog.action.like("discount.%")))


# ------------------------------------------------------------------ login
def test_passwords_are_stored_hashed(db):
    for u in db.scalars(select(User)):
        assert u.password_hash != DEMO_PASSWORD and u.password_hash.startswith("$2")


def test_login_returns_a_token_and_is_audited(db):
    r = login("owner@ceramicanova.demo")
    assert r.status_code == 200
    assert r.json()["user"]["role"] == "owner" and "password" not in r.text
    me = client.get("/api/admin/me", headers={"Authorization": f"Bearer {r.json()['token']}"}).json()
    assert me["company"] == {"name": "Ceramica Nova", "slug": "ceramica-nova", "currency": "QAR", "is_demo": True}
    assert "users.manage" in me["permissions"]
    assert db.scalar(select(AuditLog).where(AuditLog.action == "login"))


def test_wrong_password_and_unknown_email_look_the_same(db):
    a = login("owner@ceramicanova.demo", password="wrong")
    b = login("nobody@ceramicanova.demo")
    assert a.status_code == b.status_code == 401 and a.json() == b.json()
    assert db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "login.failed")) >= 2


def test_login_is_rate_limited():
    codes = [login("omar@ceramicanova.demo", password="guess").status_code for _ in range(11)]
    assert codes[-1] == 429


def test_requests_without_a_valid_token_are_rejected(db):
    assert client.get("/api/admin/me").status_code == 401
    good = token_for("owner@ceramicanova.demo")["Authorization"].split()[1]
    tampered = jwt.encode({**jwt.decode(good, options={"verify_signature": False}), "role": "owner", "sub": "1"},
                          "attacker-secret-with-enough-length-1234567", algorithm="HS256")
    assert client.get("/api/admin/me", headers={"Authorization": f"Bearer {tampered}"}).status_code == 401
    owner = db.scalar(select(User).where(User.email == "owner@ceramicanova.demo"))
    expired = auth.create_token(owner, now=datetime.now(timezone.utc) - timedelta(hours=9))
    r = client.get("/api/admin/me", headers={"Authorization": f"Bearer {expired}"})
    assert r.status_code == 401 and "expired" in r.json()["detail"]


def test_role_change_takes_effect_immediately(db):
    headers = token_for("viewer@ceramicanova.demo")
    viewer = db.scalar(select(User).where(User.email == "viewer@ceramicanova.demo"))
    viewer.role = "warehouse"
    db.commit()
    try:
        assert client.get("/api/admin/me", headers=headers).status_code == 401   # old token no longer valid
    finally:
        viewer.role = "viewer"
        db.commit()


# ------------------------------------------------------------------ roles
@pytest.mark.parametrize("email,code", [("owner@ceramicanova.demo", 200), ("manager@ceramicanova.demo", 403),
                                        ("omar@ceramicanova.demo", 403), ("warehouse@ceramicanova.demo", 403),
                                        ("viewer@ceramicanova.demo", 403)])
def test_only_the_owner_manages_users(email, code):
    assert client.get("/api/admin/users", headers=token_for(email)).status_code == code


def test_salesperson_sees_only_own_leads_manager_sees_all(db):
    t = nova(db)
    omar = db.scalar(select(User).where(User.email == "omar@ceramicanova.demo"))
    mine = client.get("/api/admin/leads?limit=200", headers=token_for("omar@ceramicanova.demo")).json()
    assert mine and {l["owner"] for l in mine} == {omar.name}
    all_leads = client.get("/api/admin/leads?limit=200", headers=token_for("manager@ceramicanova.demo")).json()
    assert len({l["owner"] for l in all_leads}) == 3 and len(all_leads) > len(mine)
    assert client.get("/api/admin/leads", headers=token_for("warehouse@ceramicanova.demo")).status_code == 403
    assert client.get("/api/admin/leads?stage=bogus", headers=token_for("manager@ceramicanova.demo")).status_code == 422


def test_audit_log_needs_permission():
    assert client.get("/api/admin/audit", headers=token_for("omar@ceramicanova.demo")).status_code == 403
    rows = client.get("/api/admin/audit", headers=token_for("manager@ceramicanova.demo")).json()
    assert rows and "login" in {r["action"] for r in rows}


# ------------------------------------------------------------------ company isolation
def test_companies_never_see_each_other(db):
    other = db.scalar(select(Tenant).where(Tenant.slug == "other-tiles"))
    other_ids = {u.id for u in db.scalars(select(User).where(User.tenant_id == other.id))}
    users = client.get("/api/admin/users", headers=token_for("owner@ceramicanova.demo")).json()
    assert users and not ({u["id"] for u in users} & other_ids)
    other_leads = {l.number for l in db.scalars(select(Lead).where(Lead.tenant_id == other.id))}
    seen = client.get("/api/admin/leads?limit=200", headers=token_for("manager@ceramicanova.demo")).json()
    nova_ids = {l.id for l in db.scalars(select(Lead).where(Lead.tenant_id == nova(db).id))}
    assert other_leads and {l["id"] for l in seen} <= nova_ids
    # same email exists in both companies: logging in to one never opens the other
    r = client.get("/api/admin/me", headers=token_for("owner@ceramicanova.demo", company="other-tiles")).json()
    assert r["company"]["slug"] == "other-tiles"
    assert login("owner@ceramicanova.demo", company="no-such-company").status_code == 401
