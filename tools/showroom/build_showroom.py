"""Builds the Ceramica Nova showroom in Blender and renders the 360° pictures for the shop's home page.

Why: a browser can only fake light, so the live 3D showroom looked like a game. Blender's Cycles renderer
works out real light (soft shadows, light bouncing off walls, reflections in glossy tiles), so each 360°
picture looks like a photo. The visitor glides from spot to spot and looks around (frontend/assets/showroom.js).

The hall is laid out like a real tile and bathroom showroom: a wall of framed tile samples, freestanding baths on
tiled plinths, shower sets on display boards, a gallery of big slabs, a lit WC and basin podium, tiled bathroom wall
sections and an outdoor corner. Every display uses our own tiles at their real size (images/<SKU>.jpg + data/catalog.json). Furniture, plants
and the sky are free CC0 assets from Poly Haven (polyhaven.com, no credit needed, commercial use allowed),
fetched with tools/showroom/fetch_assets.py. Bathtub, basins, WC and shower are modelled here.

Run (Blender as a Python module: pip install bpy==4.5.0 on Python 3.11):
    python tools/showroom/build_showroom.py --assets <dir> [--res 4096] [--samples 96] [--only s1,s2]
Writes images/showroom/<spot>.jpg (+ _s.jpg, 2048 wide for phones) and images/showroom/spots.json.
With --views it renders the flat home-page pictures instead (images/home/show_<view>.jpg + views.json with the dots);
then run tools/showroom/home_scenes.py to make data/homepage.json.
A new or changed tile needs a new render (about 20 to 30 minutes per spot on 4 processor cores).
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
from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ap = argparse.ArgumentParser()
ap.add_argument("--assets", required=True)
ap.add_argument("--out", default=os.path.join(ROOT, "images", "showroom"))
ap.add_argument("--res", type=int, default=4096)
ap.add_argument("--samples", type=int, default=96)
ap.add_argument("--only", default="")
ap.add_argument("--still", default="", help="render a normal photo from x,y,z,yaw_deg instead (for checks)")
ap.add_argument("--views", action="store_true", help="render the flat display photos for the home page instead")
ap.add_argument("--home", default=os.path.join(ROOT, "images", "home"))
ap.add_argument("--pct", type=int, default=100, help="render at this percent of full size (quick previews)")
args = ap.parse_args(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:])
IMG = os.path.join(ROOT, "images")
TEX = os.path.join(args.out, "_tex")
os.makedirs(TEX, exist_ok=True)
CAT = {p["sku"]: p for p in json.load(open(os.path.join(ROOT, "data", "catalog.json")))}
FONT = "/usr/share/fonts/opentype/inter/Inter-SemiBold.otf"
FONT_L = "/usr/share/fonts/opentype/inter/Inter-Regular.otf"

W, D, H = 18.0, 12.0, 3.6            # the hall, metres: x -9..9, y -6 (back wall) .. 6 (shop front)
EYE = 1.6
FRONT = D / 2


# ================================================================== tile textures
def tile_size(sku):
    p = CAT[sku]
    return p["tile_width_cm"], p["tile_length_cm"]


def glossy(sku):
    return CAT[sku]["finish"] in ("polished", "gloss", "glossy", "lappato")


def lay(sku, wcm, hcm, k=4, grout=(212, 208, 200), turn=False):
    """Colour image + grout mask for tiles laid at real size over wcm x hcm cm (k pixels per cm)."""
    key = f"{sku}_{int(wcm)}x{int(hcm)}_{k}{'_t' if turn else ''}" + ("" if grout == (212, 208, 200) else "_g%02x%02x%02x" % grout)
    pc, pm = os.path.join(TEX, key + ".jpg"), os.path.join(TEX, key + "_g.png")
    if os.path.exists(pc) and os.path.exists(pm):
        return pc, pm
    tw, th = tile_size(sku)
    if turn or (th > tw and th / tw < 2.5):           # lay rectangles landscape on walls, as showrooms do
        tw, th = th, tw
    im = Image.open(os.path.join(IMG, sku + ".jpg")).convert("RGB")
    Wp, Hp = int(wcm * k), int(hcm * k)
    c = Image.new("RGB", (Wp, Hp)); m = Image.new("L", (Wp, Hp), 0)
    long = max(tw, th) / min(tw, th)
    n, r = 0, 0
    while r * th < hcm:
        shift = (r % 3) * tw / 3 if long >= 2.5 else ((r % 2) * tw / 2 if long >= 1.9 else 0)
        col = -1
        while col * tw < wcm:
            x0, y0 = int((col * tw + shift) * k), int(r * th * k)
            if long >= 2.5:
                band = im.width / long
                bands = max(1, int(im.height // band))
                b = (n * 5 + r) % bands
                piece = im.crop((0, int(b * band), im.width, int((b + 1) * band)))
                if th > tw:
                    piece = piece.rotate(90, expand=True)
            else:
                piece = im.rotate(180) if (n * 7 + r * 3) % 4 == 1 else im
            c.paste(piece.resize((max(1, int(tw * k)), max(1, int(th * k))), Image.LANCZOS), (x0, y0))
            n += 1; col += 1
        r += 1
    d, dm = ImageDraw.Draw(c), ImageDraw.Draw(m)
    g = max(1, int(round(0.15 * k)))
    r = 0
    while r * th <= hcm + th:
        y = int(r * th * k)
        d.rectangle([0, y - g, Wp, y + g], fill=grout); dm.rectangle([0, y - g, Wp, y + g], fill=255)
        shift = (r % 3) * tw / 3 if long >= 2.5 else ((r % 2) * tw / 2 if long >= 1.9 else 0)
        col = -1
        while col * tw <= wcm + tw:
            x = int((col * tw + shift) * k)
            d.rectangle([x - g, y, x + g, int((r + 1) * th * k)], fill=grout); dm.rectangle([x - g, y, x + g, int((r + 1) * th * k)], fill=255)
            col += 1
        r += 1
    c.save(pc, quality=92); m.save(pm)
    return pc, pm


# ================================================================== materials
bpy.ops.wm.read_factory_settings(use_empty=True)
sc = bpy.context.scene
MATS = {}


def material(name, color=(0.8, 0.8, 0.8), rough=0.5, metal=0.0, img=None, grout=None, coat=0.0, alpha=None,
             transmission=0.0, emission=0.0, repeat=None):
    if name in MATS:
        return MATS[name]
    m = bpy.data.materials.new(name); m.use_nodes = True
    nt = m.node_tree; b = nt.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*color, 1); b.inputs["Roughness"].default_value = rough
    b.inputs["Metallic"].default_value = metal
    if coat:
        b.inputs["Coat Weight"].default_value = coat; b.inputs["Coat Roughness"].default_value = 0.04
    if transmission:
        b.inputs["Transmission Weight"].default_value = transmission; b.inputs["IOR"].default_value = 1.5
    if emission:
        b.inputs["Emission Color"].default_value = (*color, 1); b.inputs["Emission Strength"].default_value = emission
    uv = None
    if repeat:
        uv = nt.nodes.new("ShaderNodeMapping"); uv.inputs["Scale"].default_value = (repeat[0], repeat[1], 1)
        tc = nt.nodes.new("ShaderNodeTexCoord"); nt.links.new(tc.outputs["UV"], uv.inputs["Vector"])
    if img:
        t = nt.nodes.new("ShaderNodeTexImage"); t.image = bpy.data.images.load(img)
        if uv: nt.links.new(uv.outputs[0], t.inputs[0])
        nt.links.new(t.outputs["Color"], b.inputs["Base Color"])
    if grout:
        gm = nt.nodes.new("ShaderNodeTexImage"); gm.image = bpy.data.images.load(grout); gm.image.colorspace_settings.name = "Non-Color"
        if uv: nt.links.new(uv.outputs[0], gm.inputs[0])
        bump = nt.nodes.new("ShaderNodeBump"); bump.inputs["Strength"].default_value = 0.4; bump.inputs["Distance"].default_value = 0.0015
        inv = nt.nodes.new("ShaderNodeMath"); inv.operation = "SUBTRACT"; inv.inputs[0].default_value = 1.0
        nt.links.new(gm.outputs["Color"], inv.inputs[1]); nt.links.new(inv.outputs[0], bump.inputs["Height"])
        nt.links.new(bump.outputs["Normal"], b.inputs["Normal"])
        mr = nt.nodes.new("ShaderNodeMapRange"); mr.inputs["To Min"].default_value = rough; mr.inputs["To Max"].default_value = 0.75
        nt.links.new(gm.outputs["Color"], mr.inputs["Value"]); nt.links.new(mr.outputs["Result"], b.inputs["Roughness"])
    if alpha is not None:
        b.inputs["Alpha"].default_value = alpha
    MATS[name] = m
    return m


def tile_mat(sku, wcm, hcm, k=4, turn=False):
    c, g = lay(sku, wcm, hcm, k, turn=turn)
    gl = glossy(sku)
    return material(f"tile {sku} {wcm}x{hcm} {k}{turn}", rough=0.07 if gl else 0.42, img=c, grout=g, coat=0.5 if gl else 0.0)


white = material("wall paint", (0.83, 0.83, 0.81), 0.9)
ceiling_m = material("ceiling", (0.86, 0.86, 0.85), 0.95)
black = material("black powder coat", (0.012, 0.012, 0.012), 0.38, 0.4)
chrome = material("chrome", (0.92, 0.92, 0.92), 0.06, 1.0)
matte_black = material("matte black metal", (0.02, 0.02, 0.02), 0.5, 0.3)
ceramic = material("sanitary ceramic", (0.93, 0.93, 0.92), 0.05, coat=1.0)
mirror_m = material("mirror", (0.96, 0.96, 0.96), 0.0, 1.0)
glass = material("glass", (0.96, 0.98, 0.98), 0.0, transmission=1.0)
oak = material("oak", (0.2, 0.12, 0.065), 0.45)
cladding = material("facade cladding", (0.06, 0.06, 0.06), 0.55, 0.2)


# ================================================================== geometry helpers
def apply_scale(o):
    bpy.ops.object.select_all(action="DESELECT"); o.select_set(True); bpy.context.view_layer.objects.active = o
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)


def box(name, size, loc, mat, rot=(0, 0, 0), bevel=0.003, parent=None):
    bpy.ops.mesh.primitive_cube_add(size=1, location=loc, rotation=rot)
    o = bpy.context.object; o.name = name; o.scale = size; apply_scale(o)
    if bevel:
        mod = o.modifiers.new("b", "BEVEL"); mod.width = bevel; mod.segments = 2
    o.data.materials.append(mat)
    if parent: o.parent = parent
    return o


def plane(name, w, h, loc, rot, mat, parent=None):
    bpy.ops.mesh.primitive_plane_add(size=1, location=loc, rotation=rot)
    o = bpy.context.object; o.name = name; o.scale = (w, h, 1); apply_scale(o)
    o.data.materials.append(mat)
    if parent: o.parent = parent
    return o


def smooth(o, angle=35):
    bpy.ops.object.select_all(action="DESELECT"); o.select_set(True); bpy.context.view_layer.objects.active = o
    bpy.ops.object.shade_auto_smooth(angle=math.radians(angle))


def loft(name, rings, mat, cap_bottom=True, parent=None, n=72, p=2.6):
    """A shape from superellipse rings [(a, b, z), ...] (a, b = half sizes): baths and basins."""
    me = bpy.data.meshes.new(name); bm = bmesh.new()
    vs = []
    for a, b, z in rings:
        ring = []
        for i in range(n):
            t = 2 * math.pi * i / n
            c, s = math.cos(t), math.sin(t)
            x = a * math.copysign(abs(c) ** (2 / p), c); y = b * math.copysign(abs(s) ** (2 / p), s)
            ring.append(bm.verts.new((x, y, z)))
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
    for poly in o.data.polygons: poly.use_smooth = True
    if parent: o.parent = parent
    return o


def text_image(path, size, lines, bg, fg):
    im = Image.new("RGB", size, bg); d = ImageDraw.Draw(im)
    for text, font, px, y, color in lines:
        f = ImageFont.truetype(font, px)
        w = d.textlength(text, font=f)
        d.text(((size[0] - w) / 2, y), text, font=f, fill=color or fg)
    im.save(path)
    return path


def asset(name, loc, height, rot_z=0.0, parent=None):
    """Import a Poly Haven glTF, scale it to the given height, put it on the floor at loc."""
    path = os.path.join(args.assets, name, name + ".gltf")
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=path)
    new = [o for o in bpy.data.objects if o not in before]
    root = bpy.data.objects.new(name, None); bpy.context.collection.objects.link(root)
    for o in new:
        if o.parent is None: o.parent = root
    bpy.context.view_layer.update()
    pts = [o.matrix_world @ Vector(c) for o in new if o.type == "MESH" for c in o.bound_box]
    zmin = min(p.z for p in pts); zmax = max(p.z for p in pts)
    cx = (min(p.x for p in pts) + max(p.x for p in pts)) / 2; cy = (min(p.y for p in pts) + max(p.y for p in pts)) / 2
    k = height / max(1e-6, zmax - zmin)
    for o in new:
        if o.parent is root: o.location = (o.location.x - cx, o.location.y - cy, o.location.z - zmin)
    root.scale = (k, k, k); root.rotation_euler = (0, 0, rot_z); root.location = loc
    if parent: root.parent = parent
    return root


# ================================================================== the displays
# Laid out like a real tile and bathroom showroom: a wall of framed tile samples (left), freestanding baths on
# tiled plinths (middle left), shower sets on display boards (back left), a gallery of big slabs (back right),
# a lit podium with WCs and a basin (middle right), bathroom wall sections (right) and an outdoor corner (front).
BOOTHS = []          # every display the tour points at: {id, title, sku, floor, set, center (world)}
MARKS = []           # (display id, sku, surface, world point): the dots on the flat home-page pictures
charcoal = material("charcoal panel", (0.04, 0.04, 0.045), 0.42, 0.1)
alu = material("brushed aluminium", (0.78, 0.78, 0.78), 0.22, 1.0)
gold = material("brushed gold", (0.83, 0.6, 0.3), 0.22, 1.0)
board = material("display board", (0.9, 0.9, 0.88), 0.55)
navy = material("nova navy", (0.03, 0.06, 0.16), 0.4)
led = material("led strip", (1.0, 0.86, 0.68), 0.5, emission=18)
bulb = material("filament bulb", (1.0, 0.72, 0.4), 0.2, emission=30)
label_m = material("price label", (0.95, 0.95, 0.94), 0.6)


def group(name, pos, rot_z=0.0):
    e = bpy.data.objects.new(name, None); bpy.context.collection.objects.link(e)
    e.location = pos; e.rotation_euler = (0, 0, rot_z)
    bpy.context.view_layer.update()
    return e


def wpt(e, p):
    bpy.context.view_layer.update()
    return tuple(e.matrix_world @ Vector(p))


def register(did, title, sku, center, floor=None, sets=None, marks=()):
    BOOTHS.append({"id": did, "sku": sku, "floor": floor, "set": sets, "title": title, "center": tuple(center)})
    for s_, surf, p in marks:
        MARKS.append((did, s_, surf, tuple(p)))


def tmat(sku, wcm, hcm, k=4, turn=False, grout=(212, 208, 200)):
    c, g = lay(sku, wcm, hcm, k, grout=grout, turn=turn)
    gl = glossy(sku)
    return material(f"tile {os.path.basename(c)}", rough=0.07 if gl else 0.42, img=c, grout=g, coat=0.5 if gl else 0.0)


def wall_tiles(name, w, h, loc, sku, parent, k=3, side=False, turn=False):
    """Tiles on a wall that faces +y (or +x with side=True), in the parent's coordinates."""
    rot = (math.pi / 2, 0, math.pi / 2) if side else (math.pi / 2, 0, 0)
    return plane(name, w, h, loc, rot, tmat(sku, int(w * 100), int(h * 100), k=k, turn=turn), parent=parent)


