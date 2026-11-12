"""Step C2: the customer's own room photo, the phone hand-off by QR code, and saved room previews."""
import io
import json
import time
from datetime import date

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from backend import main, visual
from backend.db.database import Base, SessionLocal, engine, init_db
from backend.db.models import Activity, ChatSession, Lead
from backend.db.seed import seed_tenant

client = TestClient(main.app)
ME, OTHER = "USR-1111111111", "USR-2222222222"


@pytest.fixture(scope="module", autouse=True)
def demo_db():
    Base.metadata.drop_all(bind=engine)
    init_db()
    with SessionLocal() as db:
        seed_tenant(db, today=date(2026, 11, 10))
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(visual, "STORE", tmp_path)
    visual.limiter._hits.clear()
    visual._handoffs.clear()
    main.rate_limiter._hits.clear()


def jpeg(w=1200, h=900, gps=False) -> bytes:
    im = Image.new("RGB", (w, h), (200, 190, 180))
    buf = io.BytesIO()
    if gps:                                   # a photo with EXIF, like a phone's (GPS IFD tag 0x8825)
        exif = Image.Exif()
        exif[0x010F] = "PhoneMaker"
        exif[0x8825] = {1: "N", 2: (25.0, 17.0, 0.0)}
        im.save(buf, "JPEG", exif=exif)
    else:
        im.save(buf, "JPEG")
    return buf.getvalue()


def upload(data, who=ME):
    return client.post("/api/v1/visual/photos", content=data, headers={"X-User-Id": who, "Content-Type": "image/jpeg"})


def test_photo_upload_is_resized_and_loses_its_exif(tmp_path):
    r = upload(jpeg(3000, 2000, gps=True))
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["width"] == 2000 and d["height"] == 1333 and d["url"] == f"/api/v1/visual/files/{d['id']}.jpg"
    stored = Image.open(tmp_path / f"{d['id']}.jpg")
    assert not stored.getexif()                                      # no GPS, no phone model
    assert json.loads((tmp_path / f"{d['id']}.json").read_text())["owner"] == ME
    got = client.get(d["url"])
    assert got.status_code == 200 and got.headers["content-type"] == "image/jpeg"


def test_bad_uploads_are_refused():
    assert upload(b"not an image at all").status_code == 422
    assert upload(jpeg(200, 150)).status_code == 422                 # too small to be a room photo
    assert client.post("/api/v1/visual/photos", content=jpeg()).status_code == 400   # no visitor id
    assert client.get("/api/v1/visual/files/../../etc/passwd").status_code == 404
    assert client.get("/api/v1/visual/files/" + "0" * 32 + ".jpg").status_code == 404


def test_old_photos_are_deleted_after_7_days(tmp_path):
    d = upload(jpeg()).json()
    meta = tmp_path / f"{d['id']}.json"
    m = json.loads(meta.read_text()); m["created"] = time.time() - 8 * 86400; meta.write_text(json.dumps(m))
    assert visual._clean_old() == 1
    assert not (tmp_path / f"{d['id']}.jpg").exists()
    assert client.get(d["url"]).status_code == 404


def test_phone_handoff_works_once_and_only_for_its_screen():
    h = client.post("/api/v1/visual/handoff", headers={"X-User-Id": ME}).json()
    assert h["url"].endswith("/m/" + h["code"]) and h["expires_in"] == 900
    qr = client.get(h["qr"])
    assert qr.status_code == 200 and qr.headers["content-type"].startswith("image/svg") and b"<svg" in qr.content
    assert "Send a photo" in client.get(f"/m/{h['code']}").text      # the page the phone opens
    status = lambda who=ME: client.get(f"/api/v1/visual/handoff/{h['code']}", headers={"X-User-Id": who})
    assert status().json()["status"] == "waiting"
    assert status(OTHER).status_code == 403                          # another screen can't collect it
    sent = client.post(f"/api/v1/visual/handoff/{h['code']}/photo", content=jpeg(), headers={"Content-Type": "image/jpeg"})
    assert sent.status_code == 200
    done = status().json()
    assert done["status"] == "done" and done["photo"]["url"].startswith("/api/v1/visual/files/")
    again = client.post(f"/api/v1/visual/handoff/{h['code']}/photo", content=jpeg(), headers={"Content-Type": "image/jpeg"})
    assert again.status_code == 409                                  # one photo per link
    assert client.get("/api/v1/visual/handoff/not-a-real-code-12345").status_code == 404


def test_expired_handoff_link_is_refused():
    h = client.post("/api/v1/visual/handoff", headers={"X-User-Id": ME}).json()
    visual._handoffs[h["code"]]["expires"] = time.time() - 1
    r = client.post(f"/api/v1/visual/handoff/{h['code']}/photo", content=jpeg(), headers={"Content-Type": "image/jpeg"})
    assert r.status_code == 410
    assert client.get(f"/api/v1/visual/handoff/{h['code']}", headers={"X-User-Id": ME}).json()["status"] == "expired"


def test_saved_preview_goes_on_the_customers_lead():
    chat = client.post("/api/v1/chat", json={"user_id": ME, "message": "Bathroom floor 20 m2, modern, budget QAR 5000"}).json()
    sid = chat["session_id"]
    r = client.post(f"/api/v1/visual/renders?session_id={sid}&room=Bathroom&tiles=Arctic Pearl on the floor",
                    content=jpeg(), headers={"X-User-Id": ME, "Content-Type": "image/jpeg"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["share_url"].endswith(d["url"]) and d["lead"]
    with SessionLocal() as db:
        lead = db.scalar(select(Lead).join(ChatSession, ChatSession.lead_id == Lead.id).where(ChatSession.session_id == sid))
        acts = db.scalars(select(Activity).where(Activity.lead_id == lead.id, Activity.type == "room_preview")).all()
    assert len(acts) == 1 and d["url"] in acts[0].text and "Arctic Pearl" in acts[0].text
    # someone else can't attach a picture to this chat's lead
    r2 = client.post(f"/api/v1/visual/renders?session_id={sid}", content=jpeg(), headers={"X-User-Id": OTHER, "Content-Type": "image/jpeg"})
    assert r2.status_code == 403
