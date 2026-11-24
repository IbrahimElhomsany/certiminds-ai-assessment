"""The home page: film at the top and "shop the showroom" displays, managed by the owner (backend/homepage.py).
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


def test_shop_gets_the_built_in_showroom_with_product_names():
    d = client.get("/api/shop/home").json()
    assert d["film"] is None and len(d["scenes"]) == 8 and d["scenes"][0]["id"] == "overview"
    skus = {p["sku"] for s in d["scenes"] for p in s["spots"]}
    assert {"SN001", "SN002", "SN003"} <= skus and len([k for k in skus if k.startswith("CN")]) >= 12   # tiles and bathroom sets
    for s in d["scenes"]:
        assert s["made"] == "render" and s["spots"] and all(p["name"] and 0 <= p["x"] <= 100 for p in s["spots"])
        assert (homepage.visual.ROOT / s["image"].lstrip("/")).exists()


def test_owner_makes_an_ai_scene_and_it_comes_first(setup):
    r = client.post("/api/admin/homepage/scenes", json={"room": "kitchen", "floor": "CN001", "wall": "CN010"}, headers=H())
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["made"] == "ai" and s["title"] == "Kitchen" and [p["sku"] for p in s["spots"]] == ["CN001", "CN010"]
    assert "professional interior photograph" in setup.calls[0]["data"]["prompt"]
    assert len(setup.calls[0]["files"]) == 3                      # room + floor tile + wall tile
    home = client.get("/api/shop/home").json()
    assert home["scenes"][0]["id"] == s["id"] and len(home["scenes"]) == 9
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
    r = client.patch("/api/admin/homepage/scenes/samples", json={"title": "Our sample wall", "move": -1,
                     "spots": [{"sku": "CN003", "surface": "sample", "x": 41.26, "y": 90},
                               {"sku": "SN002", "surface": "product", "x": 10, "y": 20}]}, headers=h).json()
    assert [s["id"] for s in r["scenes"]][:2] == ["samples", "overview"]
    first = r["scenes"][0]
    assert first["title"] == "Our sample wall" and first["spots"][0]["x"] == 41.3 and first["spots"][1]["name"] == "Aria Shower Set"
    assert client.patch("/api/admin/homepage/scenes/samples", json={"spots": [{"sku": "XX9", "surface": "floor", "x": 1, "y": 1}]},
                        headers=h).status_code == 422
    assert client.delete("/api/admin/homepage/scenes/outdoor", headers=h).json()["scenes"][-1]["id"] == "bathwall"
    assert client.post("/api/admin/homepage/reset", headers=h).json()["scenes"][0]["id"] == "overview"


def test_make_a_render_photo_real_and_go_back(setup):
    h = H()
    before = {s["id"]: s for s in client.get("/api/shop/home").json()["scenes"]}["gallery"]
    r = client.post("/api/admin/homepage/scenes/gallery/photoreal", json={"quality": "medium"}, headers=h)
    assert r.status_code == 200, r.text
    after = {s["id"]: s for s in r.json()["scenes"]}["gallery"]
    assert after["made"] == "ai-photo" and after["image"].startswith("/api/shop/home/files/gallery_real_")
    assert client.get(after["image"]).status_code == 200
    call = setup.calls[0]
    assert len(call["files"]) == 1 and call["data"]["size"] == "1536x1024" and "same spot, angle and framing" in call["data"]["prompt"]
    # the render is 16:9 and the AI picture 3:2, so the dots move with the crop (and ones cut off are dropped)
    assert all(2 <= p["x"] <= 98 for p in after["spots"]) and len(after["spots"]) <= len(before["spots"])
    assert client.post("/api/admin/homepage/scenes/gallery/photoreal", json={}, headers=h).status_code == 409
    back = {s["id"]: s for s in client.post("/api/admin/homepage/scenes/gallery/render", headers=h).json()["scenes"]}["gallery"]
    assert back["image"] == before["image"] and back["spots"] == before["spots"] and back["made"] == "render"
    assert client.post("/api/admin/homepage/scenes/gallery/render", headers=h).status_code == 409


def test_refit_spots_follows_the_crop():
    spots = [{"sku": "CN001", "surface": "sample", "x": 50, "y": 50}, {"sku": "CN002", "surface": "sample", "x": 3, "y": 50}]
    out = homepage.refit_spots(spots, 1920, 1080, "1536x1024")
    assert out == [{"sku": "CN001", "surface": "sample", "x": 50.0, "y": 50.0}]          # the one at the far left is cut off


def test_old_saved_home_page_keeps_the_owners_ai_pictures(setup, tmp_path):
    import json
    (tmp_path / "home.json").write_text(json.dumps({"film": None, "scenes": [
        {"id": "living", "title": "Living room", "image": "/images/home/scene_living.jpg", "made": "visualiser", "spots": []},
        {"id": "sabc12", "title": "My kitchen", "image": "/api/shop/home/files/sabc12.jpg", "made": "ai", "spots": []}]}))
    ids = [s["id"] for s in client.get("/api/shop/home").json()["scenes"]]
    assert ids[0] == "sabc12" and "living" not in ids and "overview" in ids


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
