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
           "shelf", "cushion", "mirror", "picture frame", "stairs"]
MIN_PIXELS = 0.002          # ignore cut-outs smaller than 0.2 % of the photo


def _resize(img: Image.Image) -> Image.Image:
    img = img.convert("RGB")
    s = MAX_SIDE / max(img.size)
    return img.resize((round(img.size[0] * s), round(img.size[1] * s)), Image.LANCZOS) if s < 1 else img


def _split_walls(disp: np.ndarray, wall: np.ndarray, max_walls: int = 3) -> list[np.ndarray]:
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
    good = wall & (res.min(0) < 3 * tol * span)
    out = []
    for i in range(len(coefs)):
        m = binary_closing(binary_opening(good & (best == i), iterations=2), iterations=3) & wall
        if m.sum() >= 0.01 * wall.size:
            out.append(m)
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
    floor = layers.get("floor", np.zeros((H, W), bool)) & ~objects
    ceiling = layers.get("ceiling", np.zeros((H, W), bool)) & ~objects & ~floor
    wall = layers.get("wall", np.zeros((H, W), bool)) & ~objects & ~floor & ~ceiling

    t3 = time.perf_counter()
    wall_parts = _split_walls(disp, wall)
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
        surfaces.append({"name": "floor", "mask": floor & (floor_plane.inliers | _near(floor_plane.inliers)),
                         "uv": geometry.plane_uv(floor_plane, K, (H, W))})
        for p in wall_planes:
            part = wall_parts[p.extra["part"]]
            if abs(p.extra["angle_to_floor_deg"] - 90) > 20:      # not standing upright: not a usable wall
                continue
            surfaces.append({"name": p.name, "mask": part, "uv": geometry.plane_uv(p, K, (H, W)),
                             "angle_to_floor_deg": p.extra["angle_to_floor_deg"]})
    timings["geometry_s"] = time.perf_counter() - t3
    timings["total_s"] = time.perf_counter() - t0

    return {"scale": scale, "scale_from": scale_from, "image": img, "disp": disp, "layers": layers, "found": found, "surfaces": surfaces,
            "objects": objects, "timings": timings, "K": K}


def _near(mask: np.ndarray, r: int = 3) -> np.ndarray:
    """Grow a mask by r px (fills tiny holes the depth model leaves at grout lines and shadows)."""
    from scipy.ndimage import binary_dilation
    return binary_dilation(mask, iterations=r)
