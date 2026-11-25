"""Photo-real 360°: the AI makeover for a whole 360° room, not only the view on screen.

OpenAI's image model only makes flat pictures (3:2 at most), and a 360° photo stretched flat bends the ceiling and
floor and has a seam at the back, so it can't be edited in one go. Instead:
1. The 360° photo is cut into 8 ordinary photos looking out from the same spot: 6 around (110° wide each, so
   neighbours overlap by about 50°), one straight down at the floor and one straight up at the ceiling.
2. OpenAI redraws each one with the same instruction as the flat makeover (our tiles, the customer's request), and
   with the flat makeover the customer already has as an extra picture to copy, so every view gets the same
   floor, walls, colours and light.
3. The 8 results are put back on the sphere and blended softly where they overlap.

Honest limits: each view is drawn separately, so small things (a cushion, a tile joint) can differ from one view to
the next and a soft seam can show. It costs 8 pictures (about $0.40 at medium quality) and takes 1 to 3 minutes,
so it runs as a background job the page asks about every few seconds.
"""
from __future__ import annotations

import io
import math
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import numpy as np
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field
from PIL import Image, UnidentifiedImageError

from backend import makeover_plan as mp
from backend import roomedit as re_
from backend import visual
from backend.catalog import CATALOG
from backend.observability import log_event
from backend.ratelimit import RateLimiter

router = APIRouter(tags=["AI room makeover"])
OUT_W = 3072                                       # the result: 3072 x 1536, like our 360° sample rooms
RING = [(yaw, 0.0, 110.0, 1536, 1024) for yaw in range(0, 360, 60)]   # yaw, pitch, horizontal FOV, size
CAPS = [(0.0, -90.0, 120.0, 1024, 1024), (0.0, 90.0, 120.0, 1024, 1024)]
VIEWS = RING + CAPS
FEATHER = 0.22                                     # share of each half-frame that fades out at the edges
PANO_PATH = re.compile(r"^/images/pano_[a-z0-9_]+\.jpg$")
limiter = RateLimiter(max_requests=2, window_seconds=300)
_jobs: dict[str, dict] = {}
_lock = threading.Lock()


# ------------------------------------------------------------------ geometry
def _rot(yaw: float, pitch: float) -> np.ndarray:
    """Camera to world: first tilt up/down (pitch, around x), then turn (yaw, around y). y is up, z is ahead."""
    y, p = math.radians(yaw), math.radians(pitch)
    rx = np.array([[1, 0, 0], [0, math.cos(p), math.sin(p)], [0, -math.sin(p), math.cos(p)]])
    ry = np.array([[math.cos(y), 0, math.sin(y)], [0, 1, 0], [-math.sin(y), 0, math.cos(y)]])
    return ry @ rx


def _bilinear(img: np.ndarray, x: np.ndarray, y: np.ndarray, wrap_x: bool = False) -> np.ndarray:
    h, w = img.shape[:2]
    x0 = np.floor(x).astype(np.int32); y0 = np.floor(y).astype(np.int32)
    fx = (x - x0).astype(np.float32)[..., None]; fy = (y - y0).astype(np.float32)[..., None]
    if wrap_x:
        x0 %= w; x1 = (x0 + 1) % w
    else:
        x0 = np.clip(x0, 0, w - 1); x1 = np.clip(x0 + 1, 0, w - 1)
    y0 = np.clip(y0, 0, h - 1); y1 = np.clip(y0 + 1, 0, h - 1)
    top = img[y0, x0] * (1 - fx) + img[y0, x1] * fx         # float32 out, whatever the picture's type
    bot = img[y1, x0] * (1 - fx) + img[y1, x1] * fx
    return top * (1 - fy) + bot * fy


def view_of(pano: np.ndarray, yaw: float, pitch: float, fov: float, w: int, h: int, rows: int = 256) -> np.ndarray:
    """An ordinary photo out of the 360° picture: looking towards yaw/pitch, fov degrees wide (made in bands of
    rows, to keep memory small)."""
    t = math.tan(math.radians(fov) / 2)
    R = _rot(yaw, pitch).T.astype(np.float32)
    H, W = pano.shape[:2]
    out = np.empty((h, w, 3), np.uint8)
    xs = ((np.arange(w) + 0.5) / w * 2 - 1).astype(np.float32)
    for r0 in range(0, h, rows):
        i, j = np.meshgrid(xs, ((np.arange(r0, min(h, r0 + rows)) + 0.5) / h * 2 - 1).astype(np.float32))
        d = np.stack([i * t, -j * t * h / w, np.ones_like(i)], -1) @ R
        d /= np.linalg.norm(d, axis=-1, keepdims=True)
        lon, lat = np.arctan2(d[..., 0], d[..., 2]), np.arcsin(np.clip(d[..., 1], -1, 1))
        px = _bilinear(pano, (lon / (2 * math.pi) + 0.5) * W - 0.5, (0.5 - lat / math.pi) * H - 0.5, wrap_x=True)
        out[r0:r0 + len(i)] = np.clip(px, 0, 255).astype(np.uint8)
    return out


