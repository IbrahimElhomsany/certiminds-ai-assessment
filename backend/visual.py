"""Room visualiser: the customer's own room photos, the phone hand-off (QR code) and saved room previews.

Privacy by design:
* A photo is re-encoded on arrival (this drops EXIF data such as GPS position and phone model) and
  stored under a random id. Room photos are deleted after PHOTO_DAYS, saved previews after RENDER_DAYS.
* The QR code holds a one-time link that expires after HANDOFF_MINUTES. Only the screen that made the
  link can collect the photo taken with it.
* Nothing here needs an account; the visitor id (USR-...) is the owner, like quotations.
"""
import io
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from PIL import Image, ImageOps, UnidentifiedImageError

from backend.observability import log_event
from backend.ratelimit import RateLimiter

ROOT = Path(__file__).resolve().parent.parent
def _store_dir() -> Path:
    """Next to the database, so photos sit on the same persistent disk (Render: /var/data)."""
    if os.getenv("UPLOAD_DIR"):
        return Path(os.environ["UPLOAD_DIR"])
    db = os.getenv("DATABASE_URL", "")
    if db.startswith("sqlite:///") and db not in ("sqlite:///:memory:",):
        return Path(db.removeprefix("sqlite:///")).parent / "uploads"
    return ROOT / "data" / "uploads"


STORE = _store_dir()
PHOTO_DAYS, RENDER_DAYS, HANDOFF_MINUTES = 7, 30, 15
MAX_BYTES = 12 * 1024 * 1024          # a phone photo is 2-6 MB; anything bigger is refused
MAX_SIDE = 2000                       # stored size: enough for the visualiser, small on disk
MAX_PANO_SIDE = 4096                  # a 360° photo is seen from up close, so it keeps more pixels
VISITOR = re.compile(r"^USR-\d{3,10}$")
FILE_ID = re.compile(r"^[a-f0-9]{32}$")
CODE = re.compile(r"^[A-Za-z0-9_-]{16,40}$")

router = APIRouter(tags=["Room visualiser"])
limiter = RateLimiter(max_requests=int(os.getenv("VISUAL_RATE_PER_MIN", "12")), window_seconds=60)
_handoffs: dict[str, dict] = {}       # code -> {owner, expires, photo}; short-lived, so kept in memory
_lock = threading.Lock()


def _visitor(x_user_id: Optional[str]) -> str:
    if not x_user_id or not VISITOR.match(x_user_id):
        raise HTTPException(status_code=400, detail="Missing visitor id.")
    if not limiter.allow(x_user_id):
        raise HTTPException(status_code=429, detail="Too many uploads, please wait a minute.")
    return x_user_id


def _clean_old() -> int:
    """Delete room photos older than PHOTO_DAYS and previews older than RENDER_DAYS."""
    removed, now = 0, time.time()
    if not STORE.exists():
        return 0
    for meta in STORE.glob("*.json"):
        try:
            m = json.loads(meta.read_text())
            days = RENDER_DAYS if m.get("kind") == "render" else PHOTO_DAYS
            if now - m.get("created", 0) > days * 86400:
                meta.with_suffix(".jpg").unlink(missing_ok=True)
                meta.unlink(missing_ok=True)
                removed += 1
        except Exception:
            continue
    if removed:
        log_event("visual_cleanup", removed=removed)
    return removed


def save_image(data: bytes, owner: str, kind: str, extra: Optional[dict] = None) -> dict:
    """Check that the bytes really are an image, re-encode them as JPEG (no EXIF) and store them."""
    if not data:
        raise HTTPException(status_code=422, detail="No image received.")
    if len(data) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="That photo is too large (max 12 MB).")
    try:
        im = Image.open(io.BytesIO(data))
        im.draft("RGB", (MAX_PANO_SIDE, MAX_PANO_SIDE))
        im = ImageOps.exif_transpose(im).convert("RGB")      # phones store rotation in EXIF
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise HTTPException(status_code=422, detail="That file is not a photo we can read (use JPG, PNG or WebP).")
    if min(im.size) < 320:
        raise HTTPException(status_code=422, detail="That photo is too small. Please use one at least 640 pixels wide.")
    side = MAX_PANO_SIDE if im.width >= 1.9 * im.height else MAX_SIDE     # 360° photos keep more detail
    im.thumbnail((side, side), Image.LANCZOS)
    STORE.mkdir(parents=True, exist_ok=True)
    fid = secrets.token_hex(16)
    im.save(STORE / f"{fid}.jpg", "JPEG", quality=86, optimize=True)
    (STORE / f"{fid}.json").write_text(json.dumps({"owner": owner, "kind": kind, "created": time.time(), **(extra or {})}))
    _clean_old()
    return {"id": fid, "url": f"/api/v1/visual/files/{fid}.jpg", "width": im.width, "height": im.height}


