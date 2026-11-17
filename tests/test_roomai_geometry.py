"""Room geometry: a made-up room with a known floor and back wall must come back at the right size."""
import numpy as np

from backend.roomai.geometry import intrinsics, plane_uv, solve_room

W, H = 400, 300
HEIGHT = 145.0       # camera height in cm
WALL_Z = 600.0       # back wall 6 m in front of the camera


def synthetic_room(tilt_deg=8.0, shift=0.4, scale=150.0):
    """Disparity image of a camera tilted down a little, looking at a floor and a back wall."""
    K = intrinsics(W, H)
    ys, xs = np.mgrid[0:H, 0:W].astype(float)
    rays = np.stack([xs, ys, np.ones_like(xs)], -1) @ np.linalg.inv(K).T
    t = np.radians(tilt_deg)                                   # rotate rays: camera pitched down
    R = np.array([[1, 0, 0], [0, np.cos(t), np.sin(t)], [0, -np.sin(t), np.cos(t)]])
    world = rays @ R.T                                         # world: y down, z forward, floor y = HEIGHT
    with np.errstate(divide="ignore", invalid="ignore"):
        s_floor = np.where(world[..., 1] > 0, HEIGHT / world[..., 1], np.inf)
        s_wall = np.where(world[..., 2] > 0, WALL_Z / world[..., 2], np.inf)
    s = np.minimum(s_floor, s_wall)
    pts = rays * s[..., None]                                  # camera-frame points
    z = pts[..., 2]
    disp = scale / z + shift
    floor = s_floor < s_wall
    wall = ~floor
    return disp, floor, wall, world * s[..., None]


def test_floor_comes_back_at_real_size():
    disp, floor, wall, world = synthetic_room()
    K, fl, walls = solve_room(disp, floor, [wall], camera_height_cm=HEIGHT)
    assert fl is not None and len(walls) == 1
    assert abs(walls[0].extra["angle_to_floor_deg"] - 90) < 2
    uv = plane_uv(fl, K, disp.shape)
    # Two floor pixels: their distance in cm on the recovered floor must match the true distance.
    ys, xs = np.nonzero(floor)
    a, b = (ys[0], xs[0]), (ys[-1], xs[-1])
    true = np.linalg.norm(world[a][[0, 2]] - world[b][[0, 2]])
    got = np.linalg.norm(uv[a] - uv[b])
    assert abs(got - true) / true < 0.05


def test_wall_distance_is_found():
    disp, floor, wall, _ = synthetic_room(tilt_deg=5)
    _, fl, walls = solve_room(disp, floor, [wall], camera_height_cm=HEIGHT)
    # Distance from camera to the wall plane along its normal (the camera is tilted, so not exactly WALL_Z).
    assert abs(walls[0].dist - WALL_Z) / WALL_Z < 0.08


def test_no_floor_returns_none():
    disp = np.ones((50, 60))
    K, fl, walls = solve_room(disp, np.zeros_like(disp, bool), [])
    assert fl is None and walls == []


def test_service_square_matches_the_floor():
    """The 100 x 100 cm square the service sends to the browser must stretch tiles exactly like the AI's own cm grid."""
    from backend.roomai.service import area_m2, square_on_plane
    disp, floor, wall, world = synthetic_room()
    K, fl, _ = solve_room(disp, floor, [wall], camera_height_cm=HEIGHT)
    uv = plane_uv(fl, K, disp.shape)
    quad = square_on_plane(uv, floor, fl, K, 1.0)
    assert quad is not None
    # homography square (cm) -> photo px, then back for some floor pixels
    A, b = [], []
    for (x, y), (u, v) in zip([[0, 0], [100, 0], [100, 100], [0, 100]], quad):
        A += [[x, y, 1, 0, 0, 0, -u * x, -u * y], [0, 0, 0, x, y, 1, -v * x, -v * y]]
        b += [u, v]
    Hm = np.append(np.linalg.solve(np.array(A), np.array(b)), 1).reshape(3, 3)
    ys, xs = np.nonzero(floor)
    pick = np.linspace(0, len(xs) - 1, 50).astype(int)
    p = np.linalg.inv(Hm) @ np.stack([xs[pick], ys[pick], np.ones(50)])
    got = (p[:2] / p[2]).T
    want = uv[ys[pick], xs[pick]]
    d = got - want
    assert np.abs(d - d.mean(0)).max() < 0.5          # same grid, only shifted
    assert area_m2(uv, floor, 1.0) > 1
