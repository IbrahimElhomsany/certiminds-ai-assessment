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
    """A room can show 2-3 walls at different angles: peel them off one flat plane at a time."""
    left, out = wall.copy(), []
    for _ in range(max_walls):
        if left.sum() < 0.01 * wall.size:
            break
        coef, inl = geometry.fit_plane_ransac(disp, left, tol=0.03)
        if coef is None or inl.sum() < 0.005 * wall.size:
            break
        out.append(inl)
        left &= ~inl
    return out


def analyse(photo: Image.Image, camera_height_cm: float = geometry.CAMERA_HEIGHT_CM) -> dict:
    t0 = time.perf_counter()
    img = _resize(photo)
    W, H = img.size
    timings = {}

    found = models.detect(img, SURFACES + OBJECTS)
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
    surfaces = []
    if floor_plane is not None:
        # Keep only floor pixels that really lie on the flat floor (drops steps, low furniture edges).
        surfaces.append({"name": "floor", "mask": floor & (floor_plane.inliers | _near(floor_plane.inliers)),
                         "uv": geometry.plane_uv(floor_plane, K, (H, W))})
        for p, part in zip(wall_planes, wall_parts):
            if abs(p.extra["angle_to_floor_deg"] - 90) > 20:      # not standing upright: not a usable wall
                continue
            surfaces.append({"name": p.name, "mask": part, "uv": geometry.plane_uv(p, K, (H, W)),
                             "angle_to_floor_deg": p.extra["angle_to_floor_deg"]})
    timings["geometry_s"] = time.perf_counter() - t3
    timings["total_s"] = time.perf_counter() - t0

    return {"image": img, "disp": disp, "layers": layers, "found": found, "surfaces": surfaces,
            "objects": objects, "timings": timings, "K": K}


def _near(mask: np.ndarray, r: int = 3) -> np.ndarray:
    """Grow a mask by r px (fills tiny holes the depth model leaves at grout lines and shadows)."""
    from scipy.ndimage import binary_dilation
    return binary_dilation(mask, iterations=r)