def stitch(base: np.ndarray, views: list[tuple[tuple, np.ndarray]], rows: int = 128) -> np.ndarray:
    """Put the redrawn views back on the sphere. Each view fades out towards its edges, so overlaps blend softly;
    anything no view covers keeps the original picture. Done a band of rows at a time to keep memory small
    (the shop runs on a small server)."""
    H, W = base.shape[:2]
    out = base.copy()                                   # same type as the picture (uint8)
    lon1 = (((np.arange(W) + 0.5) / W - 0.5) * 2 * math.pi).astype(np.float32)
    for r0 in range(0, H, rows):
        lat1 = ((0.5 - (np.arange(r0, min(H, r0 + rows)) + 0.5) / H) * math.pi).astype(np.float32)
        lon, lat = np.meshgrid(lon1, lat1)
        d = np.stack([np.cos(lat) * np.sin(lon), np.sin(lat), np.cos(lat) * np.cos(lon)], -1)
        acc = np.zeros(d.shape, np.float32); wsum = np.zeros(d.shape[:2], np.float32)
        for (yaw, pitch, fov, w, h), img in views:
            c = d @ _rot(yaw, pitch).astype(np.float32)   # world -> camera (a rotation's inverse is its transpose)
            t = math.tan(math.radians(fov) / 2)
            front = c[..., 2] > 1e-6
            z = np.where(front, c[..., 2], 1.0)
            u, v = c[..., 0] / z / t, -c[..., 1] / z / (t * h / w)
            inside = front & (np.abs(u) < 1) & (np.abs(v) < 1)
            if not inside.any():
                continue
            fade = np.clip((1 - np.abs(u)) / FEATHER, 0, 1) * np.clip((1 - np.abs(v)) / FEATHER, 0, 1)
            wt = np.where(inside, fade * fade * (3 - 2 * fade), 0.0).astype(np.float32)   # smoothstep
            ih, iw = img.shape[:2]
            px = _bilinear(img, (u + 1) / 2 * iw - 0.5, (v + 1) / 2 * ih - 0.5)
            acc += px * wt[..., None]; wsum += wt
        has = wsum > 1e-6
        band = out[r0:r0 + len(lat1)]
        band[has] = np.clip(acc[has] / wsum[has][:, None] + 0.5, 0, 255).astype(out.dtype)
    return out


# ------------------------------------------------------------------ the job
def _prompt(floor, wall, extras, n_refs: int) -> str:
    style = 2 + n_refs
    return ("This is one view out of a 360° photo of a room, looking in one direction. Other views of the same room "
            "are edited the same way and joined edge to edge, so keep the exact framing and the position of "
            "everything at the edges of the picture.\n"
            + re_.build_prompt(floor, wall, extras, "photo") +
            f"\nImage {style} is the same room already redone from another angle: use exactly the same floor, wall "
            "finish, tile size, colours and light as in it, as if both were photos taken a second apart.")


