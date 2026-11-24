"""AI room makeover: the customer's room photo is re-drawn by OpenAI's image model with new floor or wall tiles.

How it works (the same thing ChatGPT does when you ask it to change a photo):
1. The customer uploads a photo (backend/visual.py stores it without EXIF) and picks our tiles, or types a change
   in their own words ("make the walls sage green").
2. We send OpenAI's image edit endpoint the photo, a picture of each chosen tile (so it copies OUR tile, not any
   marble) and an instruction written from the catalog: tile name, look, finish and real size in cm.
3. The model paints a new version of the photo. It is saved for 30 days so it can be shared, like other previews.

Limits to be honest about: the model re-draws the whole picture, so small things can shift, and tile size and
pattern are close to the catalog, not exact. The visualiser (exact tiles at real size) stays the reference.

Switched on by OPENAI_API_KEY (set in Render or .env by the shop owner). OPENAI_DAILY_IMAGES caps the cost.
"""
from __future__ import annotations

import base64
import io
import os
import threading
import time
from datetime import date
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field
from PIL import Image, UnidentifiedImageError

from backend.catalog import CATALOG
from backend import makeover_plan as mp
from backend.observability import log_event
from backend.ratelimit import RateLimiter
from backend import visual

router = APIRouter(tags=["AI room makeover"])
API = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/") + "/images/edits"
FALLBACK_MODELS = ("gpt-image-2", "gpt-image-1.5")   # if the first is not on the account, the next is tried
SIZES = {"1536x1024": 1.5, "1024x1024": 1.0, "1024x1536": 2 / 3}
COST_USD = {"medium": 0.05, "high": 0.20}            # rough price of one picture, shown to the customer
limiter = RateLimiter(max_requests=int(os.getenv("MAKEOVER_RATE_PER_MIN", "3")), window_seconds=60)
limiter_check = RateLimiter(max_requests=20, window_seconds=60)
_day = {"date": None, "count": 0}
_lock = threading.Lock()
ROOT = visual.ROOT


def is_on() -> bool:
    return bool(os.getenv("OPENAI_API_KEY", "").strip())


def daily_cap() -> int:
    return int(os.getenv("OPENAI_DAILY_IMAGES", "60"))


def models() -> list[str]:
    first = os.getenv("OPENAI_IMAGE_MODEL", "").strip()
    return [first] + [m for m in FALLBACK_MODELS if m != first] if first else list(FALLBACK_MODELS)


def err(field: str, message: str, status: int = 422):
    """Same shape as the admin's form errors, so the page can show the message next to the right field."""
    raise HTTPException(status_code=status, detail={"field": field, "message": message})


# ------------------------------------------------------------------ the instruction
def describe(sku: str) -> str:
    p = CATALOG[sku]
    look = p.style.lower()
    return (f"\"{p.name}\" by Ceramica Nova: {look}-look {p.material} tiles, {p.finish} finish, "
            f"each tile {p.tile_width_cm} x {p.tile_length_cm} cm")


def build_prompt(floor: Optional[str], wall: Optional[str], extras: Optional[list[str]] = None, source: str = "photo") -> str:
    """Written only from our catalogue and the plan (makeover_plan.py). The customer's own words never reach the AI,
    so it can only draw products we sell (plus, if asked, a paint colour or a light change)."""
    parts = ["Edit this room photo (the first image)."]
    if source == "view":
        parts = ["The first image is one view inside a computer-made 3D model of a room, seen from where the customer "
                 "is standing. Re-draw it as a real photograph of that same room from exactly the same spot and angle."]
    n = 2
    if floor:
        parts.append(f"FLOOR: replace the floor covering with the tile shown in image {n}, {describe(floor)}. "
                     "Lay them in straight rows with thin grout lines, following the floor's perspective, at true "
                     "size for this room (judge the scale from the furniture and doors). Rugs stay on top of the new floor.")
        n += 1
    else:
        parts.append("FLOOR: leave the floor exactly as it is.")
    if wall:
        parts.append(f"WALLS: cover the main painted walls with the tile shown in image {n}, {describe(wall)}, "
                     "at true size, with thin grout lines. Windows, doors, frames, TV, shelves and switches stay in front.")
    elif not any(e.startswith("WALL PAINT") for e in extras or []):
        parts.append("WALLS: leave the walls exactly as they are.")
    parts += list(extras or [])
    parts.append("Keep everything else exactly as it is: furniture, rugs, objects, plants, windows, doors, ceiling, "
                 "camera angle and framing. Do not add anything new: no lamps or wall lights, panels, cladding, "
                 "wallpaper, furniture or decorations. Keep the room's own light, and add the natural reflections and soft "
                 "shadows the new surface would have. Photorealistic, like a real photo of the same room.")
    return "\n".join(parts)


