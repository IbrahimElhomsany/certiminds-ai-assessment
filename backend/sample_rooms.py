"""The owner's own sample rooms: upload a room photo in Admin, the room AI marks floor and walls, the owner
checks and fixes it, and publishes it. Published rooms appear for every customer next to the built-in ones.

Stored next to the database (same persistent disk): <dir>/<id>.jpg, <id>_t.jpg (small picture) and
<id>.json (name, status, the room AI's answer and the owner's fixes). Only the owner role can change them.

Status: analysing -> ready (or failed: "Try again"). Only ready rooms can be published.
"""
from __future__ import annotations

import io
import json
import os
import re
import secrets
import threading
import time
import zipfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, Field

from backend import visual
from backend.auth import require
from backend.observability import log_event

router = APIRouter(tags=["Sample rooms"])
RID = re.compile(r"^r[a-f0-9]{12}$")
MAX_ROOMS = 40
_lock = threading.Lock()


def rooms_dir() -> Path:
    return Path(os.getenv("SAMPLE_ROOMS_DIR") or visual.STORE.parent / "sample-rooms")


def _path(rid: str, suffix: str) -> Path:
    if not RID.match(rid or ""):
        raise HTTPException(status_code=404, detail="No such room.")
    return rooms_dir() / f"{rid}{suffix}"


def _read(rid: str) -> dict:
    p = _path(rid, ".json")
    if not p.exists():
        raise HTTPException(status_code=404, detail="No such room.")
    return json.loads(p.read_text())


def _write(meta: dict) -> None:
    with _lock:
        p = _path(meta["id"], ".json")
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta))
        tmp.replace(p)


def _all() -> list[dict]:
    d = rooms_dir()
    if not d.exists():
        return []
    out = []
    for p in d.glob("r*.json"):
        try:
            out.append(json.loads(p.read_text()))
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda m: m.get("created", 0))


def _summary(m: dict) -> dict:
    """What the admin list needs (no masks: they are big)."""
    a = m.get("analysis") or {}
    kinds = [s["kind"] for s in a.get("surfaces", [])]
    return {k: m.get(k) for k in ("id", "name", "location", "status", "error", "published", "created", "width", "height")} | {
        "photo": f"/api/rooms/{m['id']}.jpg", "thumb": f"/api/rooms/{m['id']}_t.jpg",
        "floors": kinds.count("floor"), "walls": kinds.count("wall"),
        "fixed": bool((m.get("edits") or {}).get("remove") or (m.get("edits") or {}).get("add"))}


def visualiser_room(m: dict) -> dict:
    """A published room in the same shape as frontend/assets/rooms.js (with AI cut-outs instead of outlines)."""
    surfaces: dict[str, list] = {}
    for s in (m.get("analysis") or {}).get("surfaces", []):
        surfaces.setdefault(s["kind"], []).append({"quad": s["quad"], "size": s["size"], "mask": s["mask"], "keep": []})
    return {"id": m["id"], "name": m["name"], "location": m.get("location", "indoor"), "owner": True,
            "photo": f"/api/rooms/{m['id']}.jpg?v={int(m.get('updated', m['created']))}",
            "thumb": f"/api/rooms/{m['id']}_t.jpg", "width": m["width"], "height": m["height"],
            "surfaces": surfaces, "baseEdits": m.get("edits") or None}


def _analyse(rid: str) -> None:
    """Runs in the background: ask the room AI, save the answer."""
    try:
        data = visual.clean_analysis(visual.ask_room_ai("analyse", _path(rid, ".jpg").read_bytes()))
        meta = _read(rid)
        if not data["surfaces"]:
            meta.update(status="failed", error="The room AI found no floor or wall in this photo.")
        else:
            meta.update(status="ready", error=None, analysis=data, edits=None)
    except HTTPException as e:
        meta = _read(rid)
        meta.update(status="failed", error=str(e.detail))
    except Exception as e:                                # never leave a room stuck on "analysing"
        meta = _read(rid)
        meta.update(status="failed", error=f"Unexpected error ({type(e).__name__}).")
    meta["updated"] = time.time()
    _write(meta)
    log_event("sample_room_analysed", room=rid, status=meta["status"])


def _start(rid: str) -> None:
    threading.Thread(target=_analyse, args=(rid,), daemon=True).start()


# ------------------------------------------------------------------ admin (owner only)
@router.get("/api/admin/rooms")
def list_rooms(user=Depends(require("settings.manage"))):
    stuck = time.time() - 600
    for m in _all():                                       # a restart in the middle of an analysis
        if m.get("status") == "analysing" and m.get("updated", 0) < stuck:
            m.update(status="failed", error="The analysis was interrupted. Try again.")
            _write(m)
    return {"rooms": [_summary(m) for m in _all()], "room_ai": visual.room_ai_on(),
            "training_fixes": visual.training_count()}


