"""Draw tiles on a surface, the same recipe as the browser visualiser (frontend/assets/visualiser.js):
tile size in cm, grout lines, a random quarter turn per square tile, and the photo's own light and
shadows multiplied back over the tiles. Used to compare old and new side by side."""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageFilter


def _luma(a):
    return a[..., 0] * 0.299 + a[..., 1] * 0.587 + a[..., 2] * 0.114


def tile(photo: Image.Image, mask: np.ndarray, uv: np.ndarray, tex: Image.Image, tile_cm=(60.0, 60.0),
         grout_cm=0.3, grout_rgb=(0.82, 0.80, 0.77), light=True) -> Image.Image:
    base = np.asarray(photo.convert("RGB"), dtype=np.float64) / 255
    H, W = mask.shape
    t = np.asarray(tex.convert("RGB").resize((512, 512), Image.LANCZOS), dtype=np.float64) / 255
    ok = mask & np.isfinite(uv[..., 0]) & np.isfinite(uv[..., 1])
    u, v = uv[..., 0][ok], uv[..., 1][ok]
    gx, gy = u / tile_cm[0], v / tile_cm[1]
    cx, cy = np.floor(gx), np.floor(gy)
    lx, ly = gx - cx, gy - cy
    h = np.modf(np.sin(cx * 127.1 + cy * 311.7) * 43758.5453)[0] % 1
    r = np.floor(h * 4)                                       # quarter turns
    lx2 = np.where(r == 1, 1 - ly, np.where(r == 2, 1 - lx, np.where(r == 3, ly, lx)))
    ly2 = np.where(r == 1, lx, np.where(r == 2, 1 - ly, np.where(r == 3, 1 - lx, ly)))
    px = np.clip((lx2 * 511).astype(int), 0, 511)
    py = np.clip((ly2 * 511).astype(int), 0, 511)
    col = t[py, px]
    d = np.minimum(np.minimum(lx, 1 - lx) * tile_cm[0], np.minimum(ly, 1 - ly) * tile_cm[1])
    inside = np.clip((d - grout_cm / 2) / 0.15 + 0.5, 0, 1)[:, None]
    col = col * inside + np.array(grout_rgb) * (1 - inside)
    if light:
        blur = np.asarray(photo.convert("RGB").filter(ImageFilter.GaussianBlur(6)), dtype=np.float64) / 255
        lum = _luma(blur)
        mean = float(np.median(lum[ok])) or 0.5
        col = col * np.clip(lum[ok] / mean, 0.28, 1.75)[:, None]
    out = base.copy()
    # soft edge: 1-px feather on the mask border
    soft = np.asarray(Image.fromarray((ok * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.8)),
                      dtype=np.float64) / 255
    tiled = base.copy()
    tiled[ok] = np.clip(col, 0, 1)
    out = tiled * soft[..., None] + base * (1 - soft[..., None])
    return Image.fromarray((out * 255).astype(np.uint8))


def overlay(photo: Image.Image, layers: dict[str, np.ndarray]) -> Image.Image:
    """Colour each found layer so people can see what the AI recognised."""
    pal = [(46, 204, 113), (52, 152, 219), (241, 196, 15), (231, 76, 60), (155, 89, 182), (26, 188, 156),
           (230, 126, 34), (236, 240, 241), (149, 165, 166), (243, 156, 18)]
    a = np.asarray(photo.convert("RGB"), dtype=np.float64)
    for i, (name, m) in enumerate(layers.items()):
        c = np.array(pal[i % len(pal)], dtype=np.float64)
        a[m] = a[m] * 0.45 + c * 0.55
    return Image.fromarray(a.astype(np.uint8))
