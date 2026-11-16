"""D3: tile suggestions that learn from what customers really choose (quotations, orders, visualiser picks).

The learning only nudges the ranking: hard safety rules still come first, it needs enough projects before it says
anything, visualiser events are anonymous and can't be stuffed, and the numbers it shows are real counts."""
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend import admin_api, catalog, crm_bridge, main, recommender
from backend.crm import picks
from backend.db.database import Base, SessionLocal, engine, init_db
from backend.db.models import ChoiceEvent, Tenant
from backend.db.seed import DEMO_PASSWORD, seed_tenant
from backend.schemas import CustomerRequirements

client = TestClient(main.app)


@pytest.fixture(scope="module", autouse=True)
def demo_db():
    Base.metadata.drop_all(bind=engine)
    init_db()
    with SessionLocal() as db:
        seed_tenant(db, today=date(2026, 11, 16))
    picks._cache.clear()
    crm_bridge.refresh_catalog(force=True)
    yield
    Base.metadata.drop_all(bind=engine)
    recommender.LEARNED_BONUS = recommender.LEARNED_PAIRS = None
    picks._live.clear()
    catalog.replace_catalog(catalog.load_catalog(), catalog.load_sanitary())


@pytest.fixture(autouse=True)
def fresh_limits():
    admin_api.login_limiter._hits.clear()
    picks.limiter._hits.clear()


def tid(db):
    return db.scalar(select(Tenant.id).where(Tenant.slug == "ceramica-nova"))


def test_learns_room_favourites_and_pairs_from_quotations():
    with SessionLocal() as db:
        m = picks.learned(db, tid(db))
    bath = m["rooms"]["bathroom"]
    assert bath["quotes"] >= picks.MIN_PROJECTS
    top = max(bath["quoted"], key=bath["quoted"].get)
    assert top == "CN012"                                            # the demo history's bathroom favourite
    assert any(p["sku"] == "CN007" for p in m["pairs"]["CN012"])     # Sandstone Pro floor + Ivory Mist wall
    for lst in m["pairs"].values():
        assert all(p["together"] >= picks.PAIR_MIN and p["lift"] >= picks.PAIR_LIFT for p in lst)


def test_popular_tile_gets_a_small_bonus_with_a_real_count():
    req = CustomerRequirements(application="bathroom floor", floor_or_wall="floor", indoor_outdoor="indoor", area_m2=20)
    recs, _ = recommender.recommend(req)
    assert recs[0].sku == "CN012"
    reason = next(r for r in recs[0].reasons if r.startswith("popular for bathroom"))
    with SessionLocal() as db:
        b = picks.learned(db, tid(db))["rooms"]["bathroom"]
    assert f"in {b['quoted']['CN012']} of {b['quotes']} quotations" in reason
    assert "Ivory Mist" in recs[0].often_with
    bonus, _ = picks.live_bonus(req, "CN012")
    assert 0 < bonus <= picks.MAX_BONUS


def test_never_overrules_safety():
    """A wall-only or too slippery tile stays out, however popular it is."""
    req = CustomerRequirements(application="bathroom floor", floor_or_wall="floor", indoor_outdoor="indoor")
    old = recommender.LEARNED_BONUS
    recommender.LEARNED_BONUS = lambda r, sku: (1000.0, "very popular")
    try:
        recs, rejected = recommender.recommend(req, max_results=20)
    finally:
        recommender.LEARNED_BONUS = old
    allowed = {r.sku for r in recs}
    assert "CN007" not in allowed and any(r.sku == "CN007" for r in rejected)      # Ivory Mist is wall-only


def test_too_few_projects_says_nothing():
    m = {"rooms": {"bathroom": {"projects": 4, "chosen": {"CN012": 4}, "quotes": 4, "quoted": {"CN012": 4}}}}
    assert picks.popularity(m, "bathroom", "CN012") is None
    assert picks.popularity(m, "garage", "CN012") is None


def test_visualiser_choice_is_recorded_once_and_learned():
    body = {"sku": "CN003", "kind": "added", "room": "bedroom", "with_sku": None}
    for _ in range(3):
        assert client.post("/api/v1/choices", json=body, headers={"X-User-Id": "v-test-1"}).status_code == 204
    assert client.post("/api/v1/choices", json={**body, "sku": "NOPE1"}, headers={"X-User-Id": "v-test-1"}).status_code == 404
    assert client.post("/api/v1/choices", json={**body, "kind": "bought"}, headers={"X-User-Id": "v-test-1"}).status_code == 422
    with SessionLocal() as db:
        n = db.scalar(select(func.count(ChoiceEvent.id)).where(ChoiceEvent.visitor_id == "v-test-1"))
        m = picks.learned(db, tid(db))
    assert n == 1                                                     # one vote per visitor, tile and day
    assert m["rooms"]["bedroom"]["liked"]["CN003"] == 1 and m["events"] >= 1


def test_choice_endpoint_is_rate_limited():
    codes = [client.post("/api/v1/choices", json={"sku": "CN001", "kind": "tried", "room": "living"},
                         headers={"X-User-Id": "v-flood"}).status_code for _ in range(65)]
    assert codes[-1] == 429


def test_shop_and_admin_show_it():
    p = client.get("/api/shop/products/CN012").json()
    assert [o["sku"] for o in p["often_with"]][:1] == ["CN007"] and p["often_with"][0]["together"] >= picks.PAIR_MIN
    assert "CN007" not in [o["sku"] for o in p["related"]]           # not shown twice
    assert client.get("/api/v1/choices/pairs/CN012").json()["often_with"]
    r = client.post("/api/admin/login", json={"company": "ceramica-nova", "email": "warehouse@ceramicanova.demo", "password": DEMO_PASSWORD})
    card = client.get("/api/admin/choices", headers={"Authorization": f"Bearer {r.json()['token']}"}).json()
    assert card["demo"] and card["rooms"][0]["top"] and card["pairs"]
    assert client.get("/api/admin/choices").status_code == 401