@router.post("/api/admin/rooms")
async def add_room(request: Request, name: str = Query(..., min_length=2, max_length=40),
                   location: str = Query("indoor", pattern="^(indoor|outdoor)$"),
                   user=Depends(require("settings.manage"))):
    if not visual.room_ai_on():
        raise HTTPException(status_code=503, detail="The room AI is not switched on (ROOMAI_URL and ROOMAI_TOKEN in Render).")
    if len(_all()) >= MAX_ROOMS:
        raise HTTPException(status_code=409, detail=f"At most {MAX_ROOMS} sample rooms. Delete one first.")
    data = await request.body()
    if not data or len(data) > visual.MAX_BYTES:
        raise HTTPException(status_code=413, detail="Send one photo, at most 12 MB.")
    try:
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise HTTPException(status_code=422, detail="That file is not a photo we can read (use JPG, PNG or WebP).")
    if min(im.size) < 480:
        raise HTTPException(status_code=422, detail="That photo is too small. Use one at least 1000 pixels wide.")
    if im.width >= 1.9 * im.height:
        raise HTTPException(status_code=422, detail="360° photos can't be sample rooms here yet. Use a normal photo.")
    im.thumbnail((2000, 2000), Image.LANCZOS)
    rooms_dir().mkdir(parents=True, exist_ok=True)
    rid = "r" + secrets.token_hex(6)
    im.save(_path(rid, ".jpg"), "JPEG", quality=88, optimize=True)
    t = im.copy()
    t.thumbnail((640, 640), Image.LANCZOS)
    t.save(_path(rid, "_t.jpg"), "JPEG", quality=82)
    now = time.time()
    meta = {"id": rid, "name": name.strip(), "location": location, "status": "analysing", "error": None,
            "published": False, "created": now, "updated": now, "width": im.width, "height": im.height,
            "by": getattr(user, "email", None), "analysis": None, "edits": None}
    _write(meta)
    _start(rid)
    log_event("sample_room_added", room=rid)
    return _summary(meta)


@router.get("/api/admin/rooms/{rid}")
def get_room(rid: str, user=Depends(require("settings.manage"))):
    m = _read(rid)
    return _summary(m) | {"room": visualiser_room(m) if m.get("analysis") else None, "room_ai": visual.room_ai_on()}


@router.post("/api/admin/rooms/{rid}/analyse")
def reanalyse(rid: str, user=Depends(require("settings.manage"))):
    m = _read(rid)
    if m["status"] == "analysing":
        raise HTTPException(status_code=409, detail="Already being analysed.")
    if not visual.room_ai_on():
        raise HTTPException(status_code=503, detail="The room AI is not switched on.")
    m.update(status="analysing", error=None, published=False, updated=time.time())
    _write(m)
    _start(rid)
    return _summary(m)


class RoomEdits(BaseModel):
    remove: Optional[str] = Field(default=None, max_length=4_000_000)
    add: dict[str, str] = Field(default_factory=dict)


class RoomUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=40)
    location: Optional[str] = Field(default=None, pattern="^(indoor|outdoor)$")
    published: Optional[bool] = None
    edits: Optional[RoomEdits] = None
    clear_edits: bool = False


@router.patch("/api/admin/rooms/{rid}")
def update_room(rid: str, req: RoomUpdate, user=Depends(require("settings.manage"))):
    m = _read(rid)
    if req.name is not None:
        m["name"] = req.name.strip()
    if req.location is not None:
        m["location"] = req.location
    if req.clear_edits:
        m["edits"] = None
    if req.edits is not None:
        add = {k: v for k, v in req.edits.add.items() if k in ("floor", "wall")}
        for v in [req.edits.remove, *add.values()]:
            if v is not None and (not v.startswith("data:image/png;base64,") or len(v) > 4_000_000):
                raise HTTPException(status_code=422, detail="Fixes must be PNG pictures.")
        m["edits"] = {"remove": req.edits.remove, "add": add} if (req.edits.remove or add) else None
    if req.published is not None:
        if req.published and m["status"] != "ready":
            raise HTTPException(status_code=409, detail="Only a room the AI has finished can be published.")
        m["published"] = req.published
    m["updated"] = time.time()
    _write(m)
    log_event("sample_room_updated", room=rid, published=m["published"])
    return _summary(m)


@router.delete("/api/admin/rooms/{rid}")
def delete_room(rid: str, user=Depends(require("settings.manage"))):
    _read(rid)
    for suffix in (".json", ".jpg", "_t.jpg"):
        _path(rid, suffix).unlink(missing_ok=True)
    log_event("sample_room_deleted", room=rid)
    return {"ok": True}


@router.post("/api/admin/rooms/{rid}/tap")
def tap_room(rid: str, req: visual.TapRequest, user=Depends(require("settings.manage"))):
    m = _read(rid)
    return visual.ask_room_ai("tap", _path(rid, ".jpg").read_bytes(),
                              {"points": visual.tap_points(req.points, (m["width"], m["height"]))}, timeout=90)


@router.get("/api/admin/roomai/training.zip")
def training_zip(user=Depends(require("settings.manage"))):
    """Everything customers agreed to share: photo, the AI's answer and their fix, one folder per photo."""
    d = visual.training_dir()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if d.exists():
            for f in sorted(d.rglob("*")):
                if f.is_file():
                    z.write(f, f.relative_to(d).as_posix())
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="room-ai-fixes.zip"'})


# ------------------------------------------------------------------ public
@router.get("/api/rooms")
def published_rooms():
    return [visualiser_room(m) for m in _all() if m.get("published") and m.get("status") == "ready"]


@router.get("/api/rooms/{name}")
def room_photo(name: str):
    rid, _, ext = name.partition(".")
    small = rid.endswith("_t")
    rid = rid.removesuffix("_t")
    if ext != "jpg":
        raise HTTPException(status_code=404, detail="No such picture.")
    _read(rid)                     # drafts too: the admin page shows them (ids are random and unguessable)
    p = _path(rid, "_t.jpg" if small else ".jpg")
    if not p.exists():
        raise HTTPException(status_code=404, detail="No such picture.")
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=3600"})
