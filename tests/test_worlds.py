"""World Labs walkable rooms: only the owner can start one, only with confirm, never twice, and the
finished splats are downloaded once and served from our own disk. World Labs itself is faked here."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import admin_api, main, worlds
from backend.db.database import Base, SessionLocal, engine, init_db
from backend.db.seed import DEMO_PASSWORD, seed_tenant

client = TestClient(main.app, base_url="https://nova.test")


@pytest.fixture(scope="module", autouse=True)
def demo_db():
    Base.metadata.drop_all(bind=engine)
    init_db()
    with SessionLocal() as db:
        seed_tenant(db)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def setup(tmp_path, monkeypatch):
    admin_api.login_limiter._hits.clear()
    monkeypatch.setattr(worlds, "STORE", tmp_path / "uploads")
    monkeypatch.setattr(worlds, "POLL_EVERY", 0)
    monkeypatch.setenv("WORLDLABS_API_KEY", "test-key")


def auth(email):
    r = client.post("/api/admin/login", json={"company": "ceramica-nova", "email": email, "password": DEMO_PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


class FakeWL:
    """Stands in for api.worldlabs.ai: records requests, answers like the docs say."""
    def __init__(self):
        self.calls, self.done = [], False

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append((req.method, str(req.url), req.headers.get("WLT-Api-Key"), req.content))
        if req.method == "POST" and req.url.path.endswith("/worlds:generate"):
            return httpx.Response(200, json={"operation_id": "op-1", "done": False})
        if req.url.path.endswith("/operations/op-1"):
            if not self.done:
                return httpx.Response(200, json={"operation_id": "op-1", "done": False, "metadata": {"progress": 40}})
            return httpx.Response(200, json={"operation_id": "op-1", "done": True, "cost": 1500, "response": {
                "id": "w-9", "world_marble_url": "https://marble.worldlabs.ai/world/w-9",
                "assets": {"caption": "A bright lounge", "splats": {
                    "spz_urls": {"100k": "https://cdn.test/w-9-100k.spz", "500k": "https://cdn.test/w-9-500k.spz"},
                    "semantics_metadata": {"metric_scale_factor": 1.7, "ground_plane_offset": 1.45}}}}})
        if req.url.host == "cdn.test":
            return httpx.Response(200, content=b"SPZ" + req.url.path.encode())
        return httpx.Response(404)


@pytest.fixture
def wl(monkeypatch):
    fake = FakeWL()
    transport = httpx.MockTransport(fake.handler)
    real_client = httpx.Client
    monkeypatch.setattr(worlds, "_client", lambda: real_client(transport=transport, headers={"WLT-Api-Key": worlds._api_key()}))
    monkeypatch.setattr(worlds.httpx, "stream", lambda m, u, **kw: real_client(transport=transport).stream(m, u))
    return fake


def test_nothing_until_asked(wl):
    assert client.get("/api/worlds/tvroom").json()["status"] == "none"
    assert client.get("/worlds/tvroom.spz").status_code == 404
    assert client.get("/api/worlds/nope").status_code == 404
    assert wl.calls == []


def test_only_owner_and_only_with_confirm(wl):
    assert client.post("/api/admin/worlds/tvroom/generate", json={"confirm": True}).status_code == 401
    assert client.post("/api/admin/worlds/tvroom/generate", json={"confirm": True},
                       headers=auth("manager@ceramicanova.demo")).status_code == 403
    r = client.post("/api/admin/worlds/tvroom/generate", json={}, headers=auth("owner@ceramicanova.demo"))
    assert r.status_code == 400 and "1,500" in r.json()["detail"]
    assert wl.calls == []


def test_generate_poll_download_and_never_twice(wl):
    owner = auth("owner@ceramicanova.demo")
    r = client.post("/api/admin/worlds/tvroom/generate", json={"confirm": True}, headers=owner)
    assert r.status_code == 200 and r.json()["status"] == "pending"
    method, url, key, body = wl.calls[0]
    sent = json.loads(body)
    assert url.endswith("/marble/v1/worlds:generate") and key == "test-key"
    assert sent["world_prompt"]["image_prompt"]["uri"] == "https://nova.test/images/pano_tvroom.jpg"
    assert sent["world_prompt"]["is_pano"] is True and sent["model"] == "marble-1.1"
    # asking again while it is being made does not start (or pay for) a second one
    client.post("/api/admin/worlds/tvroom/generate", json={"confirm": True}, headers=owner)
    assert sum(1 for c in wl.calls if c[0] == "POST") == 1
    assert client.get("/api/worlds/tvroom").json()["status"] == "pending"
    wl.done = True
    s = client.get("/api/worlds/tvroom").json()
    assert s["status"] == "ready" and s["url"] == "/worlds/tvroom.spz" and s["scale"] == 1.7 and s["offset"] == 1.45
    assert "key" not in json.dumps(s).replace('"key": "tvroom"', "")          # nothing secret goes to the browser
    f = client.get("/worlds/tvroom.spz")
    assert f.status_code == 200 and f.content == b"SPZ/w-9-500k.spz"
    n = len(wl.calls)
    client.get("/api/worlds/tvroom"); client.get("/worlds/tvroom.spz")
    client.post("/api/admin/worlds/tvroom/generate", json={"confirm": True}, headers=owner)
    assert len(wl.calls) == n                                                  # ready: World Labs is not asked again


def test_needs_the_key(wl, monkeypatch):
    monkeypatch.delenv("WORLDLABS_API_KEY")
    r = client.post("/api/admin/worlds/tvroom/generate", json={"confirm": True}, headers=auth("owner@ceramicanova.demo"))
    assert r.status_code == 503 and wl.calls == []


class SyncThread:                                                 # run the auto start right away in the test
    def __init__(self, target, **kw): self.target = target
    def start(self): self.target()


def test_auto_start_on_the_live_site_once(wl, monkeypatch):
    monkeypatch.setattr(worlds.threading, "Thread", SyncThread)
    monkeypatch.setattr(worlds.time, "sleep", lambda s: setattr(wl, "done", True))
    worlds.start_auto_worlds()                                    # not on Render: does nothing
    assert wl.calls == []
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://nova.example.com")
    worlds.start_auto_worlds()
    posts = [c for c in wl.calls if c[0] == "POST"]
    assert len(posts) == 1 and json.loads(posts[0][3])["world_prompt"]["image_prompt"]["uri"] == "https://nova.example.com/images/pano_tvroom.jpg"
    assert client.get("/api/worlds").json()[0]["status"] == "ready"   # it kept checking until the room was saved
    worlds.start_auto_worlds()                                    # a restart never makes it again
    assert len([c for c in wl.calls if c[0] == "POST"]) == 1


def test_auto_start_does_not_retry_a_failed_room(wl, monkeypatch):
    monkeypatch.setattr(worlds.threading, "Thread", SyncThread)
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://nova.example.com")
    worlds._write("tvroom", {"status": "failed", "error": "x"})
    worlds.start_auto_worlds()
    assert wl.calls == []