def floor_tiles(name, w, d, loc, sku, parent, k=3):
    return plane(name, w, d, loc, (0, 0, 0), tmat(sku, int(w * 100), int(d * 100), k=k), parent=parent)


def tub(e, x, y, z=0.0, mat=None):
    rings = [(0.66, 0.27, 0.0), (0.74, 0.32, 0.12), (0.83, 0.38, 0.42), (0.86, 0.4, 0.58), (0.86, 0.4, 0.6),
             (0.835, 0.375, 0.6), (0.82, 0.36, 0.56), (0.76, 0.31, 0.28), (0.66, 0.24, 0.1), (0.45, 0.15, 0.08)]
    t = loft("freestanding bath", rings, mat or ceramic, parent=e, p=2.3)
    t.location = (x, y, z)
    sub = t.modifiers.new("s", "SUBSURF"); sub.levels = 1; sub.render_levels = 2
    return t


def floor_mixer(e, x, y, z, finish, toward=1):
    box("bath mixer", (0.035, 0.035, 1.02), (x, y, z + 0.51), finish, bevel=0.008, parent=e)
    box("bath mixer spout", (0.22, 0.03, 0.03), (x + toward * 0.1, y, z + 1.01), finish, bevel=0.008, parent=e)
    box("hand shower", (0.04, 0.04, 0.2), (x - toward * 0.05, y + 0.03, z + 0.78), finish, bevel=0.012, parent=e)


