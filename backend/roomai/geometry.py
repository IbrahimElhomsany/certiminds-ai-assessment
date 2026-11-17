"""Room geometry from a depth map: find the flat floor and walls and give every pixel a spot in cm.

Plain words:
- The depth model gives "disparity" for each pixel: big = close, small = far (like 1 / distance).
- For any flat surface (floor, a wall) disparity changes in a straight line across the photo:
  d = a*x + b*y + c. So we fit that line-equation to the floor pixels (ignoring outliers, RANSAC).
- From (a, b, c) and a guessed camera lens we get the surface's direction in 3D (its "normal").
- Walls must stand at 90 degrees to the floor. The depth model's numbers have an unknown offset;
  we pick the offset that makes floor and walls closest to 90 degrees.
- With the camera height (default 1.45 m) every floor pixel becomes a real point in cm, so a
  60 x 60 cm tile is drawn at the right size and shrinks towards the back of the room.

Only numpy here, so it is easy to test without any AI model.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

CAMERA_HEIGHT_CM = 145.0
FOCAL_FACTOR = 0.85          # focal length = 0.85 x image width (a normal phone / render lens, about 60 degrees)


@dataclass
class Plane:
    """A flat surface. coef = disparity line (a, b, c); normal = unit 3D direction; dist = cm from camera."""
    name: str
    coef: np.ndarray
    inliers: np.ndarray                     # bool mask over the image
    normal: np.ndarray | None = None
    dist: float | None = None
    axes: tuple[np.ndarray, np.ndarray] | None = None   # two in-plane directions (cm grid)
    extra: dict = field(default_factory=dict)


def intrinsics(w: int, h: int, focal_factor: float = FOCAL_FACTOR) -> np.ndarray:
    f = focal_factor * w
    return np.array([[f, 0, w / 2], [0, f, h / 2], [0, 0, 1]], dtype=np.float64)


def fit_plane_ransac(disp: np.ndarray, mask: np.ndarray, iters: int = 300, tol: float = 0.02,
                     rng: np.random.Generator | None = None, max_pts: int = 20000):
    """Fit d = a*x + b*y + c on pixels in mask. tol is relative to the disparity range. Returns (coef, inliers)."""
    rng = rng or np.random.default_rng(0)
    ys, xs = np.nonzero(mask)
    if len(xs) < 50:
        return None, np.zeros_like(mask, dtype=bool)
    d = disp[ys, xs].astype(np.float64)
    if len(xs) > max_pts:
        pick = rng.choice(len(xs), max_pts, replace=False)
        xs_s, ys_s, d_s = xs[pick], ys[pick], d[pick]
    else:
        xs_s, ys_s, d_s = xs, ys, d
    A = np.stack([xs_s, ys_s, np.ones_like(xs_s)], 1).astype(np.float64)
    span = float(np.percentile(d, 98) - np.percentile(d, 2)) or 1.0
    thr = tol * span
    best, best_n = None, -1
    for _ in range(iters):
        i = rng.choice(len(xs_s), 3, replace=False)
        try:
            coef = np.linalg.solve(A[i], d_s[i])
        except np.linalg.LinAlgError:
            continue
        n = int((np.abs(A @ coef - d_s) < thr).sum())
        if n > best_n:
            best, best_n = coef, n
    if best is None:
        return None, np.zeros_like(mask, dtype=bool)
    inl = np.abs(A @ best - d_s) < thr                      # refine with least squares on inliers
    best = np.linalg.lstsq(A[inl], d_s[inl], rcond=None)[0]
    full = np.zeros_like(mask, dtype=bool)
    Af = np.stack([xs, ys, np.ones_like(xs)], 1).astype(np.float64)
    full[ys[np.abs(Af @ best - d) < thr], xs[np.abs(Af @ best - d) < thr]] = True
    return best, full


def normal_from_coef(coef: np.ndarray, K: np.ndarray, shift: float) -> np.ndarray:
    """Disparity d - shift = s * (n . K^-1 p) / dist  ->  n is parallel to K^T (a, b, c - shift)."""
    l = np.array([coef[0], coef[1], coef[2] - shift])
    n = K.T @ l
    n = n / np.linalg.norm(n)
    return n


def solve_shift(floor: np.ndarray, walls: list[np.ndarray], K: np.ndarray, d_range: tuple[float, float]):
    """Pick the disparity offset that makes every wall closest to 90 degrees to the floor."""
    if not walls:
        return 0.0
    lo, hi = d_range
    best, best_err = 0.0, 1e9
    for t in np.linspace(lo - (hi - lo), lo, 400):        # offset must keep all disparities positive
        nf = normal_from_coef(floor, K, t)
        err = sum(abs(float(nf @ normal_from_coef(w, K, t))) for w in walls)
        if err < best_err:
            best, best_err = float(t), err
    return best


def plane_axes(normal: np.ndarray, up_hint: np.ndarray | None = None):
    """Two in-plane unit directions. For the floor: one along the camera's right, one going away.
    For a wall: one horizontal, one vertical (up_hint = floor normal)."""
    if up_hint is None:
        right = np.array([1.0, 0.0, 0.0])
        e1 = right - normal * (right @ normal)
    else:
        e1 = np.cross(up_hint, normal)
    e1 = e1 / np.linalg.norm(e1)
    e2 = np.cross(normal, e1)
    return e1, e2 / np.linalg.norm(e2)


def plane_uv(plane: Plane, K: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    """For every pixel, where it lands on the plane in cm (u, v). Pixels looking away get NaN."""
    h, w = shape
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float64)
    rays = np.stack([xs, ys, np.ones_like(xs)], -1) @ np.linalg.inv(K).T     # K^-1 p
    denom = rays @ plane.normal
    with np.errstate(divide="ignore", invalid="ignore"):
        z = plane.dist / denom
    z[(denom <= 1e-6)] = np.nan
    pts = rays * z[..., None]
    e1, e2 = plane.axes
    return np.stack([pts @ e1, pts @ e2], -1)


def solve_room(disp: np.ndarray, floor_mask: np.ndarray, wall_masks: list[np.ndarray],
               camera_height_cm: float = CAMERA_HEIGHT_CM, focal_factor: float = FOCAL_FACTOR):
    """Fit the floor and walls, fix the offset, and put everything in cm. Returns (K, floor Plane | None, [wall Planes])."""
    h, w = disp.shape
    K = intrinsics(w, h, focal_factor)
    fcoef, finl = fit_plane_ransac(disp, floor_mask)
    if fcoef is None:
        return K, None, []
    walls = []
    for i, m in enumerate(wall_masks):
        c, inl = fit_plane_ransac(disp, m, tol=0.03)
        if c is not None and inl.sum() > 400:
            walls.append(Plane(f"wall{i}", c, inl))
    vals = disp[floor_mask | np.any(wall_masks, 0)] if wall_masks else disp[floor_mask]
    shift = solve_shift(fcoef, [p.coef for p in walls], K, (float(vals.min()), float(vals.max())))

    floor = Plane("floor", fcoef, finl)
    # Camera coordinates: x right, y down, z forward. K^T l already points so that n . ray > 0 for
    # every visible pixel of the plane, i.e. for the floor it points down, and n . X = dist on the plane.
    floor.normal = normal_from_coef(fcoef, K, shift)
    floor.dist = camera_height_cm
    floor.axes = plane_axes(floor.normal)
    # The scale s links disparity to real distance; take it from the floor: (d - t) = s * (n . ray) / dist.
    l = K.T @ np.array([fcoef[0], fcoef[1], fcoef[2] - shift])
    s = camera_height_cm * np.linalg.norm(l)
    up = -floor.normal
    for p in walls:
        lw = K.T @ np.array([p.coef[0], p.coef[1], p.coef[2] - shift])
        nw = lw / np.linalg.norm(lw)
        p.normal = nw
        p.dist = s / np.linalg.norm(lw)
        p.axes = plane_axes(nw, up)
        p.extra["angle_to_floor_deg"] = float(np.degrees(np.arccos(min(1.0, abs(float(nw @ floor.normal))))))
    floor.extra["shift"] = shift
    return K, floor, walls
