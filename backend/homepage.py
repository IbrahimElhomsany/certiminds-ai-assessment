"""The shop's home page: a film at the top and "shop the showroom" pictures (displays with a dot on each product).

The pictures that ship with the shop (data/homepage.json, images/home/show_*.jpg) are views of our showroom rendered
in Blender with our exact tiles and bathroom sets (tools/showroom/build_showroom.py --views, which also works out
where each dot goes). In Admin > Settings > Home page the owner can:
* make a picture photo-real: OpenAI's image model re-draws the render as a real photograph of the same showroom
  (backend/roomedit.py, needs OPENAI_API_KEY); "Back to the render" undoes it,
* make a new scene: pick a sample room and tiles, and OpenAI's image model re-draws it as a real photo,
* move the dots, rename, reorder or remove scenes,
* upload the film for the top of the page (an mp4 made with an AI video tool or filmed in a real showroom).
Until a film is uploaded, the top of the page slowly moves across the first scenes, like a camera.

The owner's version lives next to the database (same persistent disk): <dir>/home.json, scene pictures and the film.
"""
from __future__ import annotations

import io
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from PIL import Image
from pydantic import BaseModel, Field

from backend import roomedit, visual
from backend.auth import require
from backend.catalog import CATALOG, SANITARY
from backend.observability import log_event

router = APIRouter(tags=["Home page"])
DEFAULTS = visual.ROOT / "data" / "homepage.json"
ROOMS = {"living": "Living room", "tvroom": "TV lounge", "bedroom": "Bedroom", "kitchen": "Kitchen", "bathroom": "Bathroom",
         "spa": "Spa bathroom", "office": "Office", "terrace": "Terrace", "lounge": "Showroom lounge"}
OUTDOOR = {"terrace"}
SID = re.compile(r"^[a-z0-9]{3,24}$")
FILE = re.compile(r"^[a-z0-9_]{3,40}\.(jpg|mp4|webm)$")
MAX_SCENES, MAX_FILM = 12, 40 * 1024 * 1024
_lock = threading.Lock()


def home_dir() -> Path:
    return Path(os.getenv("HOMEPAGE_DIR") or visual.STORE.parent / "homepage")


def room_photo(room: str) -> Path:
    return visual.ROOT / "images" / ("showroom_lounge.jpg" if room == "lounge" else f"vis_{room}.jpg")


def load() -> dict:
    base = json.loads(DEFAULTS.read_text())
    own = home_dir() / "home.json"
    try:
        cfg = json.loads(own.read_text()) if own.exists() else base
    except (OSError, ValueError):
        return base
    if cfg.get("version", 1) < base.get("version", 1):
        # the shop's built-in pictures changed (rooms became showroom displays): keep the owner's film and the
        # pictures they made with AI, and show the new built-in ones after them
        mine = [s for s in cfg.get("scenes", []) if s.get("made") == "ai"]
        cfg = {"version": base["version"], "film": cfg.get("film"), "scenes": (mine + base["scenes"])[:MAX_SCENES]}
    return cfg


def save(cfg: dict) -> None:
    d = home_dir()
    d.mkdir(parents=True, exist_ok=True)
    with _lock:
        tmp = d / "home.tmp"
        tmp.write_text(json.dumps(cfg, indent=1))
        tmp.replace(d / "home.json")


def product_name(sku: str) -> Optional[str]:
    """Tiles and bathroom sets can both carry a dot."""
    p = CATALOG.get(sku) or SANITARY.get(sku)
    return p.name if p else None


def public(cfg: dict) -> dict:
    """What the shop needs: the film and the scenes, each dot with its product's name."""
    scenes = []
    for s in cfg.get("scenes", []):
        spots = [dict(p, name=product_name(p["sku"])) for p in s.get("spots", []) if product_name(p.get("sku", ""))]
        scenes.append({k: s.get(k) for k in ("id", "title", "image", "made")} | {"spots": spots})
    return {"film": cfg.get("film"), "scenes": scenes}


def _scene(cfg: dict, sid: str) -> dict:
    for s in cfg.get("scenes", []):
        if s["id"] == sid:
            return s
    raise HTTPException(status_code=404, detail="No such scene.")


def _err(field: str, message: str, status: int = 422):
    raise HTTPException(status_code=status, detail={"field": field, "message": message})