def wc(e, x, y, z):
    w = loft("wall-hung wc", [(0.17, 0.25, 0.32), (0.18, 0.27, 0.36), (0.185, 0.28, 0.42), (0.18, 0.27, 0.43)], ceramic, parent=e, p=3.0)
    w.location = (x, y + 0.29, z)
    sub = w.modifiers.new("s", "SUBSURF"); sub.levels = 1; sub.render_levels = 2
    box("seat", (0.36, 0.5, 0.025), (x, y + 0.32, z + 0.445), ceramic, bevel=0.012, parent=e)
    box("flush plate", (0.24, 0.012, 0.16), (x, y + 0.006, z + 1.05), matte_black, bevel=0.004, parent=e)


def vanity(e, x, y, z, finish=chrome):
    box("vanity", (1.0, 0.48, 0.42), (x, y + 0.25, z + 0.68), oak, bevel=0.006, parent=e)
    box("vanity top", (1.02, 0.5, 0.025), (x, y + 0.25, z + 0.9), material("white quartz", (0.88, 0.88, 0.86), 0.2, coat=0.5), bevel=0.003, parent=e)
    for dx in (-0.25, 0.25):
        box("handle", (0.2, 0.015, 0.015), (x + dx, y + 0.495, z + 0.84), matte_black, bevel=0.003, parent=e)
    bowl = loft("countertop basin", [(0.2, 0.15, 0.912), (0.26, 0.19, 0.95), (0.27, 0.2, 1.04), (0.255, 0.185, 1.04),
                                      (0.24, 0.17, 0.98), (0.1, 0.07, 0.93)], ceramic, cap_bottom=False, parent=e, p=2.4)
    bowl.location = (x, y + 0.27, z)
    box("mirror frame", (0.72, 0.03, 0.92), (x, y + 0.018, z + 1.62), matte_black, bevel=0.004, parent=e)
    box("mirror", (0.68, 0.01, 0.88), (x, y + 0.035, z + 1.62), mirror_m, bevel=0, parent=e)
    box("tap", (0.03, 0.03, 0.22), (x, y + 0.08, z + 1.03), finish, bevel=0.006, parent=e)
    box("spout", (0.028, 0.14, 0.028), (x, y + 0.14, z + 1.13), finish, bevel=0.006, parent=e)


def shower_set(e, x, y, finish, rain=True):
    box("shower mixer", (0.36, 0.06, 0.06), (x, y + 0.04, 1.05), finish, bevel=0.02, parent=e)
    for dx in (-0.19, 0.19):
        bpy.ops.mesh.primitive_cylinder_add(vertices=32, radius=0.032, depth=0.05, location=(x + dx, y + 0.04, 1.05), rotation=(0, math.pi / 2, 0))
        o = bpy.context.object; o.data.materials.append(finish); o.parent = e; smooth(o)
    box("slide rail", (0.022, 0.022, 0.9), (x + 0.13, y + 0.03, 1.55), finish, bevel=0.005, parent=e)
    box("hand shower", (0.045, 0.045, 0.24), (x + 0.13, y + 0.06, 1.72), finish, bevel=0.015, parent=e)
    if rain:
        box("riser", (0.026, 0.026, 0.92), (x - 0.02, y + 0.03, 1.56), finish, bevel=0.006, parent=e)
        box("shower arm", (0.025, 0.34, 0.025), (x - 0.02, y + 0.19, 2.02), finish, bevel=0.005, parent=e)
        bpy.ops.mesh.primitive_cylinder_add(vertices=64, radius=0.14, depth=0.014, location=(x - 0.02, y + 0.34, 2.0))
        o = bpy.context.object; o.data.materials.append(finish); o.parent = e; smooth(o)


