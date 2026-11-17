"""Owner's own sample rooms (backend/sample_rooms.py) and shared fixes for training (backend/visual.py)."""
import io
import json
import time
from datetime import date

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backend import main, sample_rooms, visual
from backend.db.database import Base, SessionLocal, engine, init_db
from backend.db.seed import DEMO_PASSWORD, seed_tenant

client = TestClient(main.app)
ME = "USR-1111111111"
PNG = "data:image/png;base64,iVBORw0KGgo="
ANSWER = {"width": 1200, "height": 900, "scale": 1.0, "surfaces": [
    {"kind": "floor", "mask": PNG, "quad": [[0, 0], [1, 0], [1, 1], [0, 1]], "size": [100, 100]},
    {"kind": "wall", "mask": PNG, "quad": [[0, 0], [1, 0], [1, 1], [0, 1]], "size": [100, 100]}]}


@pytest.fixture(scope="module", autouse=True)
def demo_db():
    Base.metadata.drop_all(bind=engine)
    init_db()
    with SessionLocal() as db:
        seed_tenant(db, today=date(2026, 11, 10))
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(visual, "STORE", tmp_path / "uploads")
    monkeypatch.setenv("SAMPLE_ROOMS_DIR", str(tmp_path / "rooms"))
    monkeypatch.setenv("ROOMAI_TRAINING_DIR", str(tmp_path / "train"))
    monkeypatch.setenv("ROOMAI_URL", "https://roomai.example")
    monkeypatch.setenv("ROOMAI_TOKEN", "t")
    monkeypatch.setattr(sample_rooms, "_start", sample_rooms._analyse)      # run the analysis straight away
    calls = []

    def fake(path, image, params=None, timeout=180):
        calls.append(path)
        return ANSWER if path == "analyse" else {"mask": PNG, "share": 0.1}
    monkeypatch.setattr(visual, "ask_room_ai", fake)
    visual.limiter._hits.clear()
    main.rate_limiter._hits.clear()
    return calls


def token(email):
    r = client.post("/api/admin/login", json={"company": "ceramica-nova", "email": email, "password": DEMO_PASSWORD})
    return {"Authorization": f"Bearer {r.json()['token']}"}


def jpeg(w=1400, h=1000):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 190, 180)).save(buf, "JPEG")
    return buf.getvalue()


def test_owner_adds_checks_fixes_and_publishes_a_room():
    owner = token("owner@ceramicanova.demo")
    r = client.post("/api/admin/rooms?name=Majlis&location=indoor", content=jpeg(), headers=owner)
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    room = client.get(f"/api/admin/rooms/{rid}", headers=owner).json()
    assert room["status"] == "ready" and room["floors"] == 1 and room["walls"] == 1
    assert client.get("/api/rooms").json() == []                                # not published yet
    fix = {"edits": {"remove": PNG, "add": {"wall": PNG, "ceiling": PNG}}}
    assert client.patch(f"/api/admin/rooms/{rid}", json=fix, headers=owner).json()["fixed"]
    assert client.patch(f"/api/admin/rooms/{rid}", json={"published": True}, headers=owner).status_code == 200
    pub = client.get("/api/rooms").json()
    assert [p["name"] for p in pub] == ["Majlis"]
    assert set(pub[0]["surfaces"]) == {"floor", "wall"} and pub[0]["baseEdits"]["add"] == {"wall": PNG}
    assert client.get(pub[0]["thumb"]).status_code == 200
    assert client.post(f"/api/admin/rooms/{rid}/tap", json={"points": [[10, 10, 1]]}, headers=owner).json()["mask"] == PNG
    assert client.post(f"/api/admin/rooms/{rid}/tap", json={"points": [[9999, 10, 1]]}, headers=owner).status_code == 422
    assert client.delete(f"/api/admin/rooms/{rid}", headers=owner).status_code == 200
    assert client.get("/api/rooms").json() == []


def test_only_the_owner_manages_sample_rooms():
    manager = token("manager@ceramicanova.demo")
    assert client.get("/api/admin/rooms", headers=manager).status_code == 403
    assert client.post("/api/admin/rooms?name=X1", content=jpeg(), headers=manager).status_code == 403


def test_a_room_the_ai_could_not_read_can_be_tried_again(monkeypatch):
    owner = token("owner@ceramicanova.demo")
    monkeypatch.setattr(visual, "ask_room_ai", lambda *a, **k: (_ for _ in ()).throw(visual.HTTPException(502, "The room AI did not answer in time.")))
    rid = client.post("/api/admin/rooms?name=Office", content=jpeg(), headers=owner).json()["id"]
    room = client.get(f"/api/admin/rooms/{rid}", headers=owner).json()
    assert room["status"] == "failed" and "did not answer" in room["error"]
    assert client.patch(f"/api/admin/rooms/{rid}", json={"published": True}, headers=owner).status_code == 409
    monkeypatch.setattr(visual, "ask_room_ai", lambda *a, **k: ANSWER)
    assert client.post(f"/api/admin/rooms/{rid}/analyse", headers=owner).status_code == 200
    assert client.get(f"/api/admin/rooms/{rid}", headers=owner).json()["status"] == "ready"


def test_customer_fix_is_kept_for_training_only_with_consent(tmp_path):
    fid = client.post("/api/v1/visual/photos", content=jpeg(), headers={"X-User-Id": ME, "Content-Type": "image/jpeg"}).json()["id"]
    url = f"/api/v1/visual/photos/{fid}/share-fix"
    assert client.post(url, json={"consent": False, "remove": PNG}, headers={"X-User-Id": ME}).status_code == 422
    assert client.post(url, json={"consent": True, "remove": PNG}, headers={"X-User-Id": "USR-2222222222"}).status_code == 403
    assert client.post(url, json={"consent": True, "remove": "javascript:x"}, headers={"X-User-Id": ME}).status_code == 422
    assert client.post(url, json={"consent": True, "remove": PNG, "add": {"wall": PNG}}, headers={"X-User-Id": ME}).status_code == 200
    saved = tmp_path / "train" / fid
    assert (saved / "photo.jpg").exists() and json.loads((saved / "fix.json").read_text())["add"] == {"wall": PNG}
    assert visual.training_count() == 1
    owner = token("owner@ceramicanova.demo")
    assert client.get("/api/admin/rooms", headers=owner).json()["training_fixes"] == 1
    z = client.get("/api/admin/roomai/training.zip", headers=owner)
    assert z.status_code == 200 and z.headers["content-type"] == "application/zip"


def test_customer_tap_to_fix_goes_to_the_room_ai(dirs):
    fid = client.post("/api/v1/visual/photos", content=jpeg(), headers={"X-User-Id": ME, "Content-Type": "image/jpeg"}).json()["id"]
    r = client.post(f"/api/v1/visual/photos/{fid}/tap", json={"points": [[100, 200, 1], [300, 200, 0]]}, headers={"X-User-Id": ME})
    assert r.status_code == 200 and r.json()["mask"] == PNG and dirs[-1] == "tap"
    assert client.get("/api/v1/visual/roomai").json() == {"on": True}
