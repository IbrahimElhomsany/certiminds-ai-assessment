"""One tile face per SKU for the room visualisers: images/tex/<SKU>.jpg (square, 1024 px, no grout, the long side of
the tile runs left to right; the visualiser crops a strip for long tiles and draws the grout itself).

Most faces are the shop's earlier tile photos. The ones that used to be photos of several tiles at once (Ivory Mist,
Aqua Shield, Terra Rustic, Metro White, Pearl Mosaic) and Urban Graphite are made here instead, so every face is
one clean tile at the right size. Their surface detail comes from Poly Haven CC0 textures (polyhaven.com, free for
commercial use): the light and dark of the texture is kept and laid over our tile colour.
    python tools/tiles/make_faces.py --ph <folder with the Poly Haven *_diff_2k.jpg files, named by asset>
Assets used: dark_wood (Ebony Walnut), brushed_concrete_2 (Urban Graphite), garage_floor (Terra Rustic),
plaster_grey_04 (glaze cloud for Ivory Mist and Aqua Shield).
"""
from __future__ import annotations

import argparse
import os

import numpy as np
from PIL import Image, ImageFilter

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
IMG, TEX = os.path.join(ROOT, "images"), os.path.join(ROOT, "images", "tex")
N = 1024
KEEP = ["CN001", "CN002", "CN003", "CN004", "CN006", "CN011", "CN012", "CN014", "CN015"]   # already one tile face


def noise(rng, scale: int, n: int = N) -> np.ndarray:
    """Smooth value noise in -1..1: random grid, blown up with a soft filter."""
    g = rng.random((max(2, n // scale), max(2, n // scale)))
    im = Image.fromarray((g * 255).astype(np.uint8)).resize((n, n), Image.BICUBIC)
    im = im.filter(ImageFilter.GaussianBlur(scale / 4))
    a = np.asarray(im, np.float32) / 255
    return (a - a.mean()) / (a.std() + 1e-6) / 3


def fbm(rng, scales=(512, 256, 128, 64, 24, 8), weights=(1, .7, .5, .35, .25, .15)) -> np.ndarray:
    out = sum(w * noise(rng, s) for s, w in zip(scales, weights))
    return out / sum(weights)


def save(sku: str, rgb: np.ndarray):
    Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8)).save(os.path.join(TEX, f"{sku}.jpg"), quality=92)


def tint(base, var: np.ndarray) -> np.ndarray:
    return np.asarray(base, np.float32)[None, None, :] * (1 + var[..., None])


def detail(path: str, blur: float = 0) -> np.ndarray:
    """The light and dark of a texture, as -1..1 around its average (its own colour is dropped)."""
    im = Image.open(path).convert("L").resize((N, N), Image.LANCZOS)
    if blur:
        im = im.filter(ImageFilter.GaussianBlur(blur))
    a = np.asarray(im, np.float32)
    return (a - a.mean()) / (a.std() + 1e-6) / 3


def ivory(ph, rng):      # CN007 Ivory Mist: soft ivory glaze with a faint cloud
    return tint((236, 229, 212), 0.03 * detail(f"{ph}/plaster_grey_04.jpg", 6))


def aqua(ph, rng):       # CN008 Aqua Shield: hand-glazed aqua, deeper where the glaze pools
    cloud = detail(f"{ph}/plaster_grey_04.jpg", 3)
    rgb = tint((50, 146, 142), 0.12 * cloud + 0.04 * noise(rng, 200))
    rgb[..., 0] *= 1 - 0.15 * cloud                    # deeper blue-green in the pools
    return rgb


def terracotta(ph, rng): # CN009 Terra Rustic: fired clay, mottled, with small pits
    d = detail(f"{ph}/garage_floor.jpg", 1.5)
    rgb = tint((166, 92, 62), 0.16 * d + 0.04 * fbm(rng))
    rgb[..., 1] *= 1 + 0.05 * noise(rng, 220)
    return rgb


def metro(ph, rng):      # CN010 Metro White: bright white gloss, almost flat
    return tint((243, 243, 240), 0.012 * fbm(rng))


def graphite(ph, rng):   # CN005 Urban Graphite: dark through-body concrete look
    return tint((84, 86, 89), 0.20 * detail(f"{ph}/brushed_concrete_2.jpg"))


def pearl(ph, rng):      # CN013 Pearl Mosaic: one 30 x 30 cm sheet of 12 x 12 pearl chips
    k, gap = 12, 0.08                                   # chips per side, gap as a share of one chip
    yy, xx = np.mgrid[0:N, 0:N] / N * k
    cx, cy = np.floor(xx).astype(int), np.floor(yy).astype(int)
    lx, ly = xx - cx, yy - cy
    edge = np.minimum(np.minimum(lx, 1 - lx), np.minimum(ly, 1 - ly))
    chip = np.clip((edge - gap / 2) / 0.03, 0, 1)
    tone = rng.normal(0, 1, (k + 1, k + 1, 1)) * 5 + rng.normal(0, 1, (k + 1, k + 1, 3)) * 1.5
    col = np.array([240, 235, 227]) + tone[cy, cx]
    sheen = 8 * (1 - 2 * np.abs(lx - 0.35))[..., None] * (1 - 2 * np.abs(ly - 0.3))[..., None]   # pearly lift
    grout = np.array([212, 208, 200], np.float32)
    return grout * (1 - chip[..., None]) + (col + sheen) * chip[..., None]


def walnut(ph, rng):     # CN016 Ebony Walnut: CC0 wood, grain left to right, darkened
    a = np.asarray(Image.open(f"{ph}/dark_wood.jpg").convert("RGB").resize((N, N), Image.LANCZOS), np.float32)
    a = 0.75 * a + 0.25 * a.mean(axis=2, keepdims=True)  # a little less red
    return a * np.array([0.62, 0.56, 0.52])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ph", required=True)
    args = ap.parse_args()
    os.makedirs(TEX, exist_ok=True)
    for sku in KEEP:
        dst = os.path.join(TEX, f"{sku}.jpg")
        if not os.path.exists(dst):
            Image.open(os.path.join(IMG, f"{sku}.jpg")).convert("RGB").resize((N, N), Image.LANCZOS).save(dst, quality=92)
    rng = np.random.default_rng(7)
    for sku, fn in [("CN005", graphite), ("CN007", ivory), ("CN008", aqua), ("CN009", terracotta),
                    ("CN010", metro), ("CN013", pearl), ("CN016", walnut)]:
        save(sku, fn(args.ph, rng))
    print("faces in", TEX)


if __name__ == "__main__":
    main()
