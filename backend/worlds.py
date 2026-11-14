"""Walkable 3D rooms made by World Labs Marble from one of our own 360° pictures.

World Labs turns a 360° picture into a 3D "world" made of Gaussian splats (millions of small coloured blobs).
We ask for each sample room ONCE (it costs about 1,500 of the account's credits and takes about 5 minutes),
download the result to our own disk and serve it from there, so customers never wait for World Labs and
nothing is paid again. The browser shows it with Spark (MIT, frontend/assets/vendor/spark.module.js) and lays
the chosen floor tile over the floor.

Only the owner can start a generation (Depends(require("settings.manage"))), it never runs on its own, and a
room that is already made or in progress is never asked for again. The key is read from the WORLDLABS_API_KEY
environment variable (Render or the local .env), never from the repo.
API: https://docs.worldlabs.ai/api (POST /marble/v1/worlds:generate, GET /marble/v1/operations/{id}).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from backend.auth import require
from backend.observability import log_event
from backend.visual import STORE

API = "https://api.worldlabs.ai/marble/v1"
MODEL = "marble-1.1"
SPLAT_SIZE = "500k"                 # 500k splats (~ a few MB): sharp enough indoors, fine on a phone
POLL_EVERY = 20                     # seconds between checks while a room is being made

# Our sample 360° rooms that may be turned into a walkable world (key -> the picture it is made from).
SAMPLES = {"tvroom": {"pano": "/images/pano_tvroom.jpg", "name": "TV lounge"}}

router = APIRouter()
_lock = threading.Lock()
KEY = re.compile(r"^[a-z0-9_]{1,32}$")


def _dir() -> Path:
    d = STORE.parent / "worlds"     # same persistent disk as the database (Render: /var/data/worlds)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _meta_path(key: str) -> Path:
    return _dir() / f"{key}.json"


def _read(key: str) -> dict:
    p = _meta_path(key)
    return json.loads(p.read_text()) if p.exists() else {}


def _write(key: str, meta: dict) -> None:
    tmp = _meta_path(key).with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, indent=1))
    tmp.replace(_meta_path(key))


def _api_key() -> str:
    k = os.getenv("WORLDLABS_API_KEY", "").strip()
    if not k:
        raise HTTPException(status_code=503, detail="World Labs is not set up (WORLDLABS_API_KEY is missing).")
    return k


def _client() -> httpx.Client:
    return httpx.Client(timeout=60, headers={"WLT-Api-Key": _api_key(), "Content-Type": "application/json"})


def _public(meta: dict, key: str) -> dict:
    """What the browser may see: never the operation id's internals or the key."""
    out = {"key": key, "name": SAMPLES[key]["name"], "status": meta.get("status", "none")}
    if meta.get("status") == "ready":
        out.update(url=f"/worlds/{key}.spz", scale=meta.get("scale", 1.0), offset=meta.get("offset", 0.0),
                   caption=meta.get("caption", ""), credits=meta.get("cost"))
    if meta.get("status") == "failed":
        out["error"] = meta.get("error", "World Labs could not make this room.")
    return out


def _poll(key: str, meta: dict) -> dict:
    """Ask World Labs how a room is getting on; when it is done, download the splats to our disk."""
    if meta.get("status") != "pending" or time.time() - meta.get("checked", 0) < POLL_EVERY:
        return meta
    meta["checked"] = time.time()
    try:
        with _client() as c:
            r = c.get(f"{API}/operations/{meta['operation_id']}")
            r.raise_for_status()
            op = r.json()
            if not op.get("done"):
                meta["progress"] = (op.get("metadata") or {}).get("progress")
                return meta
            if op.get("error"):
                meta.update(status="failed", error=str((op["error"] or {}).get("message", "failed"))[:300])
                return meta
            world = op.get("response") or {}
            if not (world.get("assets") or {}).get("splats"):          # the snapshot can be thin: fetch the world
                w = c.get(f"{API}/worlds/{world['id']}")
                w.raise_for_status()
                world = w.json().get("world", w.json())
            assets = world.get("assets") or {}
            splats = assets.get("splats") or {}
            urls = splats.get("spz_urls") or {}
            url = urls.get(SPLAT_SIZE) or urls.get("100k") or urls.get("full_res")
            if not url:
                meta.update(status="failed", error="World Labs returned no splat file.")
                return meta
            sem = splats.get("semantics_metadata") or {}
            tmp = _dir() / f"{key}.spz.tmp"
            for headers in ({}, {"WLT-Api-Key": _api_key()}):              # a signed link, or one that needs the key
                with httpx.stream("GET", url, timeout=300, follow_redirects=True, headers=headers) as dl:
                    if dl.status_code in (401, 403) and not headers:
                        continue
                    dl.raise_for_status()
                    with open(tmp, "wb") as f:
                        for chunk in dl.iter_bytes(1 << 16):
                            f.write(chunk)
                break
            tmp.replace(_dir() / f"{key}.spz")
            meta.update(status="ready", world_id=world.get("id"), marble_url=world.get("world_marble_url"),
                        scale=float(sem.get("metric_scale_factor") or 1.0), offset=float(sem.get("ground_plane_offset") or 0.0),
                        caption=(assets.get("caption") or "")[:400], cost=op.get("cost"), ready_at=time.time())
            log_event("world_ready", key=key, world=world.get("id"))
    except httpx.HTTPError as e:                         # try again on the next check
        meta["last_error"] = type(e).__name__
        log_event("world_poll_failed", key=key, error=type(e).__name__)
    return meta


