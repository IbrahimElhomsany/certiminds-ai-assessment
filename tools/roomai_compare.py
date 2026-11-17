"""Old vs new on the sample rooms, side by side.

    python -m tools.roomai_compare --out compare/ [--tile CN005] [--rooms living,kitchen]

For each room it saves one picture with four panels:
  photo | today (hand-marked corners from frontend/assets/rooms.js) | new (automatic AI) | what the AI found
plus timings.json (seconds per step on this machine).
Needs: pip install -r requirements-roomai.txt  (torch, transformers, scipy), and access to huggingface.co.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from backend.roomai import analyse, render

ROOT = Path(__file__).resolve().parents[1]
GLOSS = {"polished": 1.0, "gloss": 1.0, "satin": 0.4}


def load_rooms():
    js = (ROOT / "frontend/assets/rooms.js").read_text()
    code = "const window={};" + js + ";console.log(JSON.stringify(window.NOVA_ROOMS))"
    rooms = json.loads(subprocess.run(["node", "-e", code], capture_output=True, text=True, check=True).stdout)
    return [r for r in rooms if r.get("surfaces") and not r.get("kind")]


def homography(src, dst):
    A, b = [], []
    for (x, y), (u, v) in zip(src, dst):
        A += [[x, y, 1, 0, 0, 0, -u * x, -u * y], [0, 0, 0, x, y, 1, -v * x, -v * y]]
        b += [u, v]
    h = np.linalg.solve(np.array(A, float), np.array(b, float))
    return np.append(h, 1).reshape(3, 3)


def old_surface(s, size):
    """Mask + cm grid from the hand-marked quad, exactly like the browser does it."""
    W, H = size
    w, h = s["size"]
    Hm = homography(s["quad"], [[0, 0], [w, 0], [w, h], [0, h]])
    ys, xs = np.mgrid[0:H, 0:W].astype(float)
    q = np.stack([xs, ys, np.ones_like(xs)], -1) @ Hm.T
    with np.errstate(divide="ignore", invalid="ignore"):
        uv = q[..., :2] / q[..., 2:3]
    c = np.mean(s["quad"], 0)                               # a point surely on the surface
    wc = Hm[2] @ [c[0], c[1], 1]
    uv[q[..., 2] * wc <= 0] = np.nan                         # beyond the horizon
    m = Image.new("L", size, 0)
    d = ImageDraw.Draw(m)
    d.polygon([tuple(p) for p in s["area"]], fill=255)
    for k in s.get("keep", []):
        if isinstance(k, dict) and "ring" in k:
            cx, cy, rx, ry, irx, iry = k["ring"]
            d.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=0)
        else:
            d.polygon([tuple(p) for p in k], fill=0)
    return np.asarray(m) > 127, uv


def label(img, text):
    img = img.copy()
    d = ImageDraw.Draw(img)
    try:
        f = ImageFont.truetype("DejaVuSans-Bold.ttf", max(16, img.size[0] // 40))
    except OSError:
        f = ImageFont.load_default()
    d.rectangle([0, 0, img.size[0], f.size + 16 if hasattr(f, "size") else 30], fill=(0, 0, 0))
    d.text((10, 6), text, fill=(255, 255, 255), font=f)
    return img


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="compare")
    ap.add_argument("--tile", default="CN005")
    ap.add_argument("--wall-tile", default="CN011")
    ap.add_argument("--rooms", default="")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    cat = {c["sku"]: c for c in json.loads((ROOT / "data/catalog.json").read_text())}
    ft, wt = cat[a.tile], cat[a.wall_tile]
    ftex = Image.open(ROOT / f"images/{a.tile}.jpg")
    wtex = Image.open(ROOT / f"images/{a.wall_tile}.jpg")
    fsize = (ft["tile_width_cm"], ft["tile_length_cm"])
    wsize = (wt["tile_width_cm"], wt["tile_length_cm"])
    want = set(filter(None, a.rooms.split(",")))
    timings = {}
    for room in load_rooms():
        if want and room["id"] not in want:
            continue
        photo = Image.open(ROOT / room["photo"].lstrip("/")).convert("RGB")
        # today
        old = photo
        for kind, surfs in room["surfaces"].items():
            for s in surfs:
                m, uv = old_surface(s, photo.size)
                fl = kind == "floor"
                old = render.tile(old, m, uv, ftex if fl else wtex, fsize if fl else wsize,
                                  gloss=GLOSS.get((ft if fl else wt).get("finish"), 0.0))
        # new
        res = analyse.analyse(photo)
        img = res["image"]
        new = img
        for s in res["surfaces"]:
            fl = s["name"] == "floor"
            new = render.tile(new, s["mask"], s["uv"], ftex if fl else wtex, fsize if fl else wsize,
                              gloss=GLOSS.get((ft if fl else wt).get("finish"), 0.0))
        found = render.overlay(img, {k: v for k, v in res["layers"].items()})
        names = ", ".join(sorted(res["layers"]))
        size = img.size
        panels = [label(photo.resize(size), "Photo"), label(old.resize(size), "Today (corners marked by hand)"),
                  label(new, "New (automatic AI)"), label(found, "AI found: " + names[:80])]
        sheet = Image.new("RGB", (size[0] * 2, size[1] * 2))
        for i, p in enumerate(panels):
            sheet.paste(p, ((i % 2) * size[0], (i // 2) * size[1]))
        sheet.save(out / f"{room['id']}.jpg", quality=88)
        timings[room["id"]] = {k: round(v, 1) for k, v in res["timings"].items()}
        timings[room["id"]]["scale_fix"] = round(res["scale"], 2)
        timings[room["id"]]["walls_found"] = sum(1 for s in res["surfaces"] if s["name"] != "floor")
        print(room["id"], timings[room["id"]], flush=True)
    (out / "timings.json").write_text(json.dumps(timings, indent=2))


if __name__ == "__main__":
    main()