# ------------------------------------------------------------------ shop
@router.get("/api/shop/home")
def home():
    return public(load())


@router.get("/api/shop/home/files/{name}")
def home_file(name: str):
    path = home_dir() / name
    if not FILE.match(name) or not path.exists():
        raise HTTPException(status_code=404, detail="Not found.")
    media = "video/mp4" if name.endswith(".mp4") else "video/webm" if name.endswith(".webm") else "image/jpeg"
    return FileResponse(path, media_type=media, headers={"Cache-Control": "public, max-age=86400"})


# ------------------------------------------------------------------ admin (owner)
@router.get("/api/admin/homepage")
def admin_home(user=Depends(require("settings.manage"))):
    cfg = load()
    return public(cfg) | {
        "rooms": [{"id": r, "name": n, "photo": f"/images/{room_photo(r).name}", "outdoor": r in OUTDOOR} for r, n in ROOMS.items()],
        "tiles": [{"sku": p.sku, "name": p.name, "surfaces": p.surfaces, "locations": p.locations} for p in CATALOG.values()],
        "ai_on": roomedit.is_on(), "customised": (home_dir() / "home.json").exists()}


class NewScene(BaseModel):
    room: str = Field(max_length=20)
    floor: Optional[str] = Field(default=None, max_length=20)
    wall: Optional[str] = Field(default=None, max_length=20)
    title: str = Field(default="", max_length=40)
    quality: str = Field(default="high", pattern=r"^(medium|high)$")


@router.post("/api/admin/homepage/scenes")
def new_scene(req: NewScene, user=Depends(require("settings.manage"))):
    """A sample room re-drawn by OpenAI as a real photo with the chosen tiles (about 30 seconds to 2 minutes)."""
    floor, wall = req.floor or None, req.wall or None
    if req.room not in ROOMS:
        _err("room", "Pick one of the sample rooms.")
    where = "outdoor" if req.room in OUTDOOR else "indoor"
    if floor and (floor not in CATALOG or "floor" not in CATALOG[floor].surfaces or where not in CATALOG[floor].locations):
        _err("floor", f"Pick a floor tile made for {where} use.")
    if wall and (wall not in CATALOG or "wall" not in CATALOG[wall].surfaces or where not in CATALOG[wall].locations):
        _err("wall", f"Pick a wall tile made for {where} use.")
    if not (floor or wall):
        _err("floor", "Pick a floor tile, a wall tile or both.")
    if not roomedit.is_on():
        _err("_form", "OpenAI is not switched on (OPENAI_API_KEY in Render).", 503)
    cfg = load()
    if len(cfg.get("scenes", [])) >= MAX_SCENES:
        _err("_form", f"At most {MAX_SCENES} scenes. Remove one first.", 409)
    if not roomedit._count_today():
        _err("_form", "Today's AI pictures are used up (OPENAI_DAILY_IMAGES). Try again tomorrow.", 429)
    before, size = roomedit.fit_preset(Image.open(room_photo(req.room)).convert("RGB"))
    prompt = roomedit.build_prompt(floor, wall, "") + (
        "\nThis picture is for the shop's home page: make it look like a professional interior photograph.")
    refs = [roomedit.tile_picture(s) for s in (floor, wall) if s]
    t0 = time.time()
    out, model = roomedit.edit_image(roomedit._jpeg(before, 92), refs, prompt, size, req.quality)
    im = Image.open(io.BytesIO(out)).convert("RGB")
    sid = "s" + secrets.token_hex(5)
    d = home_dir()
    d.mkdir(parents=True, exist_ok=True)
    im.save(d / f"{sid}.jpg", "JPEG", quality=88, optimize=True, progressive=True)
    spots = ([{"sku": floor, "surface": "floor", "x": 50, "y": 86}] if floor else []) + \
            ([{"sku": wall, "surface": "wall", "x": 30, "y": 34}] if wall else [])
    scene = {"id": sid, "title": req.title.strip() or ROOMS[req.room], "image": f"/api/shop/home/files/{sid}.jpg",
             "room": req.room, "made": "ai", "spots": spots}
    cfg.setdefault("scenes", []).insert(0, scene)
    save(cfg)
    log_event("home_scene_made", room=req.room, floor=floor, wall=wall, model=model, seconds=round(time.time() - t0, 1))
    return public(cfg)["scenes"][0]


