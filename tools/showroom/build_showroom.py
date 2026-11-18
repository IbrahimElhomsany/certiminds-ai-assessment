"""Builds the Ceramica Nova showroom in Blender and renders the 360° pictures for the shop's home page.

Why: a browser can only fake light, so the live 3D showroom looked like a game. Blender's Cycles renderer
works out real light (soft shadows, light bouncing off walls, reflections in glossy tiles), so each 360°
picture looks like a photo. The visitor glides from spot to spot and looks around (frontend/assets/showroom.js).

Every booth uses our own tiles at their real size (images/<SKU>.jpg + data/catalog.json). Furniture, plants
and the sky are free CC0 assets from Poly Haven (polyhaven.com, no credit needed, commercial use allowed),
fetched with tools/showroom/fetch_assets.py. Bathtub, basins, WC and shower are modelled here.

Run (Blender as a Python module: pip install bpy==4.5.0 on Python 3.11):
    python tools/showroom/build_showroom.py --assets <dir> [--res 4096] [--samples 96] [--only s1,s2]
Writes images/showroom/<spot>.jpg (+ _s.jpg, 2048 wide for phones) and images/showroom/spots.json.
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
    key = f"{sku}_{int(wcm)}x{int(hcm)}_{k}{'_t' if turn else ''}"
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
oak = material("oak", (0.45, 0.3, 0.18), 0.45)
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


# ================================================================== the booths
BOOTH_W, BOOTH_D, BOOTH_H, T = 2.4, 1.7, 2.8, 0.1
BOOTHS = []          # {sku, set, title, center (world), front (world)}


def booth(name, pos, rot_z, wall, floor, props=(), sets=None, title=""):
    """A corner booth like a real showroom stand: tiled back wall + left side wall with black frames, and a
    floor sample. Built facing +y around the origin, then moved to pos and turned by rot_z."""
    e = bpy.data.objects.new(name, None); bpy.context.collection.objects.link(e)
    w, d, h = BOOTH_W, BOOTH_D, BOOTH_H
    box("back panel", (w, T, h), (0, 0, h / 2), white, bevel=0.002, parent=e)
    box("side panel", (T, d, h), (-w / 2 + T / 2, d / 2, h / 2), white, bevel=0.002, parent=e)
    for (x, y, sx, sy, sz, z) in [(w / 2, 0, 0.05, T + 0.03, h + 0.02, h / 2), (-w / 2 + T / 2, d, T + 0.03, 0.05, h + 0.02, h / 2),
                                  (0, 0, w + 0.04, T + 0.03, 0.05, h), (-w / 2 + T / 2, d / 2, T + 0.03, d + 0.02, 0.05, h)]:
        box("frame", (sx, sy, sz), (x, y, z), black, bevel=0.003, parent=e)
    plane("tiles back", w - T - 0.02, h - 0.03, (T / 2, T / 2 + 0.002, h / 2), (math.pi / 2, 0, 0),
          tile_mat(wall, int((w - T) * 100), int(h * 100)), parent=e)
    plane("tiles side", d - T / 2, h - 0.03, (-w / 2 + T + 0.002, d / 2 + T / 4, h / 2), (math.pi / 2, 0, math.pi / 2),
          tile_mat(wall, int(d * 100), int(h * 100)), parent=e)
    box("floor sample", (w + 0.06, d + 0.35, 0.025), (0, (d + 0.35) / 2 - T / 2, 0.0125),
        tile_mat(floor, int((w + 0.06) * 100), int((d + 0.35) * 100), k=3), bevel=0.002, parent=e)
    for pr in props:
        PROPS[pr[0]](e, *pr[1:])
    e.location = pos; e.rotation_euler = (0, 0, rot_z)
    bpy.context.view_layer.update()
    mw = e.matrix_world
    BOOTHS.append({"id": name, "sku": wall, "floor": floor, "set": sets, "title": title,
                   "center": tuple(mw @ Vector((0.1, 0.35, 1.6))), "front": tuple(mw @ Vector((0, 3.2, 0)))})
    return e


# ---- props, in booth coordinates (back wall at y = 0, opening towards +y)
def p_washstand(e, x=0.15):
    o = bpy.ops.mesh.primitive_cylinder_add(vertices=96, radius=0.36, depth=0.03, location=(x, T / 2 + 0.02, 1.68), rotation=(math.pi / 2, 0, 0))
    o = bpy.context.object; o.data.materials.append(matte_black); o.parent = e; smooth(o)
    bpy.ops.mesh.primitive_cylinder_add(vertices=96, radius=0.34, depth=0.03, location=(x, T / 2 + 0.03, 1.68), rotation=(math.pi / 2, 0, 0))
    o = bpy.context.object; o.data.materials.append(mirror_m); o.parent = e
    for dx in (-0.16, 0.16):
        for dy in (0.2, 0.52):
            box("leg", (0.022, 0.022, 0.86), (x + dx, dy, 0.43), matte_black, bevel=0, parent=e)
    box("towel shelf", (0.34, 0.34, 0.015), (x, 0.36, 0.28), matte_black, bevel=0.002, parent=e)
    box("towel", (0.3, 0.2, 0.05), (x, 0.38, 0.31), material("towel", (0.08, 0.1, 0.13), 0.95), bevel=0.02, parent=e)
    bowl = loft("basin", [(0.13, 0.13, 0.86), (0.2, 0.2, 0.89), (0.235, 0.235, 0.96), (0.24, 0.24, 1.0), (0.225, 0.225, 1.0),
                          (0.215, 0.215, 0.96), (0.18, 0.18, 0.91), (0.05, 0.05, 0.895)], ceramic, cap_bottom=False, parent=e, p=2.0)
    bowl.location = (x, 0.36, 0)
    box("tap", (0.03, 0.03, 0.28), (x, 0.13, 1.12), matte_black, bevel=0.006, parent=e)
    box("spout", (0.028, 0.16, 0.028), (x, 0.2, 1.25), matte_black, bevel=0.006, parent=e)


def p_vanity(e, x=0.2):
    box("vanity", (1.0, 0.48, 0.42), (x, 0.3, 0.68), oak, bevel=0.006, parent=e)
    box("vanity top", (1.02, 0.5, 0.025), (x, 0.3, 0.9), material("white quartz", (0.88, 0.88, 0.86), 0.2, coat=0.5), bevel=0.003, parent=e)
    for dx in (-0.25, 0.25):
        box("handle", (0.2, 0.015, 0.015), (x + dx, 0.545, 0.84), matte_black, bevel=0.003, parent=e)
    bowl = loft("countertop basin", [(0.2, 0.15, 0.912), (0.26, 0.19, 0.95), (0.27, 0.2, 1.04), (0.255, 0.185, 1.04),
                                      (0.24, 0.17, 0.98), (0.1, 0.07, 0.93)], ceramic, cap_bottom=False, parent=e, p=2.4)
    bowl.location = (x, 0.32, 0)
    box("mirror frame", (0.82, 0.03, 1.02), (x, T / 2 + 0.018, 1.7), matte_black, bevel=0.004, parent=e)
    box("mirror", (0.78, 0.01, 0.98), (x, T / 2 + 0.035, 1.7), mirror_m, bevel=0, parent=e)
    box("tap", (0.03, 0.03, 0.22), (x, 0.13, 1.03), chrome, bevel=0.006, parent=e)
    box("spout", (0.028, 0.14, 0.028), (x, 0.19, 1.13), chrome, bevel=0.006, parent=e)
    asset("ceramic_vase_01", (x + 0.38, 0.22, 0.915), 0.22, parent=e)


def p_tub(e, x=0.25):
    rings = [(0.66, 0.27, 0.0), (0.74, 0.32, 0.12), (0.83, 0.38, 0.42), (0.86, 0.4, 0.58), (0.86, 0.4, 0.6),
             (0.835, 0.375, 0.6), (0.82, 0.36, 0.56), (0.76, 0.31, 0.28), (0.66, 0.24, 0.1), (0.45, 0.15, 0.08)]
    tub = loft("bathtub", rings, ceramic, parent=e, p=2.3)
    tub.location = (x, 0.62, 0.025)
    sub = tub.modifiers.new("s", "SUBSURF"); sub.levels = 1; sub.render_levels = 2
    box("tub mixer", (0.035, 0.035, 1.05), (x + 1.0, 0.62, 0.53), matte_black, bevel=0.008, parent=e)
    box("tub spout", (0.2, 0.03, 0.03), (x + 0.92, 0.62, 1.04), matte_black, bevel=0.008, parent=e)
    box("bath stool", (0.32, 0.32, 0.45), (x - 1.0, 0.85, 0.25), oak, bevel=0.02, parent=e)
    asset("ceramic_vase_03", (x - 1.0, 0.85, 0.475), 0.3, parent=e)


def p_shower(e, x=0.2):
    g = box("shower glass", (0.012, 1.1, 2.0), (x + 0.5, 0.6, 1.04), glass, bevel=0, parent=e)
    box("glass profile", (0.03, 0.03, 2.0), (x + 0.5, 0.06, 1.04), matte_black, bevel=0, parent=e)
    box("glass bar", (0.02, 1.1, 0.02), (x + 0.5, 0.6, 2.04), matte_black, bevel=0, parent=e)
    box("shower arm", (0.025, 0.4, 0.025), (x - 0.2, 0.25, 2.2), matte_black, bevel=0.005, parent=e)
    bpy.ops.mesh.primitive_cylinder_add(vertices=64, radius=0.15, depth=0.015, location=(x - 0.2, 0.45, 2.19))
    o = bpy.context.object; o.data.materials.append(matte_black); o.parent = e; smooth(o)
    box("valve", (0.18, 0.03, 0.18), (x - 0.2, T / 2 + 0.015, 1.15), matte_black, bevel=0.01, parent=e)
    box("drain", (0.7, 0.08, 0.004), (x - 0.2, 0.25, 0.03), matte_black, bevel=0, parent=e)


def p_wc(e, x=0.3):
    wc = loft("wall-hung wc", [(0.17, 0.25, 0.32), (0.18, 0.27, 0.36), (0.185, 0.28, 0.42), (0.18, 0.27, 0.43)], ceramic, parent=e, p=3.0)
    wc.location = (x, 0.33, 0.0)
    sub = wc.modifiers.new("s", "SUBSURF"); sub.levels = 1; sub.render_levels = 2
    box("seat", (0.36, 0.5, 0.025), (x, 0.36, 0.445), ceramic, bevel=0.012, parent=e)
    box("flush plate", (0.24, 0.012, 0.16), (x, T / 2 + 0.006, 1.05), matte_black, bevel=0.004, parent=e)
    box("paper holder", (0.14, 0.06, 0.02), (x + 0.4, T / 2 + 0.03, 0.75), matte_black, bevel=0.004, parent=e)


def p_plant(e, name="potted_plant_02", x=0.8, y=0.5, h=1.3):
    asset(name, (x, y, 0.025), h, rot_z=random.uniform(0, 6.28), parent=e)


def p_living(e):
    asset("Ottoman_01", (-0.2, 0.9, 0.025), 0.42, parent=e)
    asset("coffee_table_round_01", (0.55, 0.7, 0.025), 0.45, parent=e)
    asset("ceramic_vase_03", (0.55, 0.7, 0.475), 0.28, parent=e)


def p_bar(e):
    box("counter", (1.6, 0.6, 0.9), (0.2, 0.45, 0.45), material("white lacquer", (0.85, 0.85, 0.83), 0.25, coat=0.4), bevel=0.01, parent=e)
    box("counter top", (1.7, 0.66, 0.03), (0.2, 0.47, 0.915), tile_mat("CN011", 170, 66, k=3), bevel=0.004, parent=e)
    for dx in (-0.25, 0.55):
        asset("bar_chair_round_01", (dx, 1.05, 0.025), 0.95, rot_z=math.pi, parent=e)


PROPS = {"washstand": p_washstand, "vanity": p_vanity, "tub": p_tub, "shower": p_shower, "wc": p_wc,
         "plant": p_plant, "living": p_living, "bar": p_bar}

# ================================================================== the hall
random.seed(7)
fl = tile_mat("CN015", 600, 600, k=1.2)
floor = plane("hall floor", W, D, (0, 0, 0), (0, 0, 0), material("hall floor", rough=0.32, img=lay("CN015", 600, 600, 1.2)[0],
                                                                   grout=lay("CN015", 600, 600, 1.2)[1], repeat=(W / 6, D / 6)))
plane("ceiling", W, D, (0, 0, H), (math.pi, 0, 0), ceiling_m)
plane("back wall", W, H, (0, -D / 2, H / 2), (math.pi / 2, 0, 0), white)
plane("left wall", D, H, (-W / 2, 0, H / 2), (math.pi / 2, 0, -math.pi / 2), white)
plane("right wall", D, H, (W / 2, 0, H / 2), (math.pi / 2, 0, math.pi / 2), white)
for (x, y, sx, sy) in [(0, -D / 2 + 0.006, W, 0.012), (-W / 2 + 0.006, 0, 0.012, D), (W / 2 - 0.006, 0, 0.012, D)]:
    box("skirting", (sx, sy, 0.08), (x, y, 0.04), material("skirting", (0.75, 0.75, 0.73), 0.5), bevel=0)

# booths: back row, then the left and right walls
by = -D / 2 + 0.45
booth("b_metro", (-6.0, by, 0), 0, "CN010", "CN003", [("vanity",), ("plant", "potted_plant_01", 0.95, 0.35, 0.9)], "SN001", "Metro White")
booth("b_aqua", (-3.0, by, 0), 0, "CN008", "CN007", [("shower",), ("plant", "calathea_orbifolia_01", -0.75, 1.2, 0.7)], "SN002", "Aqua Shield")
booth("b_calacatta", (0.0, by, 0), 0, "CN006", "CN006", [("washstand",), ("plant", "potted_plant_02", 0.85, 0.45, 1.25)], None, "Royal Calacatta")
booth("b_onyx", (3.0, by, 0), 0, "CN011", "CN005", [("tub",)], None, "Black Onyx")
booth("b_mosaic", (6.0, by, 0), 0, "CN013", "CN012", [("wc",), ("plant", "anthurium_botany_01", -0.7, 0.5, 0.6)], "SN003", "Pearl Mosaic")
booth("b_concrete", (-W / 2 + 0.45, -1.4, 0), -math.pi / 2, "CN015", "CN015", [("living",)], None, "Concrete Light")
booth("b_sand", (-W / 2 + 0.45, 1.9, 0), -math.pi / 2, "CN002", "CN002", [("bar",)], None, "Desert Sand")
booth("b_ocean", (W / 2 - 0.45, -1.4, 0), math.pi / 2, "CN014", "CN004", [("plant", "potted_plant_01", 0.6, 0.6, 1.1), ("plant", "potted_plant_02", -0.5, 0.9, 1.4)], None, "Garden Slate")
booth("b_terra", (W / 2 - 0.45, 1.9, 0), math.pi / 2, "CN001", "CN009", [("living",)], None, "Arctic Pearl")

# slab racks in the middle: big tiles fanned on black stands (like a real showroom)
for (cx, cy, rz, skus) in [(-2.6, -1.0, math.radians(18), ["CN012", "CN005", "CN002", "CN001"]),
                           (2.6, -1.0, math.radians(-18), ["CN003", "CN009", "CN014", "CN004"])]:
    for i, sku in enumerate(skus):
        tw, th = tile_size(sku)
        sw, sh = (0.6, 1.2) if max(tw, th) >= 120 or (tw, th) == (60, 60) else (0.6, 1.2)
        mat_ = tile_mat(sku, 60, 120, k=4, turn=False)
        dx = (i - 1.5) * 0.07
        box("slab", (sw * 1.4, 0.012, sh), (cx + dx * math.cos(rz), cy + dx * math.sin(rz), 0.64), mat_,
            rot=(math.radians(-14), 0, rz), bevel=0.002)
    box("rack", (1.1, 0.36, 0.05), (cx, cy + 0.1, 0.03), matte_black, rot=(0, 0, rz), bevel=0.004)
    box("rack back", (1.1, 0.03, 0.08), (cx, cy - 0.18, 0.1), matte_black, rot=(0, 0, rz), bevel=0.004)

# consultation desk with a screen for Ceramica Nova AI
desk_y = 1.6
box("desk", (2.2, 0.75, 0.74), (0, desk_y, 0.37), material("white lacquer", (0.85, 0.85, 0.83), 0.25, coat=0.4), bevel=0.01)
box("desk top", (2.3, 0.85, 0.04), (0, desk_y, 0.76), oak, bevel=0.006)
scr = text_image(os.path.join(TEX, "screen.png"), (1600, 900), [
    ("Ceramica Nova AI", FONT, 110, 300, (255, 255, 255)), ("Ask me about tiles, quantities and prices", FONT_L, 52, 460, (200, 210, 230))],
    (26, 46, 92), (255, 255, 255))
box("screen stand", (0.06, 0.06, 0.32), (0, desk_y - 0.15, 0.94), matte_black, bevel=0.004)
box("screen frame", (0.82, 0.03, 0.48), (0, desk_y - 0.13, 1.28), matte_black, bevel=0.006)
plane("screen", 0.78, 0.44, (0, desk_y - 0.112, 1.28), (math.pi / 2, 0, math.pi), material("screen", (1, 1, 1), 0.2, img=scr))
MATS["screen"].node_tree.nodes["Principled BSDF"].inputs["Emission Strength"].default_value = 0.0
for dx in (-0.6, 0.6):
    asset("bar_chair_round_01", (dx, desk_y - 0.8, 0), 0.95)
asset("potted_plant_02", (-1.5, desk_y + 0.1, 0), 1.6)

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
for x in (-6, -3, 0, 3, 6):                           # a light track over each back-row booth
    box("track", (2.6, 0.04, 0.04), (x, -2.9, H - 0.02), matte_black, bevel=0)
    for dx in (-0.75, 0.75):
        bpy.ops.object.light_add(type="SPOT", location=(x + dx, -2.9, H - 0.12)); L = bpy.context.object
        L.data.energy = 260; L.data.spot_size = math.radians(60); L.data.spot_blend = 0.7; L.data.shadow_soft_size = 0.05
        L.data.color = (1, 0.93, 0.84); L.rotation_euler = (math.radians(-38), 0, 0)
        bpy.ops.mesh.primitive_cylinder_add(vertices=24, radius=0.045, depth=0.13, location=(x + dx, -2.9, H - 0.08), rotation=(math.radians(-38), 0, 0))
        bpy.context.object.data.materials.append(matte_black)
for side in (-1, 1):                                  # tracks over the side booths
    for y in (-1.4, 1.9):
        box("track", (0.04, 2.6, 0.04), (side * (W / 2 - 3.0), y, H - 0.02), matte_black, bevel=0)
        for dy in (-0.75, 0.75):
            bpy.ops.object.light_add(type="SPOT", location=(side * (W / 2 - 3.0), y + dy, H - 0.12)); L = bpy.context.object
            L.data.energy = 260; L.data.spot_size = math.radians(60); L.data.spot_blend = 0.7; L.data.shadow_soft_size = 0.05
            L.data.color = (1, 0.93, 0.84); L.rotation_euler = (0, math.radians(side * 38), 0)
            bpy.ops.mesh.primitive_cylinder_add(vertices=24, radius=0.045, depth=0.13, location=(side * (W / 2 - 3.0), y + dy, H - 0.08), rotation=(0, math.radians(side * 38), 0))
            bpy.context.object.data.materials.append(matte_black)
glowm = bpy.data.materials.new("downlight"); glowm.use_nodes = True
em = glowm.node_tree.nodes.new("ShaderNodeEmission"); em.inputs["Strength"].default_value = 25; em.inputs["Color"].default_value = (1, 0.95, 0.88, 1)
glowm.node_tree.links.new(em.outputs[0], glowm.node_tree.nodes["Material Output"].inputs[0])
for x in (-6, -3, 0, 3, 6):
    for y in (-0.5, 2.8):
        bpy.ops.object.light_add(type="AREA", location=(x, y, H - 0.02)); L = bpy.context.object
        L.data.size = 0.5; L.data.energy = 110; L.data.color = (1, 0.96, 0.9)
        bpy.ops.mesh.primitive_circle_add(vertices=32, radius=0.08, fill_type="NGON", location=(x, y, H - 0.004))
        bpy.context.object.data.materials.append(glowm)

# ================================================================== spots, hotspots and rendering
SPOTS = [
    {"id": "s0", "name": "Outside the shop", "pos": (0.0, FRONT + 6.0), "look": (0, -1)},
    {"id": "s1", "name": "Entrance", "pos": (0.0, FRONT - 1.6), "look": (0, -1)},
    {"id": "s2", "name": "Bathrooms, left", "pos": (-4.5, -2.4), "look": (-1, -1)},
    {"id": "s3", "name": "Marble booth", "pos": (0.0, -2.6), "look": (0, -1)},
    {"id": "s4", "name": "Bathrooms, right", "pos": (4.5, -2.4), "look": (1, -1)},
    {"id": "s5", "name": "Floor tiles", "pos": (-5.6, 0.3), "look": (-1, 0)},
    {"id": "s6", "name": "Outdoor and floor", "pos": (5.6, 0.3), "look": (1, 0)},
    {"id": "s7", "name": "Ceramica Nova AI desk", "pos": (0.0, 0.2), "look": (0, -1)},
]
LINKS = {"s0": ["s1"], "s1": ["s0", "s7", "s5", "s6"], "s7": ["s1", "s3", "s2", "s4", "s5", "s6"], "s2": ["s3", "s5", "s7"],
         "s3": ["s2", "s4", "s7"], "s4": ["s3", "s6", "s7"], "s5": ["s2", "s7", "s1"], "s6": ["s4", "s7", "s1"]}


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
out["closed"] = "/images/showroom/s0_closed.jpg"
json.dump(out, open(os.path.join(args.out, "spots.json"), "w"), indent=1)
print("spots.json written")