def _check(key: str) -> dict:
    if not KEY.match(key) or key not in SAMPLES:
        raise HTTPException(status_code=404, detail="No such room.")
    with _lock:
        meta = _read(key)
        before = json.dumps(meta, sort_keys=True)
        meta = _poll(key, meta)
        if json.dumps(meta, sort_keys=True) != before:
            _write(key, meta)
    return meta


@router.get("/api/worlds/{key}")
def world_status(key: str) -> dict:
    """Public: is the walkable version of this sample room ready? (Checks on World Labs while it is being made.)"""
    return _public(_check(key), key)


@router.get("/worlds/{key}.spz")
def world_file(key: str):
    meta = _check(key)
    f = _dir() / f"{key}.spz"
    if meta.get("status") != "ready" or not f.exists():
        raise HTTPException(status_code=404, detail="This room is not ready.")
    return FileResponse(f, media_type="application/octet-stream", headers={"Cache-Control": "public, max-age=86400"})


class GenerateRequest(BaseModel):
    confirm: bool = False            # spending credits is never the default


def _start(key: str, base: str, by: str | None) -> dict:
    """Ask World Labs to make one room. Never twice: a room already made or being made is left alone."""
    body = {"display_name": f"Nova Studio {SAMPLES[key]['name']}"[:64], "model": MODEL, "tags": ["nova-studio"],
            "permission": {"public": False},
            "world_prompt": {"type": "image", "is_pano": True,
                             "image_prompt": {"source": "uri", "uri": base + SAMPLES[key]["pano"]}}}
    with _lock:
        meta = _read(key)
        if meta.get("status") in ("pending", "ready"):
            return meta
        try:
            with _client() as c:
                r = c.post(f"{API}/worlds:generate", json=body)
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"Could not reach World Labs ({type(e).__name__}).")
        if r.status_code >= 400:
            log_event("world_generate_refused", key=key, status=r.status_code)
            meta = {"status": "failed", "error": f"World Labs said no ({r.status_code}): {r.text[:200]}"}
            _write(key, meta)
            raise HTTPException(status_code=502, detail=meta["error"])
        meta = {"status": "pending", "operation_id": r.json()["operation_id"], "started": time.time(),
                "checked": time.time(), "by": by}
        _write(key, meta)
        log_event("world_generate_started", key=key, by=by or "auto")
    return meta


@router.get("/api/worlds")
def world_list() -> list[dict]:
    """Every sample room that can be walked around in, and whether it has been made."""
    return [_public(_check(k), k) for k in SAMPLES]


@router.post("/api/admin/worlds/{key}/generate")
def world_generate(key: str, req: GenerateRequest, request: Request, user=Depends(require("settings.manage"))) -> dict:
    """Owner only: ask World Labs to make this room. Does nothing if it is already made or being made."""
    meta = _check(key)
    if meta.get("status") in ("pending", "ready"):
        return _public(meta, key)
    if not req.confirm:
        raise HTTPException(status_code=400, detail="This uses about 1,500 World Labs credits. Send confirm: true to go ahead.")
    _api_key()
    base = os.getenv("PUBLIC_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or str(request.base_url).rstrip("/")
    if not base.startswith("https://"):
        raise HTTPException(status_code=400, detail="World Labs must be able to fetch the picture: run this on the live site.")
    return _public(_start(key, base.rstrip("/"), getattr(user, "email", None)), key)


def start_auto_worlds() -> None:
    """On the live site (Render sets RENDER_EXTERNAL_URL), make the rooms listed in WORLDS_AUTO once, by
    themselves, and keep checking until they are downloaded, so nobody has to press anything or open a page.
    Default list on Render: the TV lounge. A room that failed is NOT tried again on its own (that could spend
    credits over and over); the owner can retry it from Admin -> 3D rooms."""
    base = os.getenv("PUBLIC_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or ""
    keys = [k for k in os.getenv("WORLDS_AUTO", "tvroom" if base else "").split(",") if k.strip() in SAMPLES]
    if not (keys and base.startswith("https://") and os.getenv("WORLDLABS_API_KEY")):
        return

    def run():
        for k in keys:
            try:
                if _read(k).get("status") in (None, "none"):
                    _start(k, base.rstrip("/"), None)
            except Exception as e:                             # never take the site down over this
                log_event("world_auto_failed", key=k, error=type(e).__name__)
        for _ in range(180):                                  # check for up to an hour
            if all(_read(k).get("status") != "pending" for k in keys):
                return
            for k in keys:
                try:
                    _check(k)
                except Exception as e:
                    log_event("world_auto_poll_failed", key=k, error=type(e).__name__)
            time.sleep(POLL_EVERY)

    threading.Thread(target=run, name="worlds-auto", daemon=True).start()