def _public_base(request: Request) -> str:
    """The site's address as the phone will see it (Render puts us behind an https proxy)."""
    proto = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc))
    return f"{proto}://{host}"


# ------------------------------------------------------------------ photos
@router.post("/api/v1/visual/photos")
async def upload_photo(request: Request, x_user_id: Optional[str] = Header(default=None)):
    owner = _visitor(x_user_id)
    return save_image(await request.body(), owner, "photo")


@router.get("/api/v1/visual/files/{name}")
def get_file(name: str):
    fid = name.removesuffix(".jpg")
    path = STORE / f"{fid}.jpg"
    if not FILE_ID.match(fid) or not path.exists():
        raise HTTPException(status_code=404, detail="This image has expired or does not exist.")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400",
                                                                "X-Robots-Tag": "noindex"})


# ------------------------------------------------------------------ phone hand-off (QR code)
def _handoff(code: str) -> dict:
    with _lock:
        for c in [c for c, h in _handoffs.items() if h["expires"] < time.time() - 3600]:
            _handoffs.pop(c, None)                       # forget long-expired codes
        h = _handoffs.get(code) if CODE.match(code or "") else None
    if h is None:
        raise HTTPException(status_code=404, detail="This link is not valid. Scan the QR code again.")
    return h


@router.post("/api/v1/visual/handoff")
def new_handoff(request: Request, x_user_id: Optional[str] = Header(default=None)):
    owner = _visitor(x_user_id)
    code = secrets.token_urlsafe(18)
    with _lock:
        _handoffs[code] = {"owner": owner, "expires": time.time() + HANDOFF_MINUTES * 60, "photo": None}
    return {"code": code, "url": f"{_public_base(request)}/m/{code}", "qr": f"/api/v1/visual/handoff/{code}/qr.svg",
            "expires_in": HANDOFF_MINUTES * 60}


@router.get("/m/{code}", include_in_schema=False)
def phone_page(code: str):
    """The page the phone opens from the QR code: take a photo, send it to the showroom screen."""
    return FileResponse(ROOT / "frontend" / "mobile.html", headers={"Cache-Control": "no-store"})


@router.get("/api/v1/visual/handoff/{code}/qr.svg")
def handoff_qr(code: str, request: Request):
    _handoff(code)
    import segno
    buf = io.BytesIO()
    segno.make(f"{_public_base(request)}/m/{code}", error="m").save(buf, kind="svg", scale=6, border=2, dark="#111111")
    return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "no-store"})


@router.get("/api/v1/visual/handoff/{code}")
def handoff_status(code: str, x_user_id: Optional[str] = Header(default=None)):
    h = _handoff(code)
    if h["owner"] != x_user_id:
        raise HTTPException(status_code=403, detail="This link belongs to another screen.")
    if h["photo"]:
        return {"status": "done", "photo": h["photo"]}
    return {"status": "expired" if h["expires"] < time.time() else "waiting",
            "seconds_left": max(0, int(h["expires"] - time.time()))}


@router.post("/api/v1/visual/handoff/{code}/photo")
async def handoff_upload(code: str, request: Request):
    h = _handoff(code)
    if h["photo"]:
        raise HTTPException(status_code=409, detail="A photo was already sent with this link.")
    if h["expires"] < time.time():
        raise HTTPException(status_code=410, detail="This link has expired. Scan the new QR code on the screen.")
    if not limiter.allow("handoff:" + code):
        raise HTTPException(status_code=429, detail="Too many tries, please wait a minute.")
    photo = save_image(await request.body(), h["owner"], "photo", {"via": "phone"})
    with _lock:
        if h["photo"]:                                   # two uploads raced: the first one wins
            raise HTTPException(status_code=409, detail="A photo was already sent with this link.")
        h["photo"] = photo
    return {"ok": True}


# ------------------------------------------------------------------ saved previews (to share and for the salesperson)
@router.post("/api/v1/visual/renders")
async def save_render(request: Request, x_user_id: Optional[str] = Header(default=None),
                      session_id: Optional[str] = Query(default=None, pattern=r"^[a-zA-Z0-9_-]{4,64}$"),
                      room: str = Query(default="", max_length=60), tiles: str = Query(default="", max_length=200)):
    owner = _visitor(x_user_id)
    clean = lambda s: re.sub(r"[^\w ,.:&()/-]", "", s)[:200]
    saved = save_image(await request.body(), owner, "render", {"room": clean(room), "tiles": clean(tiles)})
    saved["share_url"] = f"{_public_base(request)}{saved['url']}"
    saved["lead"] = None
    if session_id:                                       # put it on the customer's lead for the salesperson
        from backend import crm_bridge
        from backend.store import AccessDenied, get_or_create_session
        try:
            session = get_or_create_session(session_id, owner)
        except AccessDenied:
            raise HTTPException(status_code=403, detail="This session belongs to another user.")
        saved["lead"] = crm_bridge.save_room_preview(session, saved["url"], clean(room), clean(tiles))
    return saved
