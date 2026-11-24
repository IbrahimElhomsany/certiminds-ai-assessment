"""Makes the products customers can place in a room picture ("Place products" in the visualiser).

For every product it writes, into images/products/:
  <id>.glb  a small 3D model at true size in metres (the visualiser draws it into the room photo)
  <id>.jpg  a product picture rendered from that same model (shop cards and the "Add product" list)
and data/placeables.json: the list with each product's real size (W x D x H in cm, measured from the model),
how it is fitted (on the floor or on a wall) and whether we sell it.

Where the models come from (no other company's pictures or models are used):
  * Our bathroom products (WC, bidet, vanity, bath, shower set, mirror, shower screen) are modelled here,
    in the same style as the showroom displays (tools/showroom/build_showroom.py).
  * Furniture and plants, used to style a room, are free CC0 models from Poly Haven (polyhaven.com, commercial
    use allowed, no credit needed). Download them first: cd <dir> && python tools/showroom/fetch_assets.py <names>
    (the names are in DECOR below). They are marked "decor": shown for styling, not sold.

Run (Blender as a Python module, the same venv as the showroom):
    python tools/products/build_products.py --assets <dir> [--only SN003,decor_sofa] [--samples 64] [--keep]

Model orientation: the front of every product faces the viewer (+Z in the glTF file, three.js convention),
the origin is on the floor: the middle of the footprint for floor products, the middle of the back (the wall
line) for wall products. Wall products are already at their fitting height (a mirror's model starts 1.2 m up).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys

import bpy  # noqa: I001 (bpy must load before bmesh)
import bmesh
from mathutils import Vector

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ap = argparse.ArgumentParser()
ap.add_argument("--assets", required=True, help="folder with the Poly Haven glTF models")
ap.add_argument("--out", default=os.path.join(ROOT, "images", "products"))
ap.add_argument("--only", default="")
ap.add_argument("--samples", type=int, default=64)
ap.add_argument("--size", type=int, default=900, help="product picture size in pixels (square)")
ap.add_argument("--tex", type=int, default=1024, help="largest texture side kept in the .glb files")
ap.add_argument("--keep", action="store_true", help="products whose .glb and .jpg exist are only measured, not rendered again")
args = ap.parse_args(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:])
os.makedirs(args.out, exist_ok=True)
DATA = os.path.join(ROOT, "data", "placeables.json")

# ================================================================== materials and shapes
MATS = {}


def material(name, color=(0.8, 0.8, 0.8), rough=0.5, metal=0.0, coat=0.0, transmission=0.0, emission=0.0, alpha=None):
    if name in MATS:
        return MATS[name]
    m = bpy.data.materials.new(name); m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*color, 1); b.inputs["Roughness"].default_value = rough
    b.inputs["Metallic"].default_value = metal
    if coat:
        b.inputs["Coat Weight"].default_value = coat; b.inputs["Coat Roughness"].default_value = 0.04
    if transmission:
        b.inputs["Transmission Weight"].default_value = transmission; b.inputs["IOR"].default_value = 1.5
    if emission:
        b.inputs["Emission Color"].default_value = (*color, 1); b.inputs["Emission Strength"].default_value = emission
    if alpha is not None:                      # glTF has no real glass: a see-through material is the closest
        b.inputs["Alpha"].default_value = alpha
        if hasattr(m, "surface_render_method"):
            m.surface_render_method = "BLENDED"
    MATS[name] = m
    return m


chrome = material("chrome", (0.92, 0.92, 0.92), 0.06, 1.0)
matte_black = material("matte black metal", (0.02, 0.02, 0.02), 0.5, 0.3)
ceramic = material("sanitary ceramic", (0.93, 0.93, 0.92), 0.08, coat=1.0)
mirror_m = material("mirror", (0.96, 0.96, 0.96), 0.02, 1.0)
glass = material("glass", (0.9, 0.95, 0.95), 0.02, alpha=0.18)
oak = material("oak", (0.22, 0.135, 0.07), 0.45)
quartz = material("white quartz", (0.88, 0.88, 0.86), 0.2, coat=0.5)
led = material("warm led", (1.0, 0.88, 0.72), 0.5, emission=6)
fabric = material("sofa fabric", (0.55, 0.52, 0.47), 0.95)
fabric_dark = material("sofa fabric shade", (0.47, 0.44, 0.4), 0.95)


def apply_scale(o):
    bpy.ops.object.select_all(action="DESELECT"); o.select_set(True); bpy.context.view_layer.objects.active = o
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)


def box(name, size, loc, mat, bevel=0.003, rot=(0, 0, 0), segments=2):
    bpy.ops.mesh.primitive_cube_add(size=1, location=loc, rotation=rot)
    o = bpy.context.object; o.name = name; o.scale = size; apply_scale(o)
    if bevel:
        mod = o.modifiers.new("b", "BEVEL"); mod.width = bevel; mod.segments = segments
    o.data.materials.append(mat)
    return o


def cylinder(name, r, depth, loc, mat, rot=(0, 0, 0), n=48):
    bpy.ops.mesh.primitive_cylinder_add(vertices=n, radius=r, depth=depth, location=loc, rotation=rot)
    o = bpy.context.object; o.name = name; o.data.materials.append(mat); smooth(o)
    return o


def smooth(o, angle=35):
    bpy.ops.object.select_all(action="DESELECT"); o.select_set(True); bpy.context.view_layer.objects.active = o
    bpy.ops.object.shade_auto_smooth(angle=math.radians(angle))


def loft(name, rings, mat, cap_bottom=True, n=72, p=2.6, subsurf=True):
    """A shape from superellipse rings [(a, b, z), ...] (a, b = half sizes): baths, basins, WC bowls."""
    me = bpy.data.meshes.new(name); bm = bmesh.new()
    vs = []
    for a, b, z in rings:
        ring = []
        for i in range(n):
            t = 2 * math.pi * i / n
            c, s = math.cos(t), math.sin(t)
            ring.append(bm.verts.new((a * math.copysign(abs(c) ** (2 / p), c), b * math.copysign(abs(s) ** (2 / p), s), z)))
        vs.append(ring)
    for r0, r1 in zip(vs, vs[1:]):
        for i in range(n):
            bm.faces.new((r0[i], r0[(i + 1) % n], r1[(i + 1) % n], r1[i]))
    if cap_bottom:
        bm.faces.new(list(reversed(vs[0])))
        bm.faces.new(vs[-1])
    bm.normal_update(); bm.to_mesh(me); bm.free()
    o = bpy.data.objects.new(name, me); bpy.context.collection.objects.link(o)
    o.data.materials.append(mat)
    for poly in o.data.polygons:
        poly.use_smooth = True
    if subsurf:
        sub = o.modifiers.new("s", "SUBSURF"); sub.levels = 1; sub.render_levels = 1
    return o


# ================================================================== our bathroom products
# Built with the wall at y = 0 and the product reaching into the room along -y (its front faces -y, which the
# glTF export turns into +Z, towards the viewer).

def wc():
    loft("wall-hung wc", [(0.17, 0.25, 0.32), (0.18, 0.27, 0.36), (0.185, 0.28, 0.42), (0.18, 0.27, 0.43)], ceramic, p=3.0).location = (0, -0.29, 0)
    box("seat", (0.36, 0.5, 0.025), (0, -0.32, 0.445), ceramic, bevel=0.012)
    box("flush plate", (0.24, 0.012, 0.16), (0, -0.006, 1.05), matte_black, bevel=0.004)


def bidet():
    loft("wall-hung bidet", [(0.16, 0.24, 0.32), (0.175, 0.26, 0.36), (0.18, 0.27, 0.42), (0.175, 0.26, 0.43)], ceramic, p=3.0).location = (0, -0.28, 0)
    loft("bidet bowl", [(0.12, 0.17, 0.33), (0.15, 0.22, 0.43)], material("bowl shade", (0.86, 0.86, 0.85), 0.1, coat=1.0),
         cap_bottom=False, p=2.6, subsurf=False).location = (0, -0.31, 0.003)
    box("mixer", (0.03, 0.03, 0.09), (0, -0.07, 0.48), chrome, bevel=0.008)
    box("spout", (0.025, 0.09, 0.022), (0, -0.11, 0.515), chrome, bevel=0.006)


def vanity():
    box("vanity", (1.0, 0.48, 0.42), (0, -0.25, 0.68), oak, bevel=0.006)
    box("vanity top", (1.02, 0.5, 0.025), (0, -0.25, 0.9), quartz, bevel=0.003)
    for dx in (-0.25, 0.25):
        box("handle", (0.2, 0.015, 0.015), (dx, -0.495, 0.84), matte_black, bevel=0.003)
    loft("countertop basin", [(0.2, 0.15, 0.912), (0.26, 0.19, 0.95), (0.27, 0.2, 1.04), (0.255, 0.185, 1.04),
                              (0.24, 0.17, 0.98), (0.1, 0.07, 0.93)], ceramic, cap_bottom=False, p=2.4).location = (0, -0.27, 0)
    box("tap", (0.03, 0.03, 0.22), (0, -0.08, 1.03), chrome, bevel=0.006)
    box("spout", (0.028, 0.14, 0.028), (0, -0.14, 1.13), chrome, bevel=0.006)


def bath():
    rings = [(0.66, 0.27, 0.0), (0.74, 0.32, 0.12), (0.83, 0.38, 0.42), (0.86, 0.4, 0.58), (0.86, 0.4, 0.6),
             (0.835, 0.375, 0.6), (0.82, 0.36, 0.56), (0.76, 0.31, 0.28), (0.66, 0.24, 0.1), (0.45, 0.15, 0.08)]
    loft("freestanding bath", rings, ceramic, p=2.3)


def shower_set():
    box("shower mixer", (0.36, 0.06, 0.06), (0, -0.04, 1.05), chrome, bevel=0.02)
    for dx in (-0.19, 0.19):
        cylinder("knob", 0.032, 0.05, (dx, -0.04, 1.05), chrome, rot=(0, math.pi / 2, 0))
    box("slide rail", (0.022, 0.022, 0.9), (0.13, -0.03, 1.55), chrome, bevel=0.005)
    box("hand shower", (0.045, 0.045, 0.24), (0.13, -0.06, 1.72), chrome, bevel=0.015)
    box("riser", (0.026, 0.026, 0.92), (-0.02, -0.03, 1.56), chrome, bevel=0.006)
    box("shower arm", (0.025, 0.34, 0.025), (-0.02, -0.19, 2.02), chrome, bevel=0.005)
    cylinder("rain head", 0.14, 0.014, (-0.02, -0.34, 2.0), chrome, n=64)


def led_mirror():
    cylinder("mirror frame", 0.4, 0.03, (0, -0.02, 1.6), matte_black, rot=(math.pi / 2, 0, 0), n=96)
    cylinder("mirror glass", 0.385, 0.006, (0, -0.036, 1.6), mirror_m, rot=(math.pi / 2, 0, 0), n=96)
    bpy.ops.mesh.primitive_torus_add(major_radius=0.392, minor_radius=0.004, location=(0, -0.006, 1.6), rotation=(math.pi / 2, 0, 0))
    o = bpy.context.object; o.name = "back light"; o.data.materials.append(led)


def shower_screen():
    box("glass panel", (1.2, 0.008, 2.0), (0, -0.6, 1.0), glass, bevel=0.002)
    box("floor profile", (1.2, 0.03, 0.03), (0, -0.6, 0.015), matte_black, bevel=0.002)
    box("wall profile", (0.03, 0.03, 2.0), (-0.6, -0.6, 1.0), matte_black, bevel=0.002)
    box("support bar", (0.02, 0.6, 0.02), (0.55, -0.3, 1.99), matte_black, bevel=0.003)


def modern_sofa():
    """A plain modern three-seat sofa (we have no CC0 one in a modern style)."""
    w, d = 2.2, 0.92
    box("base", (w, d, 0.2), (0, 0, 0.2), fabric_dark, bevel=0.03, segments=4)
    for side in (-1, 1):
        box("arm", (0.16, d, 0.42), (side * (w / 2 - 0.08), 0, 0.42), fabric, bevel=0.05, segments=5)
    box("back", (w - 0.32, 0.2, 0.5), (0, d / 2 - 0.1, 0.55), fabric, bevel=0.06, segments=5)
    cw = (w - 0.32) / 3
    for i in range(3):
        x = -((w - 0.32) / 2) + cw * (i + 0.5)
        box("seat cushion", (cw - 0.01, d - 0.24, 0.14), (x, -0.1, 0.37), fabric, bevel=0.05, segments=5)
        box("back cushion", (cw - 0.03, 0.16, 0.38), (x, d / 2 - 0.27, 0.6), fabric, bevel=0.07, segments=5, rot=(math.radians(-10), 0, 0))
    for sx in (-1, 1):
        for sy in (-1, 1):
            box("leg", (0.035, 0.035, 0.1), (sx * (w / 2 - 0.08), sy * (d / 2 - 0.08), 0.05), matte_black, bevel=0.004)


# id, name, built by, how fitted ("floor" or "wall"), category, sold (our SKU) or decor
OURS = [
    ("SN003", "Nova Wall-Hung WC Set", wc, "wall", "wc"),
    ("SN004", "Luna Oak Vanity Set 100", vanity, "wall", "vanity"),
    ("SN002", "Aria Shower Set", shower_set, "wall", "shower"),
    ("SN005", "Oval Freestanding Bath 172", bath, "floor", "bath"),
    ("SN006", "Nova Wall-Hung Bidet Set", bidet, "wall", "bidet"),
    ("SN007", "Halo Round LED Mirror 80", led_mirror, "wall", "mirror"),
    ("SN008", "Clear Walk-In Shower Screen 120", shower_screen, "floor", "screen"),
]
# Poly Haven CC0 models (real size in metres already), and our own modern sofa
DECOR = [
    ("decor_sofa", "Modern three-seat sofa", modern_sofa, "floor", "sofa"),
    ("decor_loveseat", "Leather loveseat", "sofa_02", "floor", "sofa"),
    ("decor_classic_sofa", "Classic sofa", "Sofa_01", "floor", "sofa"),
    ("decor_lounge_chair", "Lounge chair", "mid_century_lounge_chair", "floor", "chair"),
    ("decor_armchair", "Wood and leather armchair", "modern_arm_chair_01", "floor", "chair"),
    ("decor_classic_armchair", "Classic armchair", "ArmChair_01", "floor", "chair"),
    ("decor_ottoman", "Leather ottoman", "Ottoman_01", "floor", "chair"),
    ("decor_coffee_table", "Round coffee table", "coffee_table_round_01", "floor", "table"),
    ("decor_coffee_table_2", "Concrete coffee table", "modern_coffee_table_01", "floor", "table"),
    ("decor_side_table", "Oak side table", "side_table_01", "floor", "table"),
    ("decor_sideboard", "Slatted sideboard", "modern_wooden_cabinet", "floor", "storage"),
    ("decor_wall_mirror", "Arched wall mirror", "ornate_mirror_01", "wall", "mirror"),
    ("decor_plant", "Plant in clay bowl", "potted_plant_01", "floor", "plant"),
    ("decor_plant_2", "Leafy plant in clay pot", "potted_plant_02", "floor", "plant"),
    ("decor_plant_3", "Succulent in white pot", "potted_plant_04", "floor", "plant"),
]


def clear():
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o, do_unlink=True)
    for coll in (bpy.data.meshes, bpy.data.images, bpy.data.lights, bpy.data.cameras):
        for x in list(coll):
            if x.users == 0:
                coll.remove(x)


MAX_TRIS = 120_000                             # keeps each .glb small enough for phones


def import_polyhaven(name, mount):
    path = os.path.join(args.assets, name, name + ".gltf")
    bpy.ops.import_scene.gltf(filepath=path)
    for img in bpy.data.images:                # keep the files small for phones
        if img.size[0] > args.tex or img.size[1] > args.tex:
            k = args.tex / max(img.size)
            img.scale(max(1, int(img.size[0] * k)), max(1, int(img.size[1] * k)))
    meshes = [o for o in bpy.data.objects if o.type == "MESH"]
    tris = sum(len(p.vertices) - 2 for o in meshes for p in o.data.polygons)
    if tris > MAX_TRIS:                        # dense plants: fewer triangles, same look at room-photo size
        for o in meshes:
            d = o.modifiers.new("lighter", "DECIMATE"); d.ratio = MAX_TRIS / tris
    pts = [o.matrix_world @ Vector(c) for o in meshes for c in o.bound_box]
    if mount == "wall":                        # a wall mirror: hang it with its back on the wall, centre 1.6 m up
        lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
        hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
        thin_x = (hi.x - lo.x) < (hi.y - lo.y)
        if thin_x:                             # its face looks along x: turn it to face -y
            for o in meshes:
                if o.parent is None:
                    o.rotation_euler.z += math.pi / 2
            bpy.context.view_layer.update()
            pts = [o.matrix_world @ Vector(c) for o in meshes for c in o.bound_box]
        hi_y = max(p.y for p in pts); zmid = (min(p.z for p in pts) + max(p.z for p in pts)) / 2
        cx = (min(p.x for p in pts) + max(p.x for p in pts)) / 2
        shift = Vector((-cx, -hi_y, 1.6 - zmid))
    else:
        cx = (min(p.x for p in pts) + max(p.x for p in pts)) / 2; cy = (min(p.y for p in pts) + max(p.y for p in pts)) / 2
        shift = Vector((-cx, -cy, -min(p.z for p in pts)))
    for o in bpy.data.objects:
        if o.parent is None:
            o.location += shift


def bounds():
    bpy.context.view_layer.update()
    pts = [o.matrix_world @ Vector(c) for o in bpy.data.objects if o.type == "MESH" for c in o.bound_box]
    lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
    hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    return lo, hi


def studio(lo, hi, mount):
    """Soft studio light, a floor (and for wall products a wall) that only catch the shadow, and a 3/4 camera."""
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"; sc.cycles.device = "CPU"; sc.cycles.samples = args.samples; sc.cycles.use_denoising = True
    sc.view_settings.view_transform = "AgX"; sc.view_settings.look = "AgX - Medium High Contrast"
    sc.render.film_transparent = True
    sc.render.resolution_x = sc.render.resolution_y = args.size
    w = sc.world or bpy.data.worlds.new("w"); sc.world = w; w.use_nodes = True
    w.node_tree.nodes["Background"].inputs["Color"].default_value = (0.82, 0.82, 0.82, 1)
    w.node_tree.nodes["Background"].inputs["Strength"].default_value = 0.35
    c = (lo + hi) / 2; size = hi - lo
    big = max(size.x, size.y, size.z)
    bpy.ops.mesh.primitive_plane_add(size=40, location=(0, 0, 0)); bpy.context.object.is_shadow_catcher = True
    if mount == "wall":
        bpy.ops.mesh.primitive_plane_add(size=40, location=(0, 0.001, 0), rotation=(math.pi / 2, 0, 0))
        bpy.context.object.is_shadow_catcher = True
    for loc, energy, s in [((-2.5, -3.0, 4.0), 110, 3.0), ((3.0, -1.5, 2.5), 35, 3.0), ((0.5, -3.0, 0.8), 15, 4.0)]:
        bpy.ops.object.light_add(type="AREA", location=loc); L = bpy.context.object
        L.data.energy = energy; L.data.size = s
        L.rotation_euler = (Vector(c) - Vector(loc)).to_track_quat("-Z", "Y").to_euler()
    bpy.ops.object.camera_add(); cam = bpy.context.object; sc.camera = cam
    cam.data.lens = 70
    yaw, pitch = math.radians(-28), math.radians(14)
    d = Vector((math.sin(yaw) * math.cos(pitch), -math.cos(yaw) * math.cos(pitch), math.sin(pitch)))
    radius = 0.5 * size.length
    dist = radius / math.sin(math.atan(18 / 70)) * 0.9
    cam.location = c + d * dist
    cam.rotation_euler = (-d).to_track_quat("-Z", "Y").to_euler()


def export(pid):
    """Product picture (on our light grey) and the .glb. Lights, camera and the shadow floor stay out of the .glb."""
    from PIL import Image
    png = os.path.join(args.out, f"_{pid}.png")
    bpy.context.scene.render.filepath = png
    bpy.ops.render.render(write_still=True)
    im = Image.open(png).convert("RGBA")
    bg = Image.new("RGBA", im.size, (244, 243, 241, 255)); bg.alpha_composite(im)
    bg.convert("RGB").save(os.path.join(args.out, f"{pid}.jpg"), quality=86)
    os.remove(png)
    bpy.ops.object.select_all(action="DESELECT")
    for o in bpy.data.objects:
        if o.type == "MESH" and not o.is_shadow_catcher:          # the shadow floor and wall stay out
            o.select_set(True)
    bpy.ops.export_scene.gltf(filepath=os.path.join(args.out, f"{pid}.glb"), export_format="GLB", use_selection=True,
                              export_apply=True, export_yup=True, export_image_format="JPEG", export_jpeg_quality=80,
                              export_lights=False, export_cameras=False)


keep = set(filter(None, args.only.split(",")))
rows = {r["id"]: r for r in json.load(open(DATA))} if os.path.exists(DATA) else {}
for pid, name, src, mount, category in OURS + DECOR:
    if keep and pid not in keep:
        continue
    clear()
    if callable(src):
        src()
    else:
        import_polyhaven(src, mount)
    lo, hi = bounds()
    size_cm = [round((hi.x - lo.x) * 100), round((hi.y - lo.y) * 100), round((hi.z - lo.z) * 100)]
    done = all(os.path.exists(os.path.join(args.out, f"{pid}.{x}")) for x in ("glb", "jpg"))
    if not (args.keep and done):
        studio(lo, hi, mount)
        export(pid)
    sold = pid.startswith("SN")
    rows[pid] = {"id": pid, "name": name, "category": category, "mount": mount, "sold": sold,
                 "size_cm": size_cm, "lift_cm": round(lo.z * 100),
                 "model": f"/images/products/{pid}.glb", "image": f"/images/products/{pid}.jpg",
                 "source": "Nova Studio 3D model" if callable(src) else f"Poly Haven CC0 ({src})"}
    print("product", pid, size_cm, flush=True)
    with open(DATA, "w") as f:                 # saved after each product, so a stopped run keeps what it made
        json.dump([rows[k] for k in [p[0] for p in OURS + DECOR] if k in rows], f, indent=1)

order = [p[0] for p in OURS + DECOR]
with open(DATA, "w") as f:
    json.dump([rows[k] for k in order if k in rows], f, indent=1)
print("products done")