def _run(job: str, pano: Image.Image, style: bytes, floor, wall, extras, owner: str, quality: str):
    def set_(**k):
        with _lock:
            _jobs[job].update(k)
    try:
        src = np.asarray(pano.convert("RGB").resize((OUT_W, OUT_W // 2), Image.LANCZOS))     # uint8: small
        refs = [re_.tile_picture(s) for s in (floor, wall) if s] + [style]
        prompt = _prompt(floor, wall, extras, len(refs) - 1)
        shots = [(spec, re_._jpeg(Image.fromarray(view_of(src, *spec)), 92))
                 for spec in VIEWS]                               # cut one at a time, kept as JPEG

        def one(item):
            (yaw, pitch, fov, w, h), jpg = item
            out, model = re_.edit_image(jpg, refs, prompt, f"{w}x{h}", quality)
            got = Image.open(io.BytesIO(out)).convert("RGB")
            if got.size != (w, h):
                got = got.resize((w, h), Image.LANCZOS)
            with _lock:
                _jobs[job]["done"] += 1
            return (yaw, pitch, fov, w, h), np.asarray(got)

        t0 = time.time()
        with ThreadPoolExecutor(max_workers=4) as ex:
            views = list(ex.map(one, shots))
        set_(state="joining")
        out = stitch(src, views)
        b = io.BytesIO(); Image.fromarray(out).save(b, "JPEG", quality=88)
        names = [f"{CATALOG[floor].name} floor" if floor else "", f"{CATALOG[wall].name} walls" if wall else ""]
        saved = visual.save_image(b.getvalue(), owner, "render",
                                  {"room": "AI makeover 360°", "tiles": " and ".join(n for n in names if n) or "Your idea"})
        seconds = round(time.time() - t0, 1)
        log_event("makeover360_done", seconds=seconds, floor=floor, wall=wall)
        set_(state="done", after=saved["url"], seconds=seconds)
    except HTTPException as e:                     # edit_image explains OpenAI's problems in plain words
        d = e.detail
        set_(state="failed", error=d.get("message") if isinstance(d, dict) else str(d))
    except Exception as e:                         # noqa: BLE001 - a job must always end with a message
        log_event("makeover360_error", error=type(e).__name__)
        set_(state="failed", error="The 360° picture could not be made. Please try again.")


# ------------------------------------------------------------------ API
class Makeover360Request(BaseModel):
    pano: str = Field(default="", max_length=120)           # one of our 360° rooms: /images/pano_....jpg
    photo_id: str = Field(default="", pattern=r"^([a-f0-9]{32})?$")   # or the customer's own 360° photo
    style_id: str = Field(pattern=r"^[a-f0-9]{32}$")        # the flat makeover the customer already has (to copy)
    floor: Optional[str] = Field(default=None, max_length=20)
    wall: Optional[str] = Field(default=None, max_length=20)
    request: str = Field(default="", max_length=300)
    quality: str = Field(default="medium", pattern=r"^(medium|high)$")


@router.post("/api/v1/makeover/360")
def start(req: Makeover360Request, x_user_id: Optional[str] = Header(default=None)):
    if not x_user_id or not visual.VISITOR.match(x_user_id):
        raise HTTPException(status_code=400, detail="Missing visitor id.")
    floor, wall, text = (req.floor or None), (req.wall or None), " ".join(req.request.split())
    if floor and not mp.fits(floor, "floor"):
        re_.err("floor", "Pick one of our floor tiles.")
    if wall and not mp.fits(wall, "wall"):
        re_.err("wall", "Pick one of our wall tiles.")
    if not (floor or wall or len(text) >= 3):
        re_.err("request", "Pick a floor or wall tile, or write what you'd like to change.")
    if not re_.is_on():
        re_.err("general", "AI makeovers are not switched on yet (the shop owner adds an OpenAI key).", 503)
    if req.photo_id:
        path = visual._own_photo(req.photo_id, x_user_id)
    elif PANO_PATH.match(req.pano) and (re_.ROOT / req.pano.lstrip("/")).exists():
        path = re_.ROOT / req.pano.lstrip("/")
    else:
        re_.err("pano", "This 360° room can't be found.", 404)
    style_path = visual._own_photo(req.style_id, x_user_id)
    try:
        pano = Image.open(path); pano.load()
        style = Image.open(style_path).convert("RGB"); style.thumbnail((1024, 1024))
    except (UnidentifiedImageError, OSError):
        re_.err("pano", "This 360° picture can't be read.")
    if pano.width < 1.9 * pano.height:
        re_.err("pano", "This is not a 360° picture.")
    if not limiter.allow(x_user_id):
        re_.err("general", "A 360° makeover uses 8 AI pictures, so please wait a few minutes before the next one.", 429)
    for _ in VIEWS:
        if not re_._count_today():
            re_.err("general", "Today's AI makeovers are used up. Please try again tomorrow.", 429)
    plan = mp.make_plan(text, floor, wall)
    job = secrets.token_hex(8)
    with _lock:
        for k in [k for k, j in _jobs.items() if time.time() - j["started"] > 3600]:
            del _jobs[k]                                     # old jobs are forgotten after an hour
        _jobs[job] = {"owner": x_user_id, "state": "drawing", "done": 0, "total": len(VIEWS), "started": time.time()}
    threading.Thread(target=_run, args=(job, pano, re_._jpeg(style, 90), plan.floor, plan.wall, mp.instructions(plan),
                                        x_user_id, req.quality), daemon=True).start()
    return {"job": job, "total": len(VIEWS), "cost_usd": round(re_.COST_USD[req.quality] * len(VIEWS), 2)}


@router.get("/api/v1/makeover/360/{job}")
def progress(job: str, x_user_id: Optional[str] = Header(default=None)):
    with _lock:
        j = dict(_jobs.get(job) or {})
    if not j or j.get("owner") != x_user_id:
        raise HTTPException(status_code=404, detail="This 360° makeover can't be found. Please start it again.")
    return {k: j.get(k) for k in ("state", "done", "total", "after", "error", "seconds")}