class Spot(BaseModel):
    sku: str = Field(max_length=20)
    surface: str = Field(pattern=r"^(floor|wall|sample|product)$")
    x: float = Field(ge=0, le=100)
    y: float = Field(ge=0, le=100)


class SceneEdit(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=40)
    spots: Optional[list[Spot]] = Field(default=None, max_length=10)
    move: Optional[int] = Field(default=None, ge=-1, le=1)


@router.patch("/api/admin/homepage/scenes/{sid}")
def edit_scene(sid: str, req: SceneEdit, user=Depends(require("settings.manage"))):
    cfg = load()
    s = _scene(cfg, sid)
    if req.title is not None:
        s["title"] = req.title.strip()
    if req.spots is not None:
        for p in req.spots:
            if not product_name(p.sku):
                _err("spots", "One of the dots points to a product we don't sell.")
        s["spots"] = [p.model_dump() | {"x": round(p.x, 1), "y": round(p.y, 1)} for p in req.spots]
    if req.move:
        sc = cfg["scenes"]
        i = sc.index(s)
        j = max(0, min(len(sc) - 1, i + req.move))
        sc[i], sc[j] = sc[j], sc[i]
    save(cfg)
    return public(cfg)


@router.delete("/api/admin/homepage/scenes/{sid}")
def delete_scene(sid: str, user=Depends(require("settings.manage"))):
    cfg = load()
    s = _scene(cfg, sid)
    if len(cfg["scenes"]) <= 1:
        _err("_form", "Keep at least one scene on the home page.", 409)
    cfg["scenes"].remove(s)
    for img in (s.get("image"), s.get("original")):
        name = (img or "").rsplit("/", 1)[-1]
        if (img or "").startswith("/api/shop/home/files/") and FILE.match(name):
            (home_dir() / name).unlink(missing_ok=True)
    save(cfg)
    return public(cfg)


@router.post("/api/admin/homepage/film")
async def upload_film(request: Request, user=Depends(require("settings.manage"))):
    kind = (request.headers.get("content-type") or "").split(";")[0].strip()
    if kind not in ("video/mp4", "video/webm"):
        _err("film", "Upload an MP4 or WebM video.")
    data = await request.body()
    if not data:
        _err("film", "No video received.")
    if len(data) > MAX_FILM:
        _err("film", "The video is larger than 40 MB. Export it at 1080p and about 10 to 20 seconds long.", 413)
    ok = data[4:8] == b"ftyp" if kind == "video/mp4" else data[:4] == b"\x1a\x45\xdf\xa3"
    if not ok:
        _err("film", "That file is not a video we can play.")
    d = home_dir()
    d.mkdir(parents=True, exist_ok=True)
    name = f"film_{secrets.token_hex(4)}.{'mp4' if kind == 'video/mp4' else 'webm'}"
    (d / name).write_bytes(data)
    cfg = load()
    old = (cfg.get("film") or "").rsplit("/", 1)[-1]
    if old and FILE.match(old):
        (d / old).unlink(missing_ok=True)
    cfg["film"] = f"/api/shop/home/files/{name}"
    save(cfg)
    log_event("home_film_uploaded", mb=round(len(data) / 1e6, 1))
    return public(cfg)


@router.delete("/api/admin/homepage/film")
def delete_film(user=Depends(require("settings.manage"))):
    cfg = load()
    old = (cfg.get("film") or "").rsplit("/", 1)[-1]
    if old and FILE.match(old):
        (home_dir() / old).unlink(missing_ok=True)
    cfg["film"] = None
    save(cfg)
    return public(cfg)


@router.post("/api/admin/homepage/reset")
def reset(user=Depends(require("settings.manage"))):
    """Back to the scenes that ship with the shop (AI pictures and the film are deleted)."""
    d = home_dir()
    if d.exists():
        for p in d.iterdir():
            if FILE.match(p.name) or p.name == "home.json":
                p.unlink(missing_ok=True)
    return public(load())