def pendant(x, y, z_bulb):
    box("pendant cable", (0.006, 0.006, H - z_bulb - 0.05), (x, y, (H + z_bulb) / 2), black, bevel=0)
    bpy.ops.mesh.primitive_uv_sphere_add(segments=24, ring_count=12, radius=0.055, location=(x, y, z_bulb))
    o = bpy.context.object; o.data.materials.append(bulb); smooth(o)
    bpy.ops.object.light_add(type="POINT", location=(x, y, z_bulb - 0.08)); L = bpy.context.object
    L.data.energy = 18; L.data.shadow_soft_size = 0.05; L.data.color = (1, 0.75, 0.45)


def led_line(name, size, loc, parent=None):
    return box(name, size, loc, led, bevel=0, parent=parent)


# ---- 1. the sample wall (left wall): framed tile samples in rows, like a tile shop's display wall
SAMPLE_ORDER = ["CN010", "CN013", "CN007", "CN008", "CN011", "CN015", "CN006",
                "CN001", "CN002", "CN003", "CN005", "CN012", "CN004", "CN009",
                "CN014", "CN008", "CN010", "CN013", "CN007", "CN011", "CN003"]
sw = group("sample wall", (-W / 2, -0.6, 0), -math.pi / 2)        # local +y points into the hall, local +x to the back
done_skus = set()
for i, sku in enumerate(SAMPLE_ORDER):
    row, col = divmod(i, 7)
    x, z = 3.0 - col * 1.0, 2.52 - row * 0.9
    repeat = sku in done_skus
    box("sample frame", (0.86, 0.035, 0.86), (x, 0.0175, z), black, bevel=0.003, parent=sw)
    plane("sample", 0.8, 0.8, (x, 0.0365, z), (math.pi / 2, 0, 0),
          tmat(sku, 80, 80, k=5, turn=repeat, grout=(55, 55, 55) if repeat else (212, 208, 200)), parent=sw)
    box("price label", (0.09, 0.006, 0.045), (x + 0.31, 0.04, z + 0.36), label_m, bevel=0.001, parent=sw)
    if not repeat:
        done_skus.add(sku)
        register(f"sample_{sku}", CAT[sku]["name"], sku, wpt(sw, (x, 0.3, z)), marks=[(sku, "sample", wpt(sw, (x, 0.04, z)))])
box("sample wall cove", (7.3, 0.2, 0.12), (0, 0.1, 3.18), white, bevel=0.002, parent=sw)
led_line("sample wall led", (7.2, 0.01, 0.012), (0, 0.19, 3.115), parent=sw)

# ---- 2. bathroom vignettes: two tiled plinths with freestanding baths, split by a tiled partition
vg = group("bath vignettes", (-3.4, -0.6, 0), 0)
for cx, wall, flo, finish, did, title in [(-1.2, "CN015", "CN003", gold, "vig_oak", "Concrete and oak bathroom"),
                                          (1.2, "CN006", "CN005", matte_black, "vig_marble", "Calacatta bathroom")]:
    box("plinth", (2.32, 2.12, 0.1), (cx, 1.05, 0.05), alu, bevel=0.004, parent=vg)
    floor_tiles("plinth tiles", 2.28, 2.08, (cx, 1.05, 0.101), flo, vg)
    box("back wall", (2.32, 0.1, 2.5), (cx, -0.05, 1.25), white, bevel=0.002, parent=vg)
    wall_tiles("back wall tiles", 2.28, 2.45, (cx, 0.002, 1.3), wall, vg)
    box("wall cap", (2.34, 0.12, 0.025), (cx, -0.05, 2.51), alu, bevel=0.002, parent=vg)
    tub(vg, cx + 0.15, 1.0, 0.1)
    floor_mixer(vg, cx - 0.92, 1.0, 0.1, finish)
    register(did, title, wall, wpt(vg, (cx, 0.6, 1.6)), floor=flo,
             marks=[(wall, "wall", wpt(vg, (cx + (-0.6 if cx < 0 else 0.6), 0.01, 1.95))),
                    (flo, "floor", wpt(vg, (cx + (0.55 if cx < 0 else -0.55), 1.85, 0.101)))])
box("partition", (0.1, 1.3, 2.5), (0, 0.65, 1.25), white, bevel=0.002, parent=vg)
wall_tiles("partition left", 1.28, 2.45, (-0.052, 0.65, 1.3), "CN015", vg, side=True)
wall_tiles("partition right", 1.28, 2.45, (0.052, 0.65, 1.3), "CN006", vg, side=True)
box("partition post", (0.12, 0.04, 2.52), (0, 1.3, 1.26), alu, bevel=0.003, parent=vg)
box("outer post", (0.04, 0.12, 2.52), (-2.38, -0.05, 1.26), alu, bevel=0.003, parent=vg)
box("outer post", (0.04, 0.12, 2.52), (2.38, -0.05, 1.26), alu, bevel=0.003, parent=vg)
for (px, py, pz) in [(-1.6, 0.7, 2.25), (-1.0, 1.2, 2.05), (-0.5, 0.8, 2.35), (0.7, 0.9, 2.15), (1.3, 1.3, 2.4), (1.8, 0.7, 2.0)]:
    p_ = wpt(vg, (px, py, pz)); pendant(p_[0], p_[1], p_[2])

# ---- 3. the shower wall (back wall, left): shower sets on white boards against dark marble
sh = group("shower wall", (-4.75, -D / 2, 0), 0)
wall_tiles("shower wall tiles", 7.9, 2.7, (0, 0.004, 1.35), "CN011", sh, k=2, turn=True)
box("shower wall fascia", (7.95, 0.26, 0.22), (0, 0.13, 2.81), charcoal, bevel=0.003, parent=sh)
led_line("shower wall led", (7.9, 0.012, 0.012), (0, 0.255, 2.69), parent=sh)
for i in range(7):
    x = -3.3 + i * 1.1
    box("display board", (0.52, 0.03, 2.1), (x, 0.06, 1.2), board, bevel=0.003, parent=sh)
    box("board band", (0.52, 0.032, 0.08), (x, 0.061, 2.21), navy, bevel=0.002, parent=sh)
    shower_set(sh, x, 0.075, chrome if i % 2 == 0 else matte_black, rain=(i % 3 != 1))
register("showers", "Shower sets", "CN011", wpt(sh, (0, 1.2, 1.5)), sets="SN002",
         marks=[("SN002", "product", wpt(sh, (-1.1, 0.12, 1.05))), ("CN011", "wall", wpt(sh, (-1.65, 0.006, 0.6)))])
for tx in (-7.2, -2.2):                                # two more baths in front of the shower wall
    g_ = group("bath", (tx, -4.45, 0), math.radians(4 if tx < -4 else -4))
    tub(g_, 0, 0, 0)
    floor_mixer(g_, -1.05, 0, 0, chrome if tx < -4 else matte_black)
    pendant(tx - 0.3, -4.5, 2.3); pendant(tx + 0.35, -4.3, 2.55)

