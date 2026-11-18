"""Runs the room AI (backend/roomai) on a 360° picture and saves its floor/wall votes as <photo>.votes.png.

The 360° viewer (frontend/assets/walk3d.js, panoLayout) reads that file: floor share on top, wall share below.
A 360° picture is too bent for the room AI, so we cut it into the same normal views the browser used to
(6 looking down at the floor, 1 straight down, 4 level at the walls), find floor and walls in each view, and
count back on a grid over the 360° picture how often each spot was seen as floor or wall.

    python tools/roomai_pano.py images/pano_tvroom.jpg [more.jpg ...]
"""
from __future__ import annotations

import math
import os
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.roomai import analyse, models  # noqa: E402

VW, VH = 768, 576          # one normal view
CW, CH = 480, 240          # the vote grid over the 360° picture
D = math.pi / 180


def views():
    out = [(k * 60 * D, -40 * D, 1.0) for k in range(6)] + [(0.0, -90 * D, 1.0)]
    out += [((45 + k * 90) * D, 5 * D, math.tan(52 * D)) for k in range(4)]
    return out


def cut(src: np.ndarray, yaw: float, pitch: float, f: float):
    """The view's pixels, and for each pixel its cell on the vote grid and whether it looks below eye level."""
    sh, sw = src.shape[:2]
    i, j = np.meshgrid(np.arange(VW), np.arange(VH))
    x = (2 * (i + 0.5) / VW - 1) * f
    y = -(2 * (j + 0.5) / VH - 1) * f * VH / VW
    cp, sp, cy, sy = math.cos(pitch), math.sin(pitch), math.cos(yaw), math.sin(yaw)
    y1, z1 = y * cp + sp, -y * sp + cp
    dx, dz = x * cy + z1 * sy, -x * sy + z1 * cy
    th, ph = np.arctan2(dx, dz), np.arctan2(y1, np.hypot(dx, dz))
    u = (0.5 + th / (2 * math.pi)) % 1.0
    w = np.clip(0.5 - ph / math.pi, 0, 0.999999)
    img = src[(w * sh).astype(int), (u * sw).astype(int)]
    cell = (w * CH).astype(int) * CW + (u * CW).astype(int)
    return Image.fromarray(img), cell, ph < 0


def masks(view: Image.Image):
    """Floor and wall in one view, furniture cut out (the first, quick half of analyse.analyse)."""
    img = analyse._resize(view)
    W, H = img.size
    found = [f for f in models.detect(img, analyse.SURFACES + analyse.OBJECTS) if f[0] in analyse.SURFACES or f[1] >= 0.3]
    cut_out = models.segment(img, [b for _, _, b in found]) if found else []
    layers = {}
    for (label, _s, _b), m in zip(found, cut_out):
        if m.sum() >= analyse.MIN_PIXELS * W * H:
            layers[label] = layers.get(label, np.zeros((H, W), bool)) | m
    objects = np.zeros((H, W), bool)
    for name in analyse.OBJECTS:
        if name in layers:
            objects |= layers[name]
    empty = np.zeros((H, W), bool)
    floor = analyse._fill(layers.get("floor", empty), objects)
    ceiling = analyse._fill(layers.get("ceiling", empty), objects) & ~floor
    wall = analyse._fill(layers.get("wall", empty), objects | floor | ceiling) & ~floor & ~ceiling
    if (W, H) != (VW, VH):
        floor = np.array(Image.fromarray(floor).resize((VW, VH), Image.NEAREST))
        wall = np.array(Image.fromarray(wall).resize((VW, VH), Image.NEAREST))
    return floor, wall


def votes(path: str) -> str:
    src = np.asarray(Image.open(path).convert("RGB"))
    seen, fl, wl = (np.zeros(CW * CH) for _ in range(3))
    for k, (yaw, pitch, f) in enumerate(views()):
        view, cell, below = cut(src, yaw, pitch, f)
        floor, wall = masks(view)
        np.add.at(seen, cell.ravel(), 1)
        np.add.at(fl, cell.ravel(), (floor & below).ravel())
        np.add.at(wl, cell.ravel(), wall.ravel())
        print(f"  {os.path.basename(path)} view {k + 1}/11", flush=True)
    pf = np.where(seen > 0, fl / np.maximum(seen, 1), 0).reshape(CH, CW)
    pw = np.where(seen > 0, wl / np.maximum(seen, 1), 0).reshape(CH, CW)
    out = np.vstack([pf, pw])
    dst = path[:-4] + ".votes.png"
    Image.fromarray(np.round(out * 255).astype(np.uint8)).convert("RGBA").save(dst)
    return dst


if __name__ == "__main__":
    for p in sys.argv[1:]:
        print("saved", votes(p), flush=True)
