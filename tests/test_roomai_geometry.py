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