# ---- 4. the slab gallery (back wall, right): big tiles in tall lit bays on a marble step
gl = group("slab gallery", (4.75, -D / 2, 0), 0)
box("gallery back", (7.9, 0.06, 3.0), (0, 0.03, 1.5), charcoal, bevel=0.002, parent=gl)
box("gallery step", (7.9, 0.9, 0.12), (0, 0.51, 0.06), charcoal, bevel=0.003, parent=gl)
floor_tiles("gallery step top", 7.88, 0.88, (0, 0.51, 0.121), "CN006", gl, k=2)
led_line("gallery step led", (7.9, 0.012, 0.015), (0, 0.965, 0.03), parent=gl)
box("gallery fascia", (7.95, 0.32, 0.2), (0, 0.16, 3.0), charcoal, bevel=0.003, parent=gl)
led_line("gallery top led", (7.9, 0.012, 0.012), (0, 0.315, 2.89), parent=gl)
for i, sku in enumerate(["CN006", "CN011", "CN015", "CN002", "CN001", "CN012"]):
    x = -3.3 + i * 1.32
    box("slab backing", (1.22, 0.03, 2.42), (x, 0.075, 1.45), charcoal, bevel=0.002, parent=gl)
    wall_tiles("slab", 1.2, 2.4, (x, 0.092, 1.45), sku, gl, k=3)
    for dx in (-0.66, 0.66):
        box("gallery pier", (0.1, 0.14, 2.8), (x + dx, 0.07, 1.52), charcoal, bevel=0.003, parent=gl)
        led_line("pier led", (0.012, 0.012, 2.5), (x + dx, 0.142, 1.45), parent=gl)
    register(f"slab_{sku}", CAT[sku]["name"], sku, wpt(gl, (x, 0.6, 1.45)), marks=[(sku, "sample", wpt(gl, (x, 0.095, 1.7)))])

# ---- 5. the podium: WCs and a basin on a lit charcoal platform with tiled backdrops
pd = group("wc podium", (3.9, -0.7, 0), 0)
box("podium", (3.4, 1.7, 0.14), (0, 0.85, 0.09), charcoal, bevel=0.004, parent=pd)
floor_tiles("podium top", 3.36, 1.66, (0, 0.85, 0.161), "CN005", pd)
box("podium glow", (3.36, 1.66, 0.01), (0, 0.85, 0.01), led, bevel=0, parent=pd)
box("podium back", (3.4, 0.15, 1.5), (0, 0.075, 0.91), charcoal, bevel=0.003, parent=pd)
led_line("podium back led", (3.4, 0.012, 0.012), (0, 0.155, 1.655), parent=pd)
for x, sku, did in [(-1.1, "CN007", "pod_wc1"), (0.0, "CN010", "pod_wc2")]:
    box("backdrop", (0.95, 0.06, 1.25), (x, 0.18, 0.84), white, bevel=0.002, parent=pd)
    wall_tiles("backdrop tiles", 0.93, 1.23, (x, 0.2115, 0.84), sku, pd, k=5)
    wc(pd, x, 0.212, 0.16)
    register(did, CAT[sku]["name"] + " with WC", sku, wpt(pd, (x, 0.6, 0.9)), sets="SN003",
             marks=[(sku, "wall", wpt(pd, (x - 0.3, 0.213, 1.3))), ("SN003", "product", wpt(pd, (x, 0.6, 0.6)))])
box("backdrop tall", (1.05, 0.06, 2.3), (1.15, 0.18, 1.31), white, bevel=0.002, parent=pd)
wall_tiles("backdrop tall tiles", 1.03, 2.28, (1.15, 0.2115, 1.31), "CN013", pd, k=5)
vanity(pd, 1.15, 0.212, 0.16)
register("pod_vanity", "Pearl Mosaic with basin", "CN013", wpt(pd, (1.15, 0.6, 1.2)), sets="SN001",
         marks=[("CN013", "wall", wpt(pd, (1.5, 0.213, 2.2))), ("SN001", "product", wpt(pd, (1.15, 0.715, 0.84)))])

# ---- 6. the bathroom wall (right wall): three tiled sections, each with its own floor sample
bw = group("bathroom wall", (W / 2, -1.0, 0), math.pi / 2)         # local +y into the hall, local +x to the front
for sx, wall, flo, kind, did, title, sets in [(-2.6, "CN008", "CN012", "shower", "wall_aqua", "Aqua Shield shower", "SN002"),
                                              (0.0, "CN015", "CN002", "vanity", "wall_concrete", "Concrete Light bathroom", "SN001"),
                                              (2.6, "CN011", "CN005", "wc", "wall_onyx", "Black Onyx bathroom", "SN003")]:
    wall_tiles("section tiles", 2.4, 2.6, (sx, 0.004, 1.3), wall, bw)
    floor_tiles("section floor", 2.4, 1.4, (sx, 0.7, 0.004), flo, bw)
    for dx in (-1.22, 1.22):
        box("section trim", (0.03, 0.03, 2.62), (sx + dx, 0.015, 1.31), alu, bevel=0.002, parent=bw)
    box("section fascia", (2.46, 0.2, 0.16), (sx, 0.1, 2.68), charcoal, bevel=0.003, parent=bw)
    led_line("section led", (2.4, 0.012, 0.012), (sx, 0.195, 2.6), parent=bw)
    if kind == "shower":
        shower_set(bw, sx - 0.3, 0.004, matte_black)
        box("shower glass", (0.012, 1.1, 2.0), (sx + 0.4, 0.58, 1.04), glass, bevel=0, parent=bw)
        box("glass profile", (0.03, 0.03, 2.0), (sx + 0.4, 0.03, 1.04), matte_black, bevel=0, parent=bw)
        prod = (sx - 0.3, 0.08, 1.05)
    elif kind == "vanity":
        vanity(bw, sx, 0.004, 0.0); prod = (sx, 0.5, 0.68)
    else:
        wc(bw, sx, 0.004, 0.0); prod = (sx, 0.4, 0.42)
        asset("potted_plant_01", wpt(bw, (sx + 0.8, 0.5, 0)), 0.9)
    register(did, title, wall, wpt(bw, (sx, 0.6, 1.5)), floor=flo, sets=sets,
             marks=[(wall, "wall", wpt(bw, (sx - 0.75, 0.006, 1.95))), (flo, "floor", wpt(bw, (sx + 0.7, 1.15, 0.005))),
                    (sets, "product", wpt(bw, prod))])

