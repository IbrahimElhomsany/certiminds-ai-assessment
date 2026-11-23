"""AI room makeover (backend/roomedit.py): OpenAI's image model re-draws a room photo with our tiles.
OpenAI itself is replaced by a fake here, so the tests cost nothing and need no key."""
import base64
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backend import main, roomedit, visual

client = TestClient(main.app)
ME, OTHER = "USR-1111111111", "USR-2222222222"


def jpeg(w=1200, h=900, colour=(200, 190, 180)) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (w, h), colour).save(b, "JPEG")
    return b.getvalue()


class FakeOpenAI:
    def __init__(self, answers=None):
        self.calls, self.answers = [], list(answers or [])

    def __call__(self, files, data, timeout):
        self.calls.append({"files": files, "data": data})
        if self.answers:
            return self.answers.pop(0)
        w, h = map(int, data["size"].split("x"))
        return 200, {"data": [{"b64_json": base64.b64encode(jpeg(w, h, (90, 90, 90))).decode()}]}


@pytest.fixture(autouse=True)
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(visual, "STORE", tmp_path)
    visual.limiter._hits.clear()
    roomedit.limiter._hits.clear()
    roomedit._day.update(date=None, count=0)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("OPENAI_IMAGE_MODEL", raising=False)
    fake = FakeOpenAI()
    monkeypatch.setattr(roomedit, "_post", fake)
    return fake


def upload(who=ME, w=1200, h=900) -> str:
    r = client.post("/api/v1/visual/photos", content=jpeg(w, h), headers={"Content-Type": "image/jpeg", "X-User-Id": who})
    assert r.status_code == 200
    return r.json()["id"]


def make(body, who=ME):
    return client.post("/api/v1/makeover", json=body, headers={"X-User-Id": who})


def test_status_lists_our_tiles_and_whether_it_is_on(monkeypatch):
    d = client.get("/api/v1/makeover").json()
    assert d["on"] is True and any(t["sku"] == "CN001" and "floor" in t["surfaces"] for t in d["tiles"])
    monkeypatch.delenv("OPENAI_API_KEY")
    assert client.get("/api/v1/makeover").json()["on"] is False


def test_our_tile_pictures_and_sizes_go_to_the_ai(setup):
    r = make({"photo_id": upload(), "floor": "CN001", "wall": "CN007"})
    assert r.status_code == 200, r.text
    d = r.json()
    call = setup.calls[0]
    assert [f[0] for f in call["files"]] == ["image[]"] * 3            # room + floor tile + wall tile
    p = call["data"]["prompt"]
    assert "Arctic Pearl" in p and "60 x 60 cm" in p and "Ivory Mist" in p and "30 x 60 cm" in p
    assert "Keep everything else exactly as it is" in p
    assert call["data"]["size"] == "1536x1024" and call["data"]["quality"] == "medium"
    assert call["data"]["model"] == "gpt-image-2"
    assert d["tiles"] == "Arctic Pearl floor and Ivory Mist walls"
    # before and after are the same size, so the slider lines up
    before = Image.open(visual.STORE / (d["before"].split("/")[-1]))
    after = Image.open(visual.STORE / (d["after"].split("/")[-1]))
    assert before.size == after.size == (1536, 1024) and d["width"] == 1536
    assert d["share_url"].endswith(d["after"])


def test_words_alone_are_enough(setup):
    r = make({"photo_id": upload(w=900, h=1200), "request": "paint the walls   light grey"})
    assert r.status_code == 200
    call = setup.calls[0]
    assert len(call["files"]) == 1 and call["data"]["size"] == "1024x1536"
    assert '"paint the walls light grey"' in call["data"]["prompt"]


def test_errors_name_the_field():
    pid = upload()
    assert make({"photo_id": pid}).json()["detail"] == {"field": "request", "message": "Pick a floor or wall tile, or write what you'd like to change."}
    assert make({"photo_id": pid, "floor": "CN007"}).json()["detail"]["field"] == "floor"   # a wall-only tile
    assert make({"photo_id": pid, "wall": "CN001"}).json()["detail"]["field"] == "wall"
    assert make({"photo_id": pid, "request": "ab"}).json()["detail"]["field"] == "request"
    assert make({"photo_id": pid, "floor": "CN001"}, who=OTHER).status_code == 403          # someone else's photo


def test_off_without_a_key(monkeypatch, setup):
    monkeypatch.delenv("OPENAI_API_KEY")
    r = make({"photo_id": upload(), "floor": "CN001"})
    assert r.status_code == 503 and r.json()["detail"]["field"] == "general" and not setup.calls


def test_falls_back_to_the_older_model_only_when_the_model_is_missing(monkeypatch):
    fake = FakeOpenAI([(404, {"error": {"message": "The model gpt-image-2 does not exist"}})])
    monkeypatch.setattr(roomedit, "_post", fake)
    r = make({"photo_id": upload(), "floor": "CN003"})
    assert r.status_code == 200 and r.json()["model"] == "gpt-image-1.5"
    assert [c["data"]["model"] for c in fake.calls] == ["gpt-image-2", "gpt-image-1.5"]
    bad = FakeOpenAI([(401, {"error": {"message": "Incorrect API key"}})])
    monkeypatch.setattr(roomedit, "_post", bad)
    roomedit.limiter._hits.clear()
    r = make({"photo_id": upload(), "floor": "CN003"})
    assert r.status_code == 502 and "key is not valid" in r.json()["detail"]["message"] and len(bad.calls) == 1


def test_daily_cap_protects_the_bill(monkeypatch):
    monkeypatch.setenv("OPENAI_DAILY_IMAGES", "1")
    pid = upload()
    assert make({"photo_id": pid, "floor": "CN001"}).status_code == 200
    roomedit.limiter._hits.clear()
    r = make({"photo_id": pid, "floor": "CN002"})
    assert r.status_code == 429 and "used up" in r.json()["detail"]["message"]


def test_page_is_served():
    r = client.get("/makeover")
    assert r.status_code == 200 and "AI ROOM MAKEOVER" in r.text


def test_all_nine_sample_rooms_are_offered():
    page = client.get("/makeover").text
    for f in ("vis_living", "vis_tvroom", "vis_bedroom", "vis_kitchen", "vis_bathroom", "vis_spa", "vis_office",
              "vis_terrace", "showroom_lounge"):
        assert f'"{f}"' in page and (visual.ROOT / "images" / f"{f}.jpg").exists()
