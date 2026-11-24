"""The home page: film at the top and "shop the look" scenes, managed by the owner (backend/homepage.py).
OpenAI is replaced by a fake, so these tests cost nothing."""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from backend import admin_api, homepage, main, roomedit
from backend.db.database import Base, SessionLocal, engine, init_db
from backend.db.seed import DEMO_PASSWORD, seed_tenant
from tests.test_makeover import FakeOpenAI

client = TestClient(main.app)


@pytest.fixture(scope="module", autouse=True)
def demo_db():
    Base.metadata.drop_all(bind=engine)
    init_db()
    with SessionLocal() as db:
        seed_tenant(db, today=date(2026, 11, 10))
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def setup(tmp_path, monkeypatch):
    admin_api.login_limiter._hits.clear()
    roomedit._day.update(date=None, count=0)
    monkeypatch.setenv("HOMEPAGE_DIR", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    fake = FakeOpenAI()
    monkeypatch.setattr(roomedit, "_post", fake)
    return fake


def H(who="owner"):
    r = client.post("/api/admin/login", json={"company": "ceramica-nova", "email": f"{who}@ceramicanova.demo", "password": DEMO_PASSWORD})
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_shop_gets_the_built_in_scenes_with_product_names():
    d = client.get("/api/shop/home").json()
    assert d["film"] is None and len(d["scenes"]) == 6
    first = d["scenes"][0]
    assert first["spots"][0] == {"sku": "CN006", "surface": "floor", "x": 52, "y": 84, "name": "Royal Calacatta"}
    for s in d["scenes"]:
        assert (homepage.visual.ROOT / s["image"].lstrip("/")).exists()


def test_owner_makes_an_ai_scene_and_it_comes_first(setup):
    r = client.post("/api/admin/homepage/scenes", json={"room": "kitchen", "floor": "CN001", "wall": "CN010"}, headers=H())
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["made"] == "ai" and s["title"] == "Kitchen" and [p["sku"] for p in s["spots"]] == ["CN001", "CN010"]
    assert "professional interior photograph" in setup.calls[0]["data"]["prompt"]
    assert len(setup.calls[0]["files"]) == 3                      # room + floor tile + wall tile
    home = client.get("/api/shop/home").json()
    assert home["scenes"][0]["id"] == s["id"] and len(home["scenes"]) == 7
    assert client.get(s["image"]).status_code == 200


def test_wrong_choices_are_named_by_field():
    h = H()
    bad = lambda body: client.post("/api/admin/homepage/scenes", json=body, headers=h).json()["detail"]
    assert bad({"room": "garage", "floor": "CN001"})["field"] == "room"
    assert bad({"room": "terrace", "floor": "CN001"})["field"] == "floor"      # an indoor tile on the terrace
    assert bad({"room": "kitchen", "wall": "CN001"})["field"] == "wall"         # a floor-only tile on the wall
    assert bad({"room": "kitchen"})["field"] == "floor"


def test_move_dots_rename_reorder_and_delete():
    h = H()
    r = client.patch("/api/admin/homepage/scenes/kitchen", json={"title": "Family kitchen", "move": -1,
                     "spots": [{"sku": "CN003", "surface": "floor", "x": 41.26, "y": 90}]}, headers=h).json()
    assert [s["id"] for s in r["scenes"]][:2] == ["kitchen", "living"]
    assert r["scenes"][0]["title"] == "Family kitchen" and r["scenes"][0]["spots"][0]["x"] == 41.3
    assert client.patch("/api/admin/homepage/scenes/kitchen", json={"spots": [{"sku": "XX9", "surface": "floor", "x": 1, "y": 1}]},
                        headers=h).status_code == 422
    assert client.delete("/api/admin/homepage/scenes/terrace", headers=h).json()["scenes"][-1]["id"] == "bathroom"
    assert client.post("/api/admin/homepage/reset", headers=h).json()["scenes"][0]["id"] == "living"


def test_film_upload_checks_the_file():
    h = H()
    mp4 = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 1000
    up = lambda data, kind="video/mp4": client.post("/api/admin/homepage/film", content=data, headers=h | {"Content-Type": kind})
    assert up(b"hello").json()["detail"]["field"] == "film"
    assert up(mp4, "image/gif").json()["detail"]["field"] == "film"
    film = up(mp4).json()["film"]
    assert film.startswith("/api/shop/home/files/film_") and client.get(film).status_code == 200
    assert client.get("/api/shop/home").json()["film"] == film
    assert client.delete("/api/admin/homepage/film", headers=h).json()["film"] is None


def test_only_the_owner_manages_the_home_page():
    assert client.get("/api/admin/homepage", headers=H("manager")).status_code == 403
    d = client.get("/api/admin/homepage", headers=H()).json()
    assert d["ai_on"] is True and len(d["rooms"]) == 9 and all((homepage.visual.ROOT / r["photo"].lstrip("/")).exists() for r in d["rooms"])