# ---- 7. the outdoor corner (front right, by the window): outdoor tiles on a deck with planters
od = group("outdoor corner", (7.1, 4.0, 0), 0)
box("deck", (2.62, 2.22, 0.12), (0, 0, 0.06), alu, bevel=0.004, parent=od)
floor_tiles("deck slate", 1.3, 2.2, (-0.655, 0, 0.121), "CN014", od)
floor_tiles("deck grip", 1.3, 2.2, (0.655, 0, 0.121), "CN004", od)
box("planter wall", (2.6, 0.32, 0.45), (0, 0.95, 0.345), white, bevel=0.003, parent=od)
wall_tiles("planter tiles", 2.58, 0.44, (0, 0.785, 0.345), "CN009", od, k=4)
plane("planter top", 2.58, 0.3, (0, 0.95, 0.571), (0, 0, 0), tmat("CN009", 258, 30, k=4), parent=od)
asset("potted_plant_02", wpt(od, (-0.8, 0.95, 0.57)), 1.2)
asset("calathea_orbifolia_01", wpt(od, (0.7, 0.95, 0.57)), 0.6)
asset("coffee_table_round_01", wpt(od, (0.2, -0.2, 0.12)), 0.45)
asset("Ottoman_01", wpt(od, (-0.6, -0.4, 0.12)), 0.42)
register("out_slate", "Garden Slate (outdoor)", "CN014", wpt(od, (-0.6, 0, 0.5)), floor="CN004",
         marks=[("CN014", "floor", wpt(od, (-0.8, -0.85, 0.122))), ("CN004", "floor", wpt(od, (0.85, -0.85, 0.122))),
                ("CN009", "wall", wpt(od, (-0.2, 0.784, 0.3)))])
register("out_terra", "Terra Rustic (outdoor)", "CN009", wpt(od, (0.2, 0.8, 0.4)))

# ================================================================== the hall
random.seed(7)
hf_c, hf_g = lay("CN001", 600, 600, 2)
floor = plane("hall floor", W, D, (0, 0, 0), (0, 0, 0), material("hall floor", rough=0.18, img=hf_c, grout=hf_g, coat=0.3,
                                                                   repeat=(W / 6, D / 6)))
plane("ceiling", W, D, (0, 0, H), (math.pi, 0, 0), ceiling_m)
plane("back wall", W, H, (0, -D / 2, H / 2), (math.pi / 2, 0, 0), white)
plane("left wall", D, H, (-W / 2, 0, H / 2), (math.pi / 2, 0, -math.pi / 2), white)
plane("right wall", D, H, (W / 2, 0, H / 2), (math.pi / 2, 0, math.pi / 2), white)
for (x, y, sx, sy) in [(0, -D / 2 + 0.006, W, 0.012), (-W / 2 + 0.006, 0, 0.012, D), (W / 2 - 0.006, 0, 0.012, D)]:
    box("skirting", (sx, sy, 0.08), (x, y, 0.04), material("skirting", (0.75, 0.75, 0.73), 0.5), bevel=0)
asset("potted_plant_02", (0.0, -5.5, 0), 1.7)
asset("potted_plant_01", (-8.4, 4.9, 0), 1.2)

