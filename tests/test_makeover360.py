"""Photo-real 360° makeover (backend/makeover360.py): the 360° room is cut into 8 views, each redrawn by OpenAI
(a fake here, so nothing is paid) and joined back into one 360° picture."""
import io
import math
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backend import main, makeover360 as m360, roomedit, visual
from tests.test_makeover import FakeOpenAI, jpeg, ME, OTHER

client = TestClient(main.app)
PANO = "/images/pano_ph_kiara_interior.jpg"


@pytest.fixture(autouse=True)
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(visual, "STORE", tmp_path)
    visual.limiter._hits.clear(); m360.limiter._hits.clear()
    roomedit._day.update(date=None, count=0)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    fake = FakeOpenAI()
    monkeypatch.setattr(roomedit, "_post", fake)
    return fake


def style(who=ME) -> str:
    r = client.post("/api/v1/visual/photos", content=jpeg(1536, 1024), headers={"Content-Type": "image/jpeg", "X-User-Id": who})
    return r.json()["id"]


def wait(job, who=ME):
    for _ in range(300):
        d = client.get(f"/api/v1/makeover/360/{job}", headers={"X-User-Id": who}).json()
        if d["state"] in ("done", "failed"):
            return d
        time.sleep(0.1)
    raise AssertionError("the job never finished")


def test_views_cut_from_the_360_join_back_into_the_same_picture():
    src = np.asarray(Image.open(visual.ROOT / PANO.lstrip("/")).convert("RGB").resize((768, 384)))
    views = [(s, m360.view_of(src, *s)) for s in m360.VIEWS]
    out = m360.stitch(src, views)
    assert np.abs(out.astype(float) - src).mean() < 4          # only resampling blur
    blank = m360.stitch(np.zeros_like(src), [(s, np.full((s[4], s[3], 3), 200, np.uint8)) for s in m360.VIEWS])
    assert (blank.sum(-1) == 0).sum() == 0                       # every direction is covered by some view


def test_a_360_makeover_redraws_8_views_and_returns_one_360_picture(setup):
    r = client.post("/api/v1/makeover/360", json={"pano": PANO, "style_id": style(), "floor": "CN005"}, headers={"X-User-Id": ME})
    assert r.status_code == 200, r.text
    d = wait(r.json()["job"])
    assert d["state"] == "done" and d["done"] == 8
    assert len(setup.calls) == 8
    sizes = sorted(c["data"]["size"] for c in setup.calls)
    assert sizes.count("1536x1024") == 6 and sizes.count("1024x1024") == 2
    first = setup.calls[0]
    assert "360" in first["data"]["prompt"] and "Urban Graphite" in first["data"]["prompt"]
    assert len(first["files"]) == 3                              # the view, our tile, the flat makeover to copy
    im = Image.open(io.BytesIO(client.get(d["after"]).content))
    assert im.size == (3072, 1536)
    assert client.get(f"/api/v1/makeover/360/{r.json()['job']}", headers={"X-User-Id": OTHER}).status_code == 404


def test_360_makeover_checks_its_input(monkeypatch):
    sid = style()
    bad = client.post("/api/v1/makeover/360", json={"pano": "/images/../backend/main.py", "style_id": sid, "floor": "CN005"}, headers={"X-User-Id": ME})
    assert bad.status_code == 404 and bad.json()["detail"]["field"] == "pano"
    flat = client.post("/api/v1/makeover/360", json={"pano": "/images/pano_ph_kiara_interior_thumb.jpg", "style_id": sid, "floor": "CN005"}, headers={"X-User-Id": ME})
    assert flat.status_code in (404, 422)
    other = client.post("/api/v1/makeover/360", json={"pano": PANO, "style_id": style(OTHER), "floor": "CN005"}, headers={"X-User-Id": ME})
    assert other.status_code == 403                              # someone else's makeover can't be copied
    empty = client.post("/api/v1/makeover/360", json={"pano": PANO, "style_id": sid}, headers={"X-User-Id": ME})
    assert empty.status_code == 422 and empty.json()["detail"]["field"] == "request"
    monkeypatch.delenv("OPENAI_API_KEY")
    off = client.post("/api/v1/makeover/360", json={"pano": PANO, "style_id": sid, "floor": "CN005"}, headers={"X-User-Id": ME})
    assert off.status_code == 503


def test_openai_trouble_ends_the_job_with_a_plain_message(setup):
    setup.answers = [(429, {"error": {"message": "Rate limit"}})] * 8
    r = client.post("/api/v1/makeover/360", json={"pano": PANO, "style_id": style(), "wall": "CN010"}, headers={"X-User-Id": ME})
    d = wait(r.json()["job"])
    assert d["state"] == "failed" and "OpenAI is busy" in d["error"]
