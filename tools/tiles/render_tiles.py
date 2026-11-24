"""Product pictures for our tiles: images/<SKU>.jpg, a photo-like render of the real tiles laid with grout.

Each picture is made from that tile's face (images/tex/<SKU>.jpg, see make_faces.py) at its real size in cm, so a
customer sees the true shape (a 20 x 120 plank, a 10 x 30 subway tile...), the grout and the finish (polished and
gloss tiles catch the light, matt and textured ones don't). Blender as a Python module (same venv as the showroom):
    python tools/tiles/render_tiles.py [--only CN016] [--samples 48] [--size 900]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys

import bpy  # noqa: I001 (bpy must load before bmesh)
import bmesh
from mathutils import Vector

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ap = argparse.ArgumentParser()
ap.add_argument("--only", default="")
ap.add_argument("--samples", type=int, default=48)
ap.add_argument("--size", type=int, default=900)
args = ap.parse_args(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:])

FINISH = {  # roughness, bump strength
    "polished": (0.05, 0.0), "gloss": (0.09, 0.02), "matt": (0.5, 0.12), "textured": (0.72, 0.35)}
BOND = {"CN003": 1 / 3, "CN016": 1 / 3, "CN007": 0.5, "CN008": 0.5, "CN010": 0.5, "CN009": 0.5}
GROUT = {"dark": (0.11, 0.11, 0.11), "light": (0.74, 0.73, 0.70), "warm": (0.48, 0.42, 0.36)}
GROUT_OF = {"CN005": "dark", "CN011": "dark", "CN014": "dark", "CN016": "dark", "CN003": "warm", "CN009": "warm",
            "CN012": "warm", "CN002": "warm"}


def reset():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def material(sku: str, finish: str) -> bpy.types.Material:
    rough, bump = FINISH.get(finish, (0.5, 0.1))
    m = bpy.data.materials.new(sku); m.use_nodes = True
    nt = m.node_tree; bsdf = nt.nodes["Principled BSDF"]
    img = nt.nodes.new("ShaderNodeTexImage")
    img.image = bpy.data.images.load(os.path.join(ROOT, "images", "tex", f"{sku}.jpg"))
    nt.links.new(img.outputs["Color"], bsdf.inputs["Base Color"])
    bsdf.inputs["Roughness"].default_value = rough
    if bump:
        b = nt.nodes.new("ShaderNodeBump"); b.inputs["Strength"].default_value = bump; b.inputs["Distance"].default_value = 0.002
        nt.links.new(img.outputs["Color"], b.inputs["Height"]); nt.links.new(b.outputs["Normal"], bsdf.inputs["Normal"])
    if finish == "polished":
        bsdf.inputs["Coat Weight"].default_value = 0.6; bsdf.inputs["Coat Roughness"].default_value = 0.02
    return m


def grout_material(kind: str) -> bpy.types.Material:
    m = bpy.data.materials.new("grout"); m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*GROUT[kind], 1); b.inputs["Roughness"].default_value = 0.9
    return m


def lay(sku: str, L: float, S: float, F: float, joint: float, offset: float):
    """Tiles (long side left to right) covering an F x F cm square, as one mesh. Each tile shows a random strip of
    the face picture (like the visualiser) so the tiles differ a little, and square tiles get a random quarter turn."""
    rng = random.Random(sku)
    bm = bmesh.new(); uv = bm.loops.layers.uv.new("UVMap")
    t = 0.9                                                     # tile thickness, cm
    crop = S / L
    rows = int(math.ceil(2.2 * F / (S + joint))) + 2
    for r in range(-rows // 2, rows // 2 + 1):
        y0 = r * (S + joint)
        shift = ((r * offset) % 1.0) * (L + joint)
        cols = int(math.ceil(2.2 * F / (L + joint))) + 2
        for c in range(-cols // 2 - 1, cols // 2 + 1):
            x0 = c * (L + joint) + shift
            k = 0.01                                            # cm -> m
            v = [bm.verts.new(((x0 + dx) * k, (y0 + dy) * k, z * k)) for z in (0, t) for dx, dy in ((0, 0), (L, 0), (L, S), (0, S))]
            faces = [bm.faces.new((v[4], v[5], v[6], v[7]))]   # top
            for a, b in ((0, 1), (1, 2), (2, 3), (3, 0)):
                bm.faces.new((v[a], v[b], v[b + 4], v[a + 4]))
            off = rng.random() * (1 - crop)
            turn = rng.randrange(4) if abs(L - S) < 1e-6 else rng.choice((0, 2))
            corners = [(0, off), (1, off), (1, off + crop), (0, off + crop)]
            corners = corners[turn:] + corners[:turn]
            for loop, co in zip(faces[0].loops, corners):
                loop[uv].uv = co
    me = bpy.data.meshes.new("tiles"); bm.to_mesh(me); bm.free()
    ob = bpy.data.objects.new("tiles", me); bpy.context.collection.objects.link(ob)
    bev = ob.modifiers.new("bevel", "BEVEL"); bev.width = 0.0012; bev.segments = 3; bev.limit_method = "ANGLE"
    return ob


def scene(F: float):
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"; sc.cycles.device = "CPU"; sc.cycles.samples = args.samples; sc.cycles.use_denoising = True
    sc.view_settings.view_transform = "AgX"; sc.view_settings.look = "AgX - Medium High Contrast"
    sc.render.resolution_x = sc.render.resolution_y = args.size
    w = bpy.data.worlds.new("w"); sc.world = w; w.use_nodes = True
    nt = w.node_tree; bg = nt.nodes["Background"]; bg.inputs["Strength"].default_value = 0.3
    # a soft sky: light above, darker towards the horizon, so polished tiles show a smooth sheen
    tc, sep, ramp = nt.nodes.new("ShaderNodeTexCoord"), nt.nodes.new("ShaderNodeSeparateXYZ"), nt.nodes.new("ShaderNodeValToRGB")
    nt.links.new(tc.outputs["Generated"], sep.inputs[0]); nt.links.new(sep.outputs["Z"], ramp.inputs["Fac"])
    ramp.color_ramp.elements[0].position = 0.0; ramp.color_ramp.elements[0].color = (0.25, 0.25, 0.26, 1)
    ramp.color_ramp.elements[1].position = 0.6; ramp.color_ramp.elements[1].color = (1.0, 1.0, 0.98, 1)
    nt.links.new(ramp.outputs["Color"], bg.inputs["Color"])
    m = F / 100
    # key light on the camera side: its mirror image falls outside the picture, so no hard white square
    for loc, energy, size in [((-0.7 * m, -1.2 * m, 1.9 * m), 110, 1.4)]:
        bpy.ops.object.light_add(type="AREA", location=loc); Lt = bpy.context.object
        Lt.data.energy = energy * m * m; Lt.data.size = size * m
        Lt.rotation_euler = (Vector((0, 0, 0)) - Vector(loc)).to_track_quat("-Z", "Y").to_euler()
    bpy.ops.object.camera_add(); cam = bpy.context.object; sc.camera = cam
    cam.data.lens = 50
    tilt = math.radians(32)                                      # from straight down
    d = Vector((0, -math.sin(tilt), math.cos(tilt)))
    dist = (m / 2) / math.tan(math.atan(18 / 50)) * 1.05
    cam.location = d * dist
    cam.rotation_euler = (-d).to_track_quat("-Z", "Y").to_euler()


def render(row: dict):
    sku, L, S = row["sku"], max(row["tile_width_cm"], row["tile_length_cm"]), min(row["tile_width_cm"], row["tile_length_cm"])
    F = min(210, max(60, 1.7 * L))
    joint = 0.15 if sku == "CN013" else 0.2 if row["finish"] == "polished" else 0.3
    reset()
    ob = lay(sku, L, S, F, joint, BOND.get(sku, 0.0))
    ob.data.materials.append(material(sku, row["finish"]))
    bpy.ops.mesh.primitive_plane_add(size=F / 100 * 5, location=(0, 0, 0.003))
    bpy.context.object.data.materials.append(grout_material(GROUT_OF.get(sku, "light")))
    bpy.context.object.rotation_euler = (0, 0, 0)
    scene(F)
    png = os.path.join(ROOT, "images", f"_{sku}.png")
    bpy.context.scene.render.filepath = png
    bpy.ops.render.render(write_still=True)
    from PIL import Image
    Image.open(png).convert("RGB").save(os.path.join(ROOT, "images", f"{sku}.jpg"), quality=88)
    os.remove(png)
    print("rendered", sku, flush=True)


rows = json.load(open(os.path.join(ROOT, "data", "catalog.json")))
only = set(filter(None, args.only.split(",")))
for row in rows:
    if not only or row["sku"] in only:
        render(row)
