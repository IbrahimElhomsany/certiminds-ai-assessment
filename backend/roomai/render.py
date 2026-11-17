"""Draw tiles on a surface, the same recipe as the browser visualiser (frontend/assets/visualiser.js):
tile size in cm, grout lines, a random quarter turn per square tile, and the photo's own light and
shadows multiplied back over the tiles. Used to compare old and new side by side."""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageFilter


def _luma(a):
    return a[..., 0] * 0.299 + a[..., 1] * 0.587 + a[..., 2] * 0.114


def lighting(photo: Image.Image, mask: np.ndarray):
    """The room's light on this surface, without the old surface's pattern.

    - shade: how much brighter or darker each spot is than the surface's middle value. A median filter
      first wipes out thin lines (old grout, marble veins) but keeps sun patches and shadows.
    - tint: the colour of the light (warm sun, cool daylight), kept mild.
    - shine: bright reflections (lamps, windows) for polished tiles.
    Blurs only use pixels of this surface, so a dark sofa next to the floor doesn't darken it."""
    from scipy.ndimage import gaussian_filter, median_filter

    H0, W0 = mask.shape
    if W0 > 512:                                              # light changes slowly: work small, then enlarge
        size = (512, round(H0 * 512 / W0))
        small = Image.fromarray(mask.astype(np.uint8) * 255).resize(size, Image.BILINEAR)
        sh, ti, sn = lighting(photo.resize(size, Image.BILINEAR), np.asarray(small) > 127)
        up = lambda x: np.asarray(Image.fromarray(x.astype(np.float32)).resize((W0, H0), Image.BILINEAR))
        return up(sh), np.stack([up(ti[..., i]) for i in range(3)], -1), up(sn)

    a = np.asarray(photo.convert("RGB"), dtype=np.float64) / 255
    H, W = mask.shape
    k = max(5, int(W * 0.025) | 1)                            # ~25 px on a 1024 px photo
    m = mask.astype(np.float64)

    def mblur(x, sigma):                                      # blur that ignores pixels off the surface
        num = gaussian_filter(x * m, sigma)
        den = gaussian_filter(m, sigma)
        return num / np.maximum(den, 1e-4)

    lum = _luma(a)
    clean = median_filter(lum, size=k)                        # old pattern removed, light kept
    near = mblur(clean, W * 0.003)
    wide = mblur(clean, W * 0.03)
    mid = float(np.median(near[mask])) or 0.5
    shade = np.clip(near / mid, 0.25, 1.8)
    rgb = np.stack([mblur(median_filter(a[..., i], size=k), W * 0.03) for i in range(3)], -1)
    tint = rgb / np.maximum(_luma(rgb)[..., None], 1e-4)
    tint = tint / np.maximum(np.median(tint[mask], 0), 1e-4)
    tint = 1 + (tint - 1) * 0.6
    shine = np.clip((lum - wide) / 0.35, 0, 1) * (lum > 0.75)
    return shade, tint, gaussian_filter(shine, 1.0)


def tile(photo: Image.Image, mask: np.ndarray, uv: np.ndarray, tex: Image.Image, tile_cm=(60.0, 60.0),
         grout_cm=0.3, grout_rgb=(0.82, 0.80, 0.77), light=True, gloss: float = 0.0) -> Image.Image:
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
    if light and ok.any():
        shade, tint, shine = lighting(photo, ok)
        col = col * shade[ok][:, None] * tint[ok]
        col = col + shine[ok][:, None] * gloss * 0.5
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
