"""The room AI as a small web service. The shop sends a photo, the service answers which parts are floor
and walls (a cut-out picture per surface) and how each surface lies in space (so tiles keep their real size).

It runs on a machine with a graphics card that is paid per second (Modal, see modal_app.py), because the
shop's own server is too small for the three AI models. It works on any computer too:

    pip install -r requirements-roomai.txt
    ROOMAI_TOKEN=some-long-secret uvicorn backend.roomai.service:app --port 8078

POST /tap?points=[[x,y,1],...]  (body = the photo) -> {"mask": "data:image/png...", "share"}: one cut-out for a tap
POST /analyse  (body = the photo, header Authorization: Bearer <ROOMAI_TOKEN>)
-> {"width", "height", "scale", "surfaces": [{"name", "kind": "floor"|"wall", "mask": "data:image/png;base64,...",
     "quad": [[x, y] x4], "size": [100, 100], "area_m2"}], "timings"}
quad + size use the same format as the visualiser's hand-placed dots: the 4 corners (in photo pixels)
of a 100 x 100 cm square lying on that surface.
"""
from __future__ import annotations

import base64
import hmac
import io
import os
import threading

import numpy as np
import json

from fastapi import FastAPI, Header, HTTPException, Query, Request
from PIL import Image, ImageOps, UnidentifiedImageError

from . import analyse as _analyse

app = FastAPI(title="Nova room AI", docs_url=None, redoc_url=None)
_busy = threading.Lock()              # one photo at a time per machine (the graphics card is shared)
MAX_BYTES = 15 * 1024 * 1024
SQUARE_CM = 100.0


def _check(auth: str | None):
    token = os.getenv("ROOMAI_TOKEN", "")
    if not token:
        raise HTTPException(status_code=503, detail="ROOMAI_TOKEN is not set on the room AI service.")
    given = (auth or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(given, token):
        raise HTTPException(status_code=401, detail="Wrong token.")


def smooth(mask: np.ndarray) -> np.ndarray:
    """Straighten ragged cut-out edges (a torn-paper edge along the ceiling line) without moving them."""
    from scipy.ndimage import gaussian_filter
    sigma = max(1.0, mask.shape[1] / 400)
    return gaussian_filter(mask.astype(np.float32), sigma) > 0.5


def _mask_png(mask: np.ndarray) -> str:
    """White where the surface is, see-through elsewhere (the visualiser uses it as its area)."""
    a = smooth(mask).astype(np.uint8) * 255
    im = Image.merge("LA", [Image.new("L", (a.shape[1], a.shape[0]), 255), Image.fromarray(a)])
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def square_on_plane(uv: np.ndarray, mask: np.ndarray, plane, K: np.ndarray, k: float):
    """Corners (photo pixels) of a 100 x 100 cm square on the plane, placed in the middle of the visible part.
    Any square on the plane gives the visualiser the same stretch, so one in plain view is the safest."""
    ok = mask & np.isfinite(uv[..., 0]) & np.isfinite(uv[..., 1])
    if ok.sum() < 50:
        return None
    u0, v0 = np.median(uv[ok], 0)
    e1, e2 = plane.axes
    base = plane.dist * plane.normal          # the point of the plane closest to the camera (u = v = 0 there)
    pts = []
    for du, dv in [(0, 0), (SQUARE_CM, 0), (SQUARE_CM, SQUARE_CM), (0, SQUARE_CM)]:
        X = base + (u0 + du) * e1 + (v0 + dv) * e2
        p = K @ X
        if p[2] <= 1e-6:
            return None
        pts.append([round(float(p[0] / p[2]) * k, 2), round(float(p[1] / p[2]) * k, 2)])
    return pts


def area_m2(uv: np.ndarray, mask: np.ndarray, k: float) -> float:
    """Visible area in m²: each pixel covers |du/dx * dv/dy - du/dy * dv/dx| cm² of the surface."""
    du_y, du_x = np.gradient(uv[..., 0])
    dv_y, dv_x = np.gradient(uv[..., 1])
    a = np.abs(du_x * dv_y - du_y * dv_x)
    ok = mask & np.isfinite(a)
    if not ok.any():
        return 0.0
    a = np.minimum(a[ok], np.percentile(a[ok], 99))       # a few pixels at the horizon can be huge
    return round(float(a.sum()) / 10000, 1)


def run(photo: Image.Image) -> dict:
    res = _analyse.analyse(photo)
    img = res["image"]
    k = photo.size[0] / img.size[0]          # analysed copy -> sent photo
    out = []
    for s in res["surfaces"]:
        kind = "floor" if s["name"] == "floor" else "wall"
        quad = square_on_plane(s["uv"], s["mask"], s["plane"], res["K"], k)
        if quad is None:
            continue
        out.append({"name": s["name"], "kind": kind, "mask": _mask_png(s["mask"]), "quad": quad,
                    "size": [SQUARE_CM, SQUARE_CM], "area_m2": area_m2(s["uv"], s["mask"], 1.0)})
    return {"width": photo.size[0], "height": photo.size[1], "scale": round(float(res["scale"]), 3),
            "scale_from": [str(x) for x in res.get("scale_from", [])][:6],
            "surfaces": out, "timings": {k2: round(v, 2) for k2, v in res["timings"].items()}}


@app.get("/health")
def health():
    return {"ok": True}


async def _photo(request: Request) -> Image.Image:
    data = await request.body()
    if not data or len(data) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="Send one photo, at most 15 MB.")
    try:
        return ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    except (UnidentifiedImageError, OSError):
        raise HTTPException(status_code=422, detail="Not a photo.")


@app.post("/analyse")
async def analyse_photo(request: Request, authorization: str | None = Header(default=None)):
    _check(authorization)
    photo = await _photo(request)
    with _busy:
        return run(photo)


def tap(photo: Image.Image, points: list) -> dict:
    """Tap to fix: the customer taps a spot ("this") and maybe spots that are NOT part of it.
    points = [[x, y, 1 or 0], ...] in photo pixels. Returns the cut-out of that one thing or area."""
    img = _analyse._resize(photo)
    k = img.size[0] / photo.size[0]
    pts = [[float(x) * k, float(y) * k] for x, y, _ in points]
    labels = [1 if int(l) else 0 for _, _, l in points]
    from . import models
    m = models.segment_points(img, pts, labels)
    return {"width": photo.size[0], "height": photo.size[1], "mask": _mask_png(m), "share": round(float(m.mean()), 4)}


@app.post("/tap")
async def tap_photo(request: Request, points: str = Query(..., max_length=400),
                    authorization: str | None = Header(default=None)):
    _check(authorization)
    try:
        pts = json.loads(points)
        assert isinstance(pts, list) and 1 <= len(pts) <= 8 and all(len(p) == 3 for p in pts)
    except (ValueError, AssertionError, TypeError):
        raise HTTPException(status_code=422, detail="points must be [[x, y, 1|0], ...] (1 to 8 taps).")
    photo = await _photo(request)
    with _busy:
        return tap(photo, pts)
