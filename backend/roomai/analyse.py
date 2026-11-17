"""Analyse one room photo once: which pixels are floor, walls and furniture, and where each surface pixel is in cm.

Steps (all on the server, once per photo; the result is cached):
 1. Shrink the photo to at most MAX_SIDE px (speed).
 2. Grounding DINO finds boxes for surfaces and objects by name.
 3. SAM 2 turns every box into a sharp cut-out.
 4. Objects win over surfaces (a sofa in front of the wall is not wall). Rugs are not floor.
 5. Depth Anything gives near/far; geometry.solve_room fits the floor and up to 3 walls and gives cm.
"""
from __future__ import annotations

import time

import numpy as np
from PIL import Image

from . import geometry, models

MAX_SIDE = 1024
SURFACES = ["floor", "wall", "ceiling"]
# Things that stand in front of a surface or are not to be tiled. Kept short: every label costs a little time.
OBJECTS = ["rug", "sofa", "armchair", "chair", "table", "bed", "cabinet", "kitchen counter", "bathtub",
           "toilet", "sink", "shower glass", "plant", "lamp", "television", "window", "door", "curtain",
           "shelf", "cushion", "mirror", "picture frame", "stairs",
           # things fixed ON a wall or ceiling: they stay in front of the tiles
           "air conditioner", "painting", "railing", "ceiling light", "radiator", "light switch",
           "sky", "tree"]
MIN_PIXELS = 0.002          # ignore cut-outs smaller than 0.2 % of the photo


def _resize(img: Image.Image) -> Image.Image:
    img = img.convert("RGB")
    s = MAX_SIDE / max(img.size)
    return img.resize((round(img.size[0] * s), round(img.size[1] * s)), Image.LANCZOS) if s < 1 else img


def _split_walls(disp: np.ndarray, wall: np.ndarray, max_walls: int = 3,
                 grow: np.ndarray | None = None) -> list[np.ndarray]:
    """A room can show 2-3 walls at different angles: peel them off one flat plane at a time,
    then give every wall pixel to the plane it fits best and tidy the edges."""
    from scipy.ndimage import binary_closing, binary_opening

    span = float(np.percentile(disp, 99) - np.percentile(disp, 1)) or 1.0
    tol = 0.012
    left, coefs = wall.copy(), []
    for _ in range(max_walls):
        if left.sum() < 0.01 * wall.size:
            break
        coef, inl = geometry.fit_plane_ransac(disp, left, tol=tol, span=span)
        if coef is None or inl.sum() < 0.01 * wall.size:
            break
        coefs.append(coef)
        left &= ~inl
    if not coefs:
        return []
    H, W = disp.shape
    ys, xs = np.mgrid[0:H, 0:W]
    res = np.stack([np.abs(c[0] * xs + c[1] * ys + c[2] - disp) for c in coefs])
    best = res.argmin(0)
    # Pixels next to a wall that the cut-out model missed (veins, reflections, the strip above the
    # floor) join the wall if they lie on its flat plane.
    region = wall | grow if grow is not None else wall
    good = region & (res.min(0) < 2 * tol * span)
    out = []
    for i in range(len(coefs)):
        m = binary_closing(binary_opening(good & (best == i), iterations=2), iterations=3) & region
        if m.sum() >= 0.01 * wall.size:
            out.append(m)
    return out