# ------------------------------------------------------------------ photo-real pictures of the showroom
PHOTO_PROMPT = (
    "The image is a computer-made picture (a 3D render) of a tile and bathroom showroom. Re-draw it as a real "
    "professional photograph of this same showroom, taken from exactly the same spot, angle and framing. "
    "Keep every display, wall, plinth, bath, basin, WC, shower set, plant and light where it is and the same size. "
    "Keep every tile exactly as it is: the same colours, patterns, veining, tile sizes and grout lines, because "
    "customers buy these exact tiles. Only make the light, materials, reflections, soft shadows and small real-life "
    "details look like a real camera photo. Do not add or remove anything, no people, no text, no logos.")


def scene_picture(image: str) -> Path:
    """Where a scene's picture is on disk: shipped with the shop (/images/home/...) or the owner's (files/...)."""
    name = image.rsplit("/", 1)[-1]
    if image.startswith("/images/home/") and re.match(r"^[a-z0-9_]{3,40}\.jpg$", name):
        return visual.ROOT / "images" / "home" / name
    if image.startswith("/api/shop/home/files/") and FILE.match(name):
        return home_dir() / name
    raise HTTPException(status_code=404, detail="This scene has no picture on the server.")


def refit_spots(spots: list, w: int, h: int, size: str) -> list:
    """fit_preset scales the picture to cover the model's size and crops the middle: move the dots the same way."""
    tw, th = map(int, size.split("x"))
    k = max(tw / w, th / h)
    left, top = (w * k - tw) / 2, (h * k - th) / 2
    out = []
    for p in spots:
        x = (p["x"] / 100 * w * k - left) / tw * 100
        y = (p["y"] / 100 * h * k - top) / th * 100
        if 2 <= x <= 98 and 2 <= y <= 98:
            out.append(dict(p, x=round(x, 1), y=round(y, 1)))
    return out


class PhotoReal(BaseModel):
    quality: str = Field(default="high", pattern=r"^(medium|high)$")


@router.post("/api/admin/homepage/scenes/{sid}/photoreal")
def photoreal(sid: str, req: PhotoReal, user=Depends(require("settings.manage"))):
    """Re-draw a scene's render as a real photo (about 30 seconds to 2 minutes). The render is kept for undo."""
    if not roomedit.is_on():
        _err("_form", "OpenAI is not switched on (OPENAI_API_KEY in Render).", 503)
    cfg = load()
    s = _scene(cfg, sid)
    if s.get("original"):
        _err("_form", "This picture is already photo-real. Go back to the render first to make it again.", 409)
    if not roomedit._count_today():
        _err("_form", "Today's AI pictures are used up (OPENAI_DAILY_IMAGES). Try again tomorrow.", 429)
    src = Image.open(scene_picture(s["image"])).convert("RGB")
    before, size = roomedit.fit_preset(src)
    t0 = time.time()
    out, model = roomedit.edit_image(roomedit._jpeg(before, 92), [], PHOTO_PROMPT, size, req.quality)
    im = Image.open(io.BytesIO(out)).convert("RGB")
    if im.size != before.size:
        im = im.resize(before.size, Image.LANCZOS)
    d = home_dir()
    d.mkdir(parents=True, exist_ok=True)
    name = f"{sid}_real_{secrets.token_hex(3)}.jpg"
    im.save(d / name, "JPEG", quality=88, optimize=True, progressive=True)
    s.update(original=s["image"], original_spots=s.get("spots", []), original_made=s.get("made"),
             image=f"/api/shop/home/files/{name}", made="ai-photo", spots=refit_spots(s.get("spots", []), src.width, src.height, size))
    save(cfg)
    log_event("home_scene_photoreal", scene=sid, model=model, seconds=round(time.time() - t0, 1))
    return public(cfg)


@router.post("/api/admin/homepage/scenes/{sid}/render")
def back_to_render(sid: str, user=Depends(require("settings.manage"))):
    """Undo "photo-real": show the render again, with its own dots."""
    cfg = load()
    s = _scene(cfg, sid)
    if not s.get("original"):
        _err("_form", "This picture is not an AI photo.", 409)
    name = s["image"].rsplit("/", 1)[-1]
    if FILE.match(name):
        (home_dir() / name).unlink(missing_ok=True)
    s.update(image=s.pop("original"), spots=s.pop("original_spots", []), made=s.pop("original_made", None) or "render")
    save(cfg)
    return public(cfg)