# ================================================================== the shop front, the square outside and the sky
DOOR_W, GLASS_H = 2.6, 3.0
for side in (-1, 1):                                  # glass shop windows with black mullions on both sides of the doors
    x0, x1 = side * DOOR_W / 2, side * W / 2
    width = abs(x1 - x0); cx = (x0 + x1) / 2
    box("shop window", (width, 0.012, GLASS_H), (cx, FRONT, GLASS_H / 2), glass, bevel=0)
    n = int(width // 1.8) + 1
    for i in range(n + 1):
        box("mullion", (0.06, 0.12, GLASS_H), (x0 + side * i * width / n, FRONT, GLASS_H / 2), black, bevel=0.002)
box("transom", (W, 0.14, 0.12), (0, FRONT, GLASS_H + 0.06), black, bevel=0.002)
box("front wall top", (W, 0.2, H - GLASS_H), (0, FRONT + 0.05, GLASS_H + (H - GLASS_H) / 2), white, bevel=0)
box("cladding", (W + 12, 0.25, 2.6), (0, FRONT + 0.28, GLASS_H + 1.3), cladding, bevel=0.005)
box("cladding left", (6, 0.25, GLASS_H), (-W / 2 - 3, FRONT + 0.28, GLASS_H / 2), cladding, bevel=0.005)
box("cladding right", (6, 0.25, GLASS_H), (W / 2 + 3, FRONT + 0.28, GLASS_H / 2), cladding, bevel=0.005)
sign = text_image(os.path.join(TEX, "sign.png"), (3000, 420), [
    ("CERAMICA NOVA", FONT, 230, 40, (244, 240, 232)), ("TILES  ·  BATHROOMS  ·  SHOWROOM", FONT_L, 82, 300, (201, 180, 143))],
    (12, 12, 12), (255, 255, 255))
plane("sign", 7.5, 1.05, (0, FRONT + 0.41, GLASS_H + 1.3), (math.pi / 2, 0, math.pi), material("sign", (1, 1, 1), 0.4, img=sign))
DOORS = []
for side in (-1, 1):                                  # two sliding glass doors
    g = box("door glass", (DOOR_W / 2 - 0.04, 0.014, GLASS_H - 0.08), (side * DOOR_W / 4, FRONT + 0.12, GLASS_H / 2), glass, bevel=0)
    f1 = box("door frame", (0.05, 0.05, GLASS_H - 0.06), (side * (DOOR_W / 2 - 0.03), FRONT + 0.12, GLASS_H / 2), black, bevel=0.002)
    f2 = box("door frame", (0.05, 0.05, GLASS_H - 0.06), (side * 0.03, FRONT + 0.12, GLASS_H / 2), black, bevel=0.002)
    hd = box("door handle", (0.025, 0.06, 1.1), (side * 0.12, FRONT + 0.17, 1.1), chrome, bevel=0.006)
    DOORS.append((side, [g, f1, f2, hd]))


def doors_open(k):
    for side, parts in DOORS:
        for o in parts:
            o.location.x += side * k * (DOOR_W / 2 - 0.1) - o.get("shift", 0.0)
            o["shift"] = side * k * (DOOR_W / 2 - 0.1)


box("door track", (DOOR_W + 2.6, 0.12, 0.08), (0, FRONT + 0.12, GLASS_H - 0.0), black, bevel=0.002)
box("threshold", (DOOR_W, 0.3, 0.012), (0, FRONT + 0.1, 0.006), chrome, bevel=0)
box("door mat", (2.2, 1.2, 0.012), (0, FRONT - 0.9, 0.006), material("coir mat", (0.17, 0.15, 0.13), 1.0), bevel=0.003)
pave = material("square paving", rough=0.6, img=lay("CN004", 240, 240, 2)[0], grout=lay("CN004", 240, 240, 2)[1], repeat=(40 / 2.4, 20 / 2.4))
plane("square", 40, 20, (0, FRONT + 10.1, -0.001), (0, 0, 0), pave)
box("kerb", (40, 0.3, 0.15), (0, FRONT + 20, 0.075), material("kerb", (0.6, 0.58, 0.55), 0.8), bevel=0.01)
plane("road", 60, 30, (0, FRONT + 35, -0.02), (0, 0, 0), material("asphalt", (0.07, 0.07, 0.075), 0.85))
for x in (-3.0, 3.0, -8.5, 8.5):                       # big planters by the shop front
    box("planter", (1.0, 1.0, 0.6), (x, FRONT + 1.4, 0.3), material("planter", (0.18, 0.18, 0.18), 0.6), bevel=0.02)
    asset("potted_plant_02", (x, FRONT + 1.4, 0.45), 1.7)
for x in (-12, 12):
    box("neighbour", (6, 6, 9), (x * 1.6, FRONT + 3, 4.5), material("neighbour", (0.78, 0.74, 0.68), 0.8), bevel=0.02)
# the sky (Poly Haven HDRI) lights the outside and shines in through the glass
world = bpy.data.worlds.new("sky"); sc.world = world; world.use_nodes = True
env = world.node_tree.nodes.new("ShaderNodeTexEnvironment")
env.image = bpy.data.images.load(os.path.join(args.assets, "drakensberg_solitary_mountain_puresky.hdr"))
world.node_tree.links.new(env.outputs["Color"], world.node_tree.nodes["Background"].inputs["Color"])
world.node_tree.nodes["Background"].inputs["Strength"].default_value = 0.8
mp = world.node_tree.nodes.new("ShaderNodeMapping"); tc = world.node_tree.nodes.new("ShaderNodeTexCoord")
mp.inputs["Rotation"].default_value = (0, 0, math.radians(200))
world.node_tree.links.new(tc.outputs["Generated"], mp.inputs["Vector"]); world.node_tree.links.new(mp.outputs[0], env.inputs[0])

# ================================================================== lights inside
def spot(loc, target, energy=240, size=50):
    """A track spotlight aimed at a point (with its black housing)."""
    d = (Vector(target) - Vector(loc)).normalized()
    rot = d.to_track_quat("-Z", "Y").to_euler()
    bpy.ops.object.light_add(type="SPOT", location=Vector(loc) + d * 0.08); L = bpy.context.object
    L.data.energy = energy; L.data.spot_size = math.radians(size); L.data.spot_blend = 0.7; L.data.shadow_soft_size = 0.05
    L.data.color = (1, 0.93, 0.84); L.rotation_euler = rot
    bpy.ops.mesh.primitive_cylinder_add(vertices=24, radius=0.045, depth=0.13, location=loc)
    c = bpy.context.object; c.rotation_euler = rot; c.data.materials.append(matte_black)


def track(a, b, n, aim):
    """A black ceiling track from a to b (x, y) with n spots, each aimed by aim(x, y) -> target."""
    (x0, y0), (x1, y1) = a, b
    box("track", (max(0.04, abs(x1 - x0)), max(0.04, abs(y1 - y0)), 0.04), ((x0 + x1) / 2, (y0 + y1) / 2, H - 0.02), matte_black, bevel=0)
    for i in range(n):
        t = (i + 0.5) / n; x, y = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
        spot((x, y, H - 0.1), aim(x, y))


track((-8.5, -4.4), (-1.0, -4.4), 6, lambda x, y: (x, -D / 2, 1.3))           # shower wall
track((1.0, -4.3), (8.5, -4.3), 6, lambda x, y: (x, -D / 2, 1.4))             # slab gallery
track((-7.3, -3.9), (-7.3, 2.7), 6, lambda x, y: (-W / 2, y, 1.6))            # sample wall
track((7.4, -4.0), (7.4, 2.4), 5, lambda x, y: (W / 2, y, 1.3))               # bathroom wall
track((2.4, 1.9), (5.4, 1.9), 3, lambda x, y: (x, 0.0, 0.6))                  # podium
glowm = bpy.data.materials.new("downlight"); glowm.use_nodes = True
em = glowm.node_tree.nodes.new("ShaderNodeEmission"); em.inputs["Strength"].default_value = 25; em.inputs["Color"].default_value = (1, 0.95, 0.88, 1)
glowm.node_tree.links.new(em.outputs[0], glowm.node_tree.nodes["Material Output"].inputs[0])
for x in (-6, -3, 0, 3, 6):
    for y in (-2.4, 1.0, 4.0):
        bpy.ops.object.light_add(type="AREA", location=(x, y, H - 0.02)); L = bpy.context.object
        L.data.size = 0.5; L.data.energy = 100; L.data.color = (1, 0.96, 0.9)
        bpy.ops.mesh.primitive_circle_add(vertices=32, radius=0.08, fill_type="NGON", location=(x, y, H - 0.004))
        bpy.context.object.data.materials.append(glowm)

# ================================================================== spots, hotspots and rendering
SPOTS = [
    {"id": "s0", "name": "Outside the shop", "pos": (0.0, FRONT + 6.0), "look": (0, -1)},
    {"id": "s1", "name": "Entrance", "pos": (0.0, FRONT - 1.6), "look": (0, -1)},
    {"id": "s2", "name": "Tile sample wall", "pos": (-6.3, 0.0), "look": (-1, 0)},
    {"id": "s3", "name": "Bath displays", "pos": (-3.3, 2.5), "look": (0, -1)},
    {"id": "s4", "name": "Shower wall", "pos": (-4.7, -2.9), "look": (0, -1)},
    {"id": "s7", "name": "Middle of the showroom", "pos": (0.0, -2.0), "look": (0, -1)},
    {"id": "s5", "name": "Big tile gallery", "pos": (4.6, -3.3), "look": (0, -1)},
    {"id": "s6", "name": "WC and basin podium", "pos": (3.9, 2.3), "look": (0, -1)},
    {"id": "s8", "name": "Bathroom wall", "pos": (6.6, -1.0), "look": (1, 0)},
    {"id": "s9", "name": "Outdoor tiles", "pos": (5.0, 3.5), "look": (1, 0)},
]
LINKS = {"s0": ["s1"], "s1": ["s0", "s3", "s6", "s7", "s9"], "s2": ["s3", "s4", "s7"], "s3": ["s1", "s2", "s4", "s7"],
         "s4": ["s2", "s3", "s7"], "s7": ["s1", "s3", "s4", "s5", "s6", "s2"], "s5": ["s7", "s6", "s8"],
         "s6": ["s1", "s5", "s8", "s9", "s7"], "s8": ["s5", "s6", "s9"], "s9": ["s1", "s6", "s8"]}

# flat "display" pictures for the home page: (id, title, camera, target, lens in mm)
VIEWS = [("overview", "Our showroom", (0.0, 5.3, 1.7), (0.0, -4.0, 1.0), 17),
         ("samples", "Tile sample wall", (-5.0, 3.6, 1.55), (-8.6, -1.2, 1.35), 22),
         ("baths", "Bath displays", (-1.2, 3.7, 1.6), (-3.7, -0.3, 0.8), 22),
         ("gallery", "Big tile gallery", (1.6, -2.0, 1.55), (6.2, -6.0, 1.3), 20),
         ("podium", "WC and basin podium", (6.6, 3.1, 1.6), (3.5, -0.3, 0.7), 22),
         ("showers", "Shower wall", (-4.6, -1.0, 1.55), (-4.6, -6.0, 1.3), 18),
         ("bathwall", "Bathroom wall", (6.3, 2.6, 1.6), (9.0, -1.8, 1.2), 18),
         ("outdoor", "Outdoor tiles", (3.9, 1.4, 1.6), (7.4, 4.3, 0.4), 22)]


def uv_of(cam, target):
    d = Vector(target) - Vector(cam)
    u = (0.5 + math.atan2(d.x, d.y) / (2 * math.pi)) % 1.0
    v = 0.5 - math.atan2(d.z, math.hypot(d.x, d.y)) / math.pi
    return round(u, 4), round(v, 4)


def seen(cam, target):
    """True when nothing but glass stands between the camera and the target."""
    dg = bpy.context.evaluated_depsgraph_get()
    o, t = Vector(cam), Vector(target)
    while True:
        d = t - o; dist = d.length
        hit, loc, _n, _i, ob, _m = sc.ray_cast(dg, o, d.normalized(), distance=dist)
        if not hit or (loc - t).length < 0.35:
            return True
        if "glass" in ob.name:
            o = loc + d.normalized() * 0.02; continue
        return False


sc.render.engine = "CYCLES"; sc.cycles.device = "CPU"
sc.cycles.samples = args.samples; sc.cycles.use_denoising = True; sc.cycles.max_bounces = 8
sc.cycles.transparent_max_bounces = 8; sc.cycles.caustics_reflective = False; sc.cycles.caustics_refractive = False
sc.render.threads_mode = "FIXED"; sc.render.threads = os.cpu_count() or 4
sc.view_settings.view_transform = "AgX"; sc.view_settings.look = "AgX - Medium High Contrast"; sc.view_settings.exposure = -0.1
sc.render.image_settings.file_format = "JPEG"; sc.render.image_settings.quality = 88
bpy.ops.object.camera_add(rotation=(math.radians(90), 0, 0)); cam = bpy.context.object; sc.camera = cam

if args.still:                                        # a normal photo, for checking the look quickly
    x, y, z, yaw = map(float, args.still.split(","))
    cam.location = (x, y, z); cam.rotation_euler = (math.radians(88), 0, math.radians(yaw)); cam.data.lens = 18
    sc.render.resolution_x, sc.render.resolution_y = 1400, 1000
    sc.render.filepath = os.path.join(args.out, "_check.jpg")
    doors_open(1.0)
    bpy.ops.render.render(write_still=True); print("still done"); sys.exit(0)

if args.views:                                        # flat "display" photos for the home page's shop-the-look
    from bpy_extras.object_utils import world_to_camera_view
    sc.render.resolution_x, sc.render.resolution_y = 1920, 1080
    sc.render.resolution_percentage = args.pct
    doors_open(1.0)

    def visible(p):
        dg = bpy.context.evaluated_depsgraph_get()
        o = cam.matrix_world.translation.copy(); t = Vector(p)
        while True:
            d = t - o; dist = d.length
            hit, loc, _n, _i, ob, _m = sc.ray_cast(dg, o, d.normalized(), distance=max(0.0, dist - 0.01))
            if not hit or (loc - t).length < 0.08:
                return True
            if "glass" in ob.name:
                o = loc + d.normalized() * 0.02; continue
            return False

    def dots():
        here = cam.matrix_world.translation
        found = {}
        for _did, sku, surface, p in MARKS:
            c = world_to_camera_view(sc, cam, Vector(p))
            if c.z <= 0.3 or c.z > 7.5 or not (0.05 < c.x < 0.95 and 0.08 < c.y < 0.93) or not visible(p):
                continue
            dist = (Vector(p) - here).length
            if sku not in found or dist < found[sku][0]:        # one dot per product: the nearest one
                found[sku] = (dist, {"sku": sku, "surface": surface, "x": round(c.x * 100, 1), "y": round((1 - c.y) * 100, 1)})
        return [v for _d, v in sorted(found.values(), key=lambda t: t[0])[:8]]

    keep = set(filter(None, args.only.split(",")))
    os.makedirs(args.home, exist_ok=True)
    jp = os.path.join(args.home, "views.json")
    res = json.load(open(jp)) if os.path.exists(jp) else {}
    for vid, title, loc, target, lens in VIEWS:
        cam.location = loc; cam.data.lens = lens
        cam.rotation_euler = (Vector(target) - Vector(loc)).to_track_quat("-Z", "Y").to_euler()
        bpy.context.view_layer.update()
        res[vid] = {"title": title, "image": f"/images/home/show_{vid}.jpg", "spots": dots()}
        if keep and vid not in keep:
            continue
        sc.render.filepath = os.path.join(args.home, f"show_{vid}.jpg")
        bpy.ops.render.render(write_still=True)
        print("view", vid, res[vid]["spots"], flush=True)
        json.dump({k: res[k] for k in [v[0] for v in VIEWS] if k in res}, open(jp, "w"), indent=1)
    json.dump({k: res[k] for k in [v[0] for v in VIEWS] if k in res}, open(jp, "w"), indent=1)
    print("views done"); sys.exit(0)

cam.data.type = "PANO"; cam.data.panorama_type = "EQUIRECTANGULAR"
sc.render.resolution_x, sc.render.resolution_y = args.res, args.res // 2
only = set(filter(None, args.only.split(",")))
by_id = {s["id"]: s for s in SPOTS}
doors_open(1.0)
out = {"spots": [], "booths": []}
for s in SPOTS:
    pos = (s["pos"][0], s["pos"][1], EYE)
    cam.location = pos
    lk = s["look"]; look_u = round((0.5 + math.atan2(lk[0], lk[1]) / (2 * math.pi)) % 1, 4)
    links = []
    for t in LINKS[s["id"]]:
        tp = by_id[t]["pos"]; u, v = uv_of(pos, (tp[0], tp[1], 0.05))
        links.append({"to": t, "u": u, "v": v, "dist": round(math.dist(pos[:2], tp), 2)})
    booths = []
    for b in BOOTHS:
        dist = math.dist(pos[:2], b["center"][:2])
        if dist < 8.5 and seen(pos, b["center"]):
            u, v = uv_of(pos, b["center"]); booths.append({"id": b["id"], "u": u, "v": v, "dist": round(dist, 2)})
    out["spots"].append({"id": s["id"], "name": s["name"], "image": f"/images/showroom/{s['id']}.jpg",
                         "small": f"/images/showroom/{s['id']}_s.jpg", "look_u": look_u, "links": links, "booths": booths})
    if only and s["id"] not in only and not (s["id"] == "s0" and "s0c" in only):
        continue
    variants = [("", 1.0)] + ([("_closed", 0.0)] if s["id"] == "s0" else [])
    for suffix, k in variants:
        if only and s["id"] + suffix not in only and not (suffix == "_closed" and "s0c" in only):
            continue
        doors_open(k)
        path = os.path.join(args.out, f"{s['id']}{suffix}.jpg")
        sc.render.filepath = path
        bpy.ops.render.render(write_still=True)
        im = Image.open(path); im.resize((2048, 1024), Image.LANCZOS).save(path.replace(".jpg", "_s.jpg"), quality=84)
        print("rendered", path, flush=True)
    doors_open(1.0)
out["booths"] = [{"id": b["id"], "sku": b["sku"], "floor": b["floor"], "set": b["set"], "title": b["title"]} for b in BOOTHS]
out["order"] = [s["id"] for s in SPOTS if s["id"] != "s0"]
out["closed"] = "/images/showroom/s0_closed.jpg"
json.dump(out, open(os.path.join(args.out, "spots.json"), "w"), indent=1)
print("spots.json written")