def _missed_walls(disp: np.ndarray, free: np.ndarray, known: np.ndarray, min_share: float = 0.012) -> list[np.ndarray]:
    """Side walls the cut-out model didn't call "wall" (a narrow white strip next to a door, the wall
    round a corner). Any big leftover patch that touches the floor or a found wall and is one flat
    piece in the depth picture becomes a wall candidate; the geometry then keeps it only if it stands upright."""
    from scipy.ndimage import binary_opening, label

    H, W = free.shape
    span = float(np.percentile(disp, 99) - np.percentile(disp, 1)) or 1.0
    lab, n = label(binary_opening(free, iterations=3))
    touch = _near(known, max(4, W // 60))
    out = []
    for i in range(1, n + 1):
        m = lab == i
        if m.sum() < min_share * H * W or not (m & touch).any():
            continue
        coef, inl = geometry.fit_plane_ransac(disp, m, tol=0.012, span=span)
        if coef is None:
            continue
        # keep the biggest flat piece (the patch can also hold a bit of cornice or door frame)
        pl, k = label(binary_opening(inl, iterations=2))
        if not k:
            continue
        sizes = np.bincount(pl.ravel())[1:]
        best = pl == (1 + int(sizes.argmax()))
        if best.sum() >= min_share * H * W and best.sum() >= 0.4 * m.sum():
            out.append(best)
    return out


def _merge_parallel(parts, walls, max_deg=12.0):
    """Two pieces of the same wall (same direction, same distance) become one."""
    groups = []
    for m, p in zip(parts, walls):
        for g in groups:
            q = g["plane"]
            ang = np.degrees(np.arccos(min(1.0, abs(float(p.normal @ q.normal)))))
            if ang < max_deg and abs(p.dist - q.dist) / max(q.dist, 1e-6) < 0.15:
                g["mask"] |= m
                break
        else:
            groups.append({"mask": m.copy(), "plane": p})
    return [g["mask"] for g in groups]


def analyse(photo: Image.Image, camera_height_cm: float = geometry.CAMERA_HEIGHT_CM) -> dict:
    t0 = time.perf_counter()
    img = _resize(photo)
    W, H = img.size
    timings = {}

    found = [f for f in models.detect(img, SURFACES + OBJECTS) if f[0] in SURFACES or f[1] >= 0.3]
    timings["find_s"] = time.perf_counter() - t0

    t1 = time.perf_counter()
    masks = models.segment(img, [b for _, _, b in found])
    timings["cut_s"] = time.perf_counter() - t1

    t2 = time.perf_counter()
    disp = models.depth(img)
    timings["depth_s"] = time.perf_counter() - t2

    layers: dict[str, np.ndarray] = {}
    for (label, score, box), m in zip(found, masks):
        if m.sum() < MIN_PIXELS * W * H:
            continue
        layers[label] = layers.get(label, np.zeros((H, W), bool)) | m

    objects = np.zeros((H, W), bool)
    for name in OBJECTS:
        if name in layers:
            objects |= layers[name]
    empty = np.zeros((H, W), bool)
    floor = _fill(layers.get("floor", empty), objects)
    ceiling = _fill(layers.get("ceiling", empty), objects) & ~floor
    wall = _fill(layers.get("wall", empty), objects | floor | ceiling) & ~floor & ~ceiling

    t3 = time.perf_counter()
    free = ~(objects | floor | ceiling)
    wall_parts = _split_walls(disp, wall, grow=free & _near(wall, max(4, W // 40)))
    wall_parts += _missed_walls(disp, free & ~np.any(wall_parts, 0) if wall_parts else free, floor | wall)
    K, floor_plane, wall_planes = geometry.solve_room(disp, floor, wall_parts, camera_height_cm=camera_height_cm)
    if len(wall_planes) > 1:
        merged = _merge_parallel([wall_parts[p.extra["part"]] for p in wall_planes], wall_planes)
        if len(merged) < len(wall_planes):
            wall_parts = merged
            K, floor_plane, wall_planes = geometry.solve_room(disp, floor, wall_parts, camera_height_cm=camera_height_cm)
    scale, scale_from = 1.0, []
    if floor_plane is not None:
        scale, scale_from = geometry.scale_from_objects(floor_plane, K, layers)
        for p in [floor_plane, *wall_planes]:
            p.dist *= scale
    surfaces = []
    if floor_plane is not None:
        # Keep only floor pixels that really lie on the flat floor (drops steps, low furniture edges).
        # Floor next to the cut-out that the model missed joins if it lies on the floor plane.
        c = floor_plane.coef
        ys, xs = np.mgrid[0:H, 0:W]
        on_plane = np.abs(c[0] * xs + c[1] * ys + c[2] - disp) < 0.02 * (np.ptp(disp[floor]) or 1.0)
        walls_any = np.any(wall_parts, 0) if wall_parts else np.zeros((H, W), bool)
        floor = floor | (~(objects | ceiling | walls_any) & _near(floor, max(4, W // 40)) & on_plane)
        surfaces.append({"name": "floor", "mask": floor & (floor_plane.inliers | _near(floor_plane.inliers)) | (floor & on_plane),
                         "uv": geometry.plane_uv(floor_plane, K, (H, W)), "plane": floor_plane})
        for p in wall_planes:
            part = wall_parts[p.extra["part"]]
            if abs(p.extra["angle_to_floor_deg"] - 90) > 20:      # not standing upright: not a usable wall
                continue
            uv = geometry.plane_uv(p, K, (H, W))
            part = _straight_top(part, uv, objects | floor)
            surfaces.append({"name": p.name, "mask": part, "uv": uv, "plane": p,
                             "angle_to_floor_deg": p.extra["angle_to_floor_deg"]})
    timings["geometry_s"] = time.perf_counter() - t3
    timings["total_s"] = time.perf_counter() - t0

    return {"scale": scale, "scale_from": scale_from, "image": img, "disp": disp, "layers": layers, "found": found, "surfaces": surfaces,
            "objects": objects, "timings": timings, "K": K}


def _straight_top(mask: np.ndarray, uv: np.ndarray, blocked: np.ndarray, max_gap: float = 0.06) -> np.ndarray:
    """A wall's top edge (where it meets the ceiling) is a level line on the wall, but the cut-out model
    often leaves it ragged like torn paper. On the wall's own cm grid that line has one height, so:
    find that height from the tops of the columns, cut spikes above it, fill small bites below it."""
    H, W = mask.shape
    v = uv[..., 1]
    if mask.sum() < 500 or not np.isfinite(v[mask]).any():
        return mask
    cols = np.nonzero(mask.any(0))[0]
    top = mask.argmax(0)[cols]
    vt = v[top, cols]
    ok = np.isfinite(vt)
    if ok.sum() < 20:
        return mask
    up = np.sign(np.nanmedian(vt[ok]) - np.nanmedian(v[mask])) or 1.0
    h = up * v                                       # height on the wall in cm, bigger = higher
    line = float(np.percentile(h[top[ok], cols[ok]], 90))
    span = float(np.nanpercentile(h[mask], 95) - np.nanpercentile(h[mask], 5)) or 1.0
    below = np.isfinite(h) & (h <= line)
    out = mask & (np.isfinite(h) & (h <= line + 0.01 * span))          # spikes above the line go
    gap = int(max_gap * H)
    for x, y in zip(cols[ok], top[ok]):                                  # bites below the line are filled
        ys = np.nonzero(below[:y, x])[0]
        if len(ys) and 0 < y - ys[0] <= gap:
            out[ys[0]:y, x] |= ~blocked[ys[0]:y, x]
    return out


def _fill(surface: np.ndarray, others: np.ndarray) -> np.ndarray:
    """Close holes the cut-out model leaves in a surface (bright sun patches, reflections): any gap
    surrounded by the surface and the things in front of it becomes surface, then the things are removed."""
    from scipy.ndimage import binary_fill_holes
    if not surface.any():
        return surface
    return binary_fill_holes(surface | (others & _near(surface, 8))) & ~others


def _near(mask: np.ndarray, r: int = 3) -> np.ndarray:
    """Grow a mask by r px (fills tiny holes the depth model leaves at grout lines and shadows)."""
    from scipy.ndimage import binary_dilation
    return binary_dilation(mask, iterations=r)