# ------------------------------------------------------------------ pictures
def fit_preset(im: Image.Image) -> tuple[Image.Image, str]:
    """Crop the photo to the nearest size the model makes (3:2, 1:1 or 2:3), so before and after line up exactly."""
    size = min(SIZES, key=lambda s: abs(SIZES[s] - im.width / im.height))
    w, h = map(int, size.split("x"))
    k = max(w / im.width, h / im.height)
    im = im.resize((round(im.width * k), round(im.height * k)), Image.LANCZOS)
    l, t = (im.width - w) // 2, (im.height - h) // 2
    return im.crop((l, t, l + w, t + h)), size


def _jpeg(im: Image.Image, q: int = 90) -> bytes:
    b = io.BytesIO()
    im.convert("RGB").save(b, "JPEG", quality=q)
    return b.getvalue()


def tile_picture(sku: str) -> bytes:
    path = ROOT / "images" / f"{sku}.jpg"
    im = Image.open(path)
    im.thumbnail((768, 768))
    return _jpeg(im)


# ------------------------------------------------------------------ OpenAI
def _post(files: list, data: dict, timeout: float) -> tuple[int, dict]:
    """The one network call (tests replace it)."""
    import httpx
    r = httpx.post(API, files=files, data=data, timeout=httpx.Timeout(timeout, connect=20),
                   headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY'].strip()}"})
    try:
        body = r.json()
    except ValueError:
        body = {}
    return r.status_code, body


def _problem(status: int, body: dict) -> str:
    msg = ((body or {}).get("error") or {}).get("message", "")
    if status == 401:
        return "The OpenAI key is not valid. The shop owner needs to check OPENAI_API_KEY."
    if status == 429:
        return "OpenAI is busy or the account has no credit left. Please try again in a minute."
    if status == 400 and ("safety" in msg.lower() or "moderation" in msg.lower()):
        return "OpenAI would not edit this photo. Please try another photo."
    return "OpenAI could not edit this photo right now." + (f" ({msg[:160]})" if msg else "")


def edit_image(photo: bytes, refs: list[bytes], prompt: str, size: str, quality: str) -> tuple[bytes, str]:
    files = [("image[]", ("room.jpg", photo, "image/jpeg"))]
    files += [("image[]", (f"tile{i + 1}.jpg", b, "image/jpeg")) for i, b in enumerate(refs)]
    last = (502, {})
    for model in models():
        data = {"model": model, "prompt": prompt, "size": size, "quality": quality, "n": "1", "output_format": "jpeg"}
        try:
            status, body = _post(files, data, timeout=240)
        except Exception as e:                                  # network trouble, timeout
            log_event("makeover_error", error=type(e).__name__)
            err("general", "OpenAI did not answer in time. Please try again.", 504)
        if status == 200 and body.get("data"):
            return base64.b64decode(body["data"][0]["b64_json"]), model
        last = (status, body)
        msg = str(((body or {}).get("error") or {}).get("message", "")).lower()
        if not (status in (400, 403, 404) and "model" in msg):   # only a missing model is worth another try
            break
    log_event("makeover_error", status=last[0])
    err("general", _problem(*last), 502)


def _count_today() -> bool:
    with _lock:
        today = date.today().isoformat()
        if _day["date"] != today:
            _day.update(date=today, count=0)
        if _day["count"] >= daily_cap():
            return False
        _day["count"] += 1
        return True


# ------------------------------------------------------------------ API
@router.get("/api/v1/makeover")
def status():
    """Whether AI makeovers are switched on, and the tiles that can be used."""
    tiles = [{"sku": p.sku, "name": p.name, "surfaces": p.surfaces, "style": p.style, "finish": p.finish,
              "size_cm": f"{p.tile_width_cm}x{p.tile_length_cm}", "image": f"/images/{p.sku}.jpg",
              "price": p.price_per_m2, "currency": p.currency}
             for p in CATALOG.values() if (ROOT / "images" / f"{p.sku}.jpg").exists()]
    return {"on": is_on(), "tiles": tiles, "cost_usd": COST_USD}


class MakeoverRequest(BaseModel):
    photo_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    floor: Optional[str] = Field(default=None, max_length=20)
    wall: Optional[str] = Field(default=None, max_length=20)
    request: str = Field(default="", max_length=300)
    quality: str = Field(default="medium", pattern=r"^(medium|high)$")
    source: str = Field(default="photo", pattern=r"^(photo|view)$")   # view = a snapshot from a 360° / 3D room


@router.post("/api/v1/makeover")
def makeover(req: MakeoverRequest, request: Request, x_user_id: Optional[str] = Header(default=None)):
    if not x_user_id or not visual.VISITOR.match(x_user_id):
        raise HTTPException(status_code=400, detail="Missing visitor id.")
    floor, wall, text = (req.floor or None), (req.wall or None), " ".join(req.request.split())
    if floor and (floor not in CATALOG or "floor" not in CATALOG[floor].surfaces):
        err("floor", "Pick one of our floor tiles.")
    if wall and (wall not in CATALOG or "wall" not in CATALOG[wall].surfaces):
        err("wall", "Pick one of our wall tiles.")
    if text and len(text) < 3:
        err("request", "Tell us a little more, for example \"make the walls light grey\".")
    if not (floor or wall or text):
        err("request", "Pick a floor or wall tile, or write what you'd like to change.")
    if not is_on():
        err("general", "AI makeovers are not switched on yet (the shop owner adds an OpenAI key).", 503)
    plan = mp.make_plan(text, floor, wall)          # the words become our products (or are left out)
    if plan.empty:
        err("request", nothing_to_draw(plan))
    floor, wall, extras = plan.floor, plan.wall, mp.instructions(plan)
    path = visual._own_photo(req.photo_id, x_user_id)
    if not limiter.allow(x_user_id):
        err("general", "Please wait a minute before the next makeover.", 429)
    if not _count_today():
        err("general", "Today's AI makeovers are used up. Please try again tomorrow.", 429)

    try:
        before, size = fit_preset(Image.open(path).convert("RGB"))
    except (UnidentifiedImageError, OSError):
        err("photo", "This photo can't be read. Please upload it again.")
    prompt = build_prompt(floor, wall, extras, req.source)
    refs = [tile_picture(s) for s in (floor, wall) if s]
    t0 = time.time()
    out, model = edit_image(_jpeg(before, 92), refs, prompt, size, req.quality)
    seconds = round(time.time() - t0, 1)
    try:
        after = Image.open(io.BytesIO(out)).convert("RGB")
    except (UnidentifiedImageError, OSError):
        err("general", "OpenAI sent back a picture we can't read. Please try again.", 502)
    if after.size != before.size:
        after = after.resize(before.size, Image.LANCZOS)

    names = [f"{CATALOG[floor].name} floor" if floor else "", f"{CATALOG[wall].name} walls" if wall else "",
             f"{plan.paint} paint" if plan.paint else "", mp.LIGHT_WORDS[plan.light] if plan.light else ""]
    tiles = " and ".join(n for n in names if n)
    b = visual.save_image(_jpeg(before), x_user_id, "render", {"room": "AI makeover (before)", "tiles": ""})
    a = visual.save_image(_jpeg(after), x_user_id, "render", {"room": "AI makeover", "tiles": tiles,
                                                              "model": model, "seconds": seconds})
    log_event("makeover_done", model=model, seconds=seconds, quality=req.quality, floor=floor, wall=wall)
    return {"before": b["url"], "after": a["url"], "share_url": f"{visual._public_base(request)}{a['url']}",
            "tiles": tiles, "model": model, "width": before.width, "height": before.height, "seconds": seconds, "quality": req.quality,
            "cost_usd": COST_USD[req.quality], "prompt": prompt,
            "products": [used(s, where) for s, where in ((floor, "floor"), (wall, "walls")) if s],
            "notes": [l.model_dump() for l in plan.lines]}


def used(sku: str, where: str) -> dict:
    p = CATALOG[sku]
    return {"sku": sku, "name": p.name, "where": where, "price": p.price_per_m2, "currency": p.currency,
            "image": f"/images/{sku}.jpg", "url": f"/#/p/{sku}"}


def nothing_to_draw(plan: "mp.Plan") -> str:
    left = [l for l in plan.lines if l.kind in ("missing", "left_out")]
    return (left[0].text.split(" Pick one")[0] + " " if left else "") + "Pick one of our floor or wall tiles above, or ask for a wall colour or brighter light."


class CheckRequest(BaseModel):
    floor: Optional[str] = Field(default=None, max_length=20)
    wall: Optional[str] = Field(default=None, max_length=20)
    request: str = Field(default="", max_length=300)


@router.post("/api/v1/makeover/check")
def check(req: CheckRequest, x_user_id: Optional[str] = Header(default=None)):
    """Before drawing: what the AI will put in the room, what was swapped for our closest product, and what is
    left out because we don't sell it. The page shows this next to the customer's words."""
    if not x_user_id or not visual.VISITOR.match(x_user_id):
        raise HTTPException(status_code=400, detail="Missing visitor id.")
    floor, wall, text = (req.floor or None), (req.wall or None), " ".join(req.request.split())
    if floor and not mp.fits(floor, "floor"):
        err("floor", "Pick one of our floor tiles.")
    if wall and not mp.fits(wall, "wall"):
        err("wall", "Pick one of our wall tiles.")
    if text and not limiter_check.allow(x_user_id):
        err("request", "Please wait a moment before checking again.", 429)
    plan = mp.make_plan(text, floor, wall)
    return {"floor": plan.floor, "wall": plan.wall, "paint": plan.paint, "light": plan.light,
            "needs_ok": plan.needs_ok, "empty": plan.empty, "lines": [l.model_dump() for l in plan.lines],
            "message": nothing_to_draw(plan) if plan.empty else ""}
