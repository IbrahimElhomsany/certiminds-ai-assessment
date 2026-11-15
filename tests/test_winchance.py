"""Win chance (D1): a small model learned from the company's own won and lost leads.

Checks that it learns something real (better than guessing on leads it never saw), only scores open leads,
says "not enough data" instead of guessing, retrains when a lead closes, and respects roles and companies."""
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend import admin_api, catalog, crm_bridge, main
from backend.crm import winchance
from backend.db.database import Base, SessionLocal, engine, init_db
from backend.db.models import Lead, Tenant
from backend.db.seed import DEMO_PASSWORD, seed_tenant

client = TestClient(main.app)


@pytest.fixture(scope="module", autouse=True)
def demo_db():
    Base.metadata.drop_all(bind=engine)
    init_db()
    with SessionLocal() as db:
        seed_tenant(db, today=date(2026, 11, 15))
    crm_bridge.refresh_catalog(force=True)
    winchance._cache.clear()
    yield
    Base.metadata.drop_all(bind=engine)
    catalog.replace_catalog(catalog.load_catalog(), catalog.load_sanitary())


@pytest.fixture(autouse=True)
def fresh_limits():
    admin_api.login_limiter._hits.clear()


def auth(email):
    r = client.post("/api/admin/login", json={"company": "ceramica-nova", "email": email, "password": DEMO_PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def tenant_id(db):
    return db.scalar(select(Tenant.id).where(Tenant.slug == "ceramica-nova"))


def test_learns_something_real():
    with SessionLocal() as db:
        c = winchance.card(db, tenant_id(db))
    assert c["ready"] and c["demo"] and c["closed"] >= winchance.MIN_CLOSED
    for m in (c["early"], c["quoted"]):
        chk = m["check"]
        assert chk["tested_on"] == m["trained_on"]                   # every closed lead was scored by a model that never saw it
        assert chk["auc"] > 0.55                                      # better than guessing (0.5)
        assert chk["likely_won"] > chk["base_rate"] > chk["unlikely_won"]
        assert m["top"]


def test_scores_only_open_leads_with_reasons():
    with SessionLocal() as db:
        leads = db.scalars(select(Lead).where(Lead.tenant_id == tenant_id(db))).all()
        s = winchance.score(db, tenant_id(db), leads)
    for l in leads:
        if l.stage in ("won", "lost"):
            assert s[l.id] is None
        else:
            assert 1 <= s[l.id]["pct"] <= 99 and s[l.id]["model"] in ("early", "quoted")
            assert all(set(r) == {"text", "up"} for r in s[l.id]["reasons"])
    quoted = [l for l in leads if l.stage in ("quote_sent", "negotiation")]
    assert quoted and all(s[l.id]["model"] == "quoted" for l in quoted)


def test_not_enough_data_says_so():
    with SessionLocal() as db:
        seed_tenant(db, slug="tiny-tiles", name="Tiny Tiles", today=date(2026, 11, 15))
        tid = db.scalar(select(Tenant.id).where(Tenant.slug == "tiny-tiles"))
        closed = db.scalars(select(Lead).where(Lead.tenant_id == tid, Lead.stage.in_(("won", "lost")))).all()
        for l in closed[10:]:
            l.stage = "qualified"                                     # leave only 10 closed leads
        db.commit()
        c = winchance.card(db, tid)
        open_ = db.scalars(select(Lead).where(Lead.tenant_id == tid, Lead.stage == "qualified")).all()
        assert not c["ready"] and c["closed"] == 10
        assert all(v is None for v in winchance.score(db, tid, open_).values())


def test_retrains_when_a_lead_closes():
    with SessionLocal() as db:
        tid = tenant_id(db)
        before = winchance.card(db, tid)
        lead = db.scalars(select(Lead).where(Lead.tenant_id == tid, Lead.stage == "negotiation")).first()
        lead.stage = "won"
        db.commit()
        after = winchance.card(db, tid)
    assert after["closed"] == before["closed"] + 1 and after["won"] == before["won"] + 1


def test_api_sort_card_and_roles():
    owner = auth("owner@ceramicanova.demo")
    r = client.get("/api/admin/leads/search?sort=likely&limit=100", headers=owner).json()
    pcts = [i["win"]["pct"] for i in r["items"]]
    assert r["items"] and all(i["stage"] not in ("won", "lost") for i in r["items"])
    assert pcts == sorted(pcts, reverse=True)
    lid = r["items"][0]["id"]
    d = client.get(f"/api/admin/leads/{lid}", headers=owner).json()
    assert d["win"]["pct"] == pcts[0]
    assert client.get("/api/admin/winchance", headers=owner).json()["ready"]
    assert client.get("/api/admin/winchance").status_code == 401
    assert client.get("/api/admin/winchance", headers=auth("warehouse@ceramicanova.demo")).status_code == 403
    mine = client.get("/api/admin/leads/search?sort=likely&limit=100", headers=auth("omar@ceramicanova.demo")).json()["items"]
    assert mine and all(i["owner"] == "Omar Khalil" for i in mine)    # a salesperson still sees only their own leads


def test_a_quiet_lead_goes_cold():
    with SessionLocal() as db:
        leads = db.scalars(select(Lead).where(Lead.tenant_id == tenant_id(db), Lead.stage == "qualified")).all()
        s = winchance.score(db, tenant_id(db), leads)
    cold = [s[l.id] for l in leads if s[l.id]["reasons"] and s[l.id]["reasons"][0]["text"].startswith("no contact for")]
    assert cold and all(not w["reasons"][0]["up"] for w in cold)
