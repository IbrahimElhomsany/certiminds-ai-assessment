"""Fair test: today's AUTOMATIC path for a customer's own photo vs the new room AI, on the same photos.

Today's automatic path (what a customer gets after uploading a photo):
  1. SegFormer-B0 ADE20K labels floor / wall (same weights the browser uses; the browser runs an 8-bit copy).
  2. frontend/assets/segment.js fit() turns the labels into 4 floor dots, 4 wall dots and a furniture mask.
     We run the real fit() code in node, unchanged.
  3. The customer types the room width; the depth is guessed (visualiser.js rectShape). Here we give it a
     perfect customer: the width taken from our hand-marked version of the same room.
Each picture: photo | hand-marked (the best case, a person traced it) | today automatic | new AI.

    python -m tools.roomai_fairtest --out fair/ [--rooms living,kitchen]
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image

from backend.roomai import analyse, render
from tools.roomai_compare import GLOSS, ROOT, label, load_rooms, old_surface

SEGFORMER = "nvidia/segformer-b0-finetuned-ade-512-512"
FLOOR = {"floor", "road", "sidewalk", "path"}
GRID = 480
_seg = {}


def segformer_grid(photo: Image.Image):
    """Same as segment.js detect(): labels on a small copy, then a 480-wide grid of 0 other / 1 floor / 2 wall."""
    import torch
    from transformers import AutoImageProcessor, SegformerForSemanticSegmentation
    if not _seg:
        _seg["p"] = AutoImageProcessor.from_pretrained(SEGFORMER)
        _seg["m"] = SegformerForSemanticSegmentation.from_pretrained(SEGFORMER).eval()
    p, m = _seg["p"], _seg["m"]
    k = min(1, 640 / max(photo.size))
    small = photo.resize((round(photo.size[0] * k), round(photo.size[1] * k)), Image.LANCZOS)
    with torch.no_grad():
        logits = m(**p(images=small, return_tensors="pt")).logits
    lab = torch.nn.functional.interpolate(logits, size=small.size[::-1], mode="bilinear").argmax(1)[0].numpy()
    names = m.config.id2label
    gh = max(1, round(small.size[1] * GRID / small.size[0]))
    ys = np.minimum(small.size[1] - 1, ((np.arange(gh) + 0.5) * small.size[1] / gh).astype(int))
    xs = np.minimum(small.size[0] - 1, ((np.arange(GRID) + 0.5) * small.size[0] / GRID).astype(int))
    g = lab[ys][:, xs]
    out = np.zeros_like(g, dtype=np.uint8)
    for i, n in names.items():
        n = n.strip().lower()
        if n in FLOOR:
            out[g == int(i)] = 1
        elif n == "wall":
            out[g == int(i)] = 2
    return {"gw": GRID, "gh": int(gh), "lab": out.ravel().tolist()}


def js_fit(seg, W, H):
    """Run the browser's own fit() and rectShape() in node."""
    code = f"""
      const window = {{}}; const document = {{}};
      {(ROOT / 'frontend/assets/segment.js').read_text()}
      {(ROOT / 'frontend/assets/visualiser.js').read_text()}
      const seg = JSON.parse(require('fs').readFileSync(0, 'utf8'));
      seg.lab = Uint8Array.from(seg.lab);
      const r = window.NovaSegment.fit(seg, {W}, {H});
      if (!r) {{ console.log('null'); }} else {{
        const shape = p => p ? window.NovaVisualiser.rectShape(p, {W}, {H}) : null;
        console.log(JSON.stringify({{ floor: r.floor, wall: r.wall, keep: Array.from(r.keep), gw: r.gw, gh: r.gh,
                                     fshape: shape(r.floor), wshape: shape(r.wall) }}));
      }}"""
    out = subprocess.run(["node", "-e", code], input=json.dumps(seg), capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def today_auto(photo, room, ftex, fsize, wtex, wsize, fgloss, wgloss):
    W, H = photo.size
    res = js_fit(segformer_grid(photo), W, H)
    if not res or not res.get("floor"):
        return photo, "no floor found: customer must place dots by hand"
    keep = np.array(res["keep"], np.uint8).reshape(res["gh"], res["gw"]) * 255
    keep = np.asarray(Image.fromarray(keep).resize((W, H), Image.BILINEAR)) > 127
    # perfect customer: types the real width of the far floor edge (from our hand-marked room)
    hs = room["surfaces"]["floor"][0]
    hq = np.array(hs["quad"], float)
    q = np.array(res["floor"], float)
    width_cm = hs["size"][0] * np.linalg.norm(q[1] - q[0]) / np.linalg.norm(hq[1] - hq[0])
    depth_cm = min(6000, width_cm * res["fshape"]["along"])
    fl, fr, nr, nl = q.tolist()

    def down(a, b):
        y = H * 1.05
        t = (y - a[1]) / ((b[1] - a[1]) or 1e-6)
        return [a[0] + (b[0] - a[0]) * t, y] if t > 1 else b
    out = photo
    s = {"quad": q.tolist(), "size": [width_cm, depth_cm], "area": [fl, fr, down(fr, nr), down(fl, nl)], "keep": []}
    m, uv = old_surface(s, (W, H))
    out = render.tile(out, m & ~keep, uv, ftex, fsize, gloss=fgloss)
    note = "floor found"
    if res.get("wall"):
        wq = np.array(res["wall"], float)
        wh = width_cm * res["wshape"]["along"]
        s = {"quad": wq.tolist(), "size": [width_cm, wh], "area": wq.tolist(), "keep": []}
        m, uv = old_surface(s, (W, H))
        out = render.tile(out, m & ~keep, uv, wtex, wsize, gloss=wgloss)
        note += " + back wall"
    return out, note


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="fair")
    ap.add_argument("--tile", default="CN001")
    ap.add_argument("--wall-tile", default="CN015")
    ap.add_argument("--rooms", default="")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    cat = {c["sku"]: c for c in json.loads((ROOT / "data/catalog.json").read_text())}
    ft, wt = cat[a.tile], cat[a.wall_tile]
    ftex, wtex = Image.open(ROOT / f"images/{a.tile}.jpg"), Image.open(ROOT / f"images/{a.wall_tile}.jpg")
    fsize, wsize = (ft["tile_width_cm"], ft["tile_length_cm"]), (wt["tile_width_cm"], wt["tile_length_cm"])
    fg, wg = GLOSS.get(ft["finish"], 0.0), GLOSS.get(wt["finish"], 0.0)
    want = set(filter(None, a.rooms.split(",")))
    notes = {}
    for room in load_rooms():
        if want and room["id"] not in want:
            continue
        photo = Image.open(ROOT / room["photo"].lstrip("/")).convert("RGB")
        hand = photo
        for kind, surfs in room["surfaces"].items():
            for s in surfs:
                m, uv = old_surface(s, photo.size)
                f = kind == "floor"
                hand = render.tile(hand, m, uv, ftex if f else wtex, fsize if f else wsize, gloss=fg if f else wg)
        auto, note = today_auto(photo, room, ftex, fsize, wtex, wsize, fg, wg)
        res = analyse.analyse(photo)
        new = res["image"]
        for s in res["surfaces"]:
            f = s["name"] == "floor"
            new = render.tile(new, s["mask"], s["uv"], ftex if f else wtex, fsize if f else wsize, gloss=fg if f else wg)
        size = new.size
        panels = [label(photo.resize(size), "Photo"),
                  label(hand.resize(size), "Hand-marked (best case, traced by a person)"),
                  label(auto.resize(size), "Today, automatic: " + note),
                  label(new, "New AI, automatic")]
        sheet = Image.new("RGB", (size[0] * 2, size[1] * 2))
        for i, p in enumerate(panels):
            sheet.paste(p, ((i % 2) * size[0], (i // 2) * size[1]))
        sheet.save(out / f"{room['id']}.jpg", quality=88)
        notes[room["id"]] = note
        print(room["id"], note, flush=True)
    (out / "notes.json").write_text(json.dumps(notes, indent=2))


if __name__ == "__main__":
    main()
