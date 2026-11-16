"""Grout, adhesive and spacers: what a tiling job needs besides the tiles, and how much of it.

The amounts use the usual trade rules of thumb, worked out from the tile's own size, so the customer gets a
sensible starting quantity with the reason shown. They are estimates: the installer has the final say.

- Grout (kg per m2)  = (L + W) / (L x W) x joint width x tile thickness x 1.6     (all in mm; 1.6 = grout density)
- Adhesive (kg / m2) = about 4 for tiles up to 60 cm, 5.5 for larger tiles (thicker bed, back-buttering)
- Spacers            = one bag of 250 for about every 10 m2
Every estimate is rounded UP to whole bags and has about 10% spare, like the tiles.
"""
from __future__ import annotations

import math

JOINT_MM = 3            # the joint width the visualiser uses by default
THICKNESS_MM = 9        # typical porcelain / ceramic floor and wall tile

# demo products (prices in QAR); kind "acc" = accessory, sold per bag
ACCESSORIES = [
    {"sku": "GR01", "name": "Nova Grout 5 kg, White", "group": "grout", "colour": "white", "hex": "#f4f2ee", "price": 32.0, "bag_kg": 5, "stock": 400},
    {"sku": "GR02", "name": "Nova Grout 5 kg, Light grey", "group": "grout", "colour": "light grey", "hex": "#d9d6cf", "price": 32.0, "bag_kg": 5, "stock": 400},
    {"sku": "GR03", "name": "Nova Grout 5 kg, Sand beige", "group": "grout", "colour": "sand beige", "hex": "#cdb89a", "price": 32.0, "bag_kg": 5, "stock": 300},
    {"sku": "GR04", "name": "Nova Grout 5 kg, Anthracite", "group": "grout", "colour": "anthracite", "hex": "#4a4a4a", "price": 34.0, "bag_kg": 5, "stock": 300},
    {"sku": "AD01", "name": "Nova Flex Tile Adhesive C2TE 20 kg", "group": "adhesive", "price": 48.0, "bag_kg": 20, "stock": 600},
    {"sku": "SP01", "name": "Tile spacers 3 mm, bag of 250", "group": "spacers", "price": 12.0, "stock": 500},
]
BY_SKU = {a["sku"]: a for a in ACCESSORIES}

DARK_WORDS = ("onyx", "graphite", "slate", "black", "anthracite", "charcoal")
WARM_WORDS = ("sand", "oak", "wood", "terra", "rustic", "beige", "travertine", "desert")


def grout_colour(name: str, style: str = "") -> str:
    """A grout that blends in: dark tiles get anthracite, warm tiles sand beige, white tiles white, others light grey."""
    t = f"{name} {style}".lower()
    if any(w in t for w in DARK_WORDS):
        return "GR04"
    if any(w in t for w in WARM_WORDS):
        return "GR03"
    if any(w in t for w in ("white", "pearl", "ivory", "arctic", "calacatta", "metro")):
        return "GR01"
    return "GR02"


def grout_kg_per_m2(width_cm: float, length_cm: float) -> float:
    w, l = width_cm * 10, length_cm * 10
    return (w + l) / (w * l) * JOINT_MM * THICKNESS_MM * 1.6


def adhesive_kg_per_m2(width_cm: float, length_cm: float) -> float:
    return 5.5 if max(width_cm, length_cm) > 60 else 4.0


def bags(kg: float, bag_kg: float) -> int:
    return max(1, math.ceil(kg * 1.1 / bag_kg))


def needs_for(tiles: list[dict]) -> list[dict]:
    """tiles: [{"sku", "name", "style", "width_cm", "length_cm", "m2"}] -> suggested accessories with quantity and why.
    One grout per tile (its own colour), one adhesive and one spacer line for the whole job."""
    out, adhesive_kg, total_m2 = [], 0.0, 0.0
    grout: dict[str, dict] = {}
    for t in tiles:
        m2 = float(t["m2"])
        total_m2 += m2
        adhesive_kg += adhesive_kg_per_m2(t["width_cm"], t["length_cm"]) * m2
        g = grout_colour(t["name"], t.get("style", ""))
        kg = grout_kg_per_m2(t["width_cm"], t["length_cm"]) * m2
        e = grout.setdefault(g, {"kg": 0.0, "for": []})
        e["kg"] += kg
        e["for"].append(t["name"])
    for g, e in grout.items():
        a = BY_SKU[g]
        n = bags(e["kg"], a["bag_kg"])
        out.append({"sku": g, "group": "grout", "quantity": n,
                    "why": f"Fills the joints between the tiles. About {e['kg']:.1f} kg for {' and '.join(e['for'])} "
                           f"with {JOINT_MM} mm joints, so {n} bag{'s' if n > 1 else ''} with spare. "
                           f"{a['colour'].capitalize()} blends in with {'these tiles' if len(e['for']) > 1 else 'this tile'}."})
    if total_m2:
        a = BY_SKU["AD01"]
        n = bags(adhesive_kg, a["bag_kg"])
        out.append({"sku": "AD01", "group": "adhesive", "quantity": n,
                    "why": f"Fixes the tiles to the floor or wall. Porcelain needs a flexible C2 adhesive: about "
                           f"{adhesive_kg:.0f} kg for {total_m2:g} m², so {n} bag{'s' if n > 1 else ''}."})
        n = max(1, math.ceil(total_m2 / 10))
        out.append({"sku": "SP01", "group": "spacers", "quantity": n,
                    "why": f"Keeps every joint exactly {JOINT_MM} mm, so the lines stay straight. About one bag per 10 m²."})
    return out
