"""Shop-only makeovers: turn the customer's own words into a plan made only of things we sell.

Why: the image AI draws whatever it is told. If "dark wood wall with lights" went to it word for word, the customer
would see wood panels and wall lights that we don't sell. So the words never go to the image AI. Instead:

1. READ  - an LLM (or, without a key, simple keyword rules) splits the words into parts: a tile for the floor or
           walls, a paint colour, a light mood, or something else (furniture, lamps, panels, wallpaper...).
2. MATCH - each tile part is matched to OUR catalogue, respecting where the tile may go (floor / wall).
           A close match is swapped in and the customer is told ("We don't sell X. Closest from our range: Y").
           No good match: that surface is left as it is and our nearest tiles are suggested.
           Things we don't sell at all are left out, and the customer is told.
3. WRITE - the server writes the image instruction itself from the plan (SKU names, a colour word, a light mood).

The tile picked in the grid always wins over the words for the same surface.
"""
from __future__ import annotations

import json
import os
import re
from collections import OrderedDict
from typing import Literal, Optional

from pydantic import BaseModel, Field

from backend.catalog import CATALOG
from backend.observability import log_event

LIGHTS = {"brighter": "Make the room look brighter, as if on a sunny day, without adding any lamps.",
          "warmer": "Give the room a warmer, golden light, without adding any lamps.",
          "cooler": "Give the room a cooler, crisp daylight, without adding any lamps.",
          "evening": "Show the room in soft evening light, using only the lights already in the photo.",
          "daylight": "Show the room in clear natural daylight, without adding any lamps."}
LIGHT_WORDS = {"brighter": "a brighter room", "warmer": "warmer light", "cooler": "cooler light",
               "evening": "evening light", "daylight": "daylight"}
COLOUR = re.compile(r"^[a-z][a-z \-]{1,28}[a-z]$")
TONE = {"dark": {"CN005", "CN011", "CN014"}, "light": {"CN001", "CN007", "CN010", "CN013", "CN015"},
        "warm": {"CN002", "CN003", "CN009", "CN012"}}


# ------------------------------------------------------------------ what the reader returns
class Ask(BaseModel):
    asked: str = Field(max_length=80, description="the customer's words for this one part, short")
    kind: Literal["tile", "paint", "light", "other"] = Field(
        description="tile = a floor or wall covering (tiles, marble, wood, stone, concrete, parquet, panels, cladding, "
                    "wallpaper, carpet); paint = a wall paint colour; light = mood of the light (brighter, warmer, "
                    "evening); other = anything else (furniture, lamps, plants, objects, ceilings, windows)")
    surface: Literal["floor", "wall", "none"]
    sku: Optional[str] = Field(default=None, description="our tile SKU that best fits, only for kind=tile")
    match: Literal["same", "close", "none"] = Field(
        default="none", description="same = this is what they asked for; close = same look family, a fair swap; "
                                    "none = we have nothing like it")
    colour: Optional[str] = Field(default=None, description="for kind=paint: plain colour words, e.g. 'light grey'")
    light: Optional[Literal["brighter", "warmer", "cooler", "evening", "daylight"]] = None


class Reading(BaseModel):
    asks: list[Ask] = Field(default_factory=list, max_length=8)


# ------------------------------------------------------------------ reader 1: keyword rules (always available)
LOOKS = [  # words -> our tiles, best first; surfaces are checked later
    (r"wood|oak|timber|parquet|walnut|teak|plank", ["CN003"]),
    (r"calacatta|white marble|marble", ["CN006", "CN001", "CN011"]),
    (r"onyx|black|dark marble|nero", ["CN011", "CN005"]),
    (r"slate", ["CN014", "CN005"]),
    (r"concrete|cement|microcement|industrial|grey|gray", ["CN015", "CN005"]),
    (r"graphite|charcoal|anthracite", ["CN005", "CN011"]),
    (r"terracotta|terra|rustic|clay|cotto", ["CN009", "CN012"]),
    (r"sand|beige|travertine|limestone|stone", ["CN002", "CN012", "CN015"]),
    (r"subway|metro|brick", ["CN010"]),
    (r"mosaic|small squares", ["CN013"]),
    (r"aqua|turquoise|teal|blue|green|zellige", ["CN008"]),
    (r"anti.?slip|non.?slip|pool|grip", ["CN004", "CN014"]),
    (r"ivory|cream|white", ["CN007", "CN001", "CN010"]),
]
NOT_TILES = r"(?:panel|cladding|wallpaper|carpet|laminate|vinyl|slat|batten|board|veneer|real wood|solid wood)s?"
OBJECTS = (r"\b(?:(?:wall |ceiling |spot ?|pendant |led )?lights|lamps?|spot ?lights?|sconces?|chandeliers?|led strips?|"
           r"sofas?|couch(?:es)?|(?:arm)?chairs?|tables?|beds?|rugs?|curtains?|plants?|trees?|furniture|tvs?|art|"
           r"paintings?|pictures?|mirrors?|shel(?:f|ves)|cabinets?|ceilings?|windows?|doors?|fireplaces?|"
           r"kitchen islands?|bathtubs?|toilets?)\b")
PAINT_COLOURS = (r"light grey|dark grey|warm grey|grey|gray|beige|warm beige|white|off white|cream|sage green|olive|"
                 r"green|navy|blue|black|terracotta|pink|blush|yellow|mustard|taupe|brown|charcoal")


def _surface(part: str) -> str:
    if re.search(r"\bwalls?\b|backsplash|splashback|feature wall", part):
        return "wall"
    if re.search(r"\bfloors?\b|flooring|ground|terrace|patio", part):
        return "floor"
    return "none"


def read_rules(text: str) -> Reading:
    asks: list[Ask] = []
    for raw in re.split(r"[,.;!]| and | plus | but | also ", text.lower()):
        part = raw.strip()
        if len(part) < 3:
            continue
        had_obj = False
        for m in dict.fromkeys(x.group(0) for x in re.finditer(OBJECTS, part)):     # lamps, sofas...: never drawn
            asks.append(Ask(asked=m, kind="other", surface="none"))
            part, had_obj = part.replace(m, " "), True
        part = re.sub(r"\s+", " ", re.sub(r"\b(with|add|some|a|an|the)\s*$", "", part.strip())).strip()
        if len(part) < 3 or not re.search(r"[a-z]{3}", part):
            continue
        surface = _surface(part)
        light = next((k for k, w in (("brighter", r"bright|lighter room|more light|sunny|sunlight"),
                                     ("warmer", r"warm(er)? light|cosy|cozy|golden"),
                                     ("cooler", r"cool(er)? light|crisp"), ("evening", r"evening|night|sunset"),
                                     ("daylight", r"daylight|daytime|natural light")) if re.search(w, part)), None)
        if light:
            asks.append(Ask(asked=part[:80], kind="light", surface="none", light=light))
            continue
        look = next(((re.search(pat, part).group(0), skus) for pat, skus in LOOKS if re.search(pat, part)), None)
        colour = re.search(PAINT_COLOURS, part)
        other = re.search(NOT_TILES, part)
        tone = re.search(r"\b(dark|light|warm|white|black)\b", part)
        short = " ".join(dict.fromkeys(w for w in ((tone.group(0) if tone else None), (look[0] if look else None),
                                                   (other.group(0) if other else None)) if w)) or part
        if (re.search(r"\bpaint|colou?r", part) or (colour and not re.search(r"tiles?|look|marble|stone|wood|concrete", part))) \
                and surface in ("wall", "none") and colour and not other:
            asks.append(Ask(asked=part[:80], kind="paint", surface="wall", colour=colour.group(0)))
        elif other:                    # real wood panels, wallpaper, carpet: not tiles, a tile look-alike is only "close"
            asks.append(Ask(asked=short[:80], kind="tile", surface=surface if surface != "none" else "wall",
                            sku=look[1][0] if look else None, match="close" if look else "none"))
        elif look:
            asks.append(Ask(asked=short[:80], kind="tile", surface=surface if surface != "none" else "floor",
                            sku=look[1][0], match="same"))
        elif re.search(r"tiles?|marble|stone|floor|wall", part):
            asks.append(Ask(asked=part[:80], kind="tile", surface=surface if surface != "none" else "floor", match="none"))
        elif not had_obj:              # leftovers like "add a" next to an object are not a wish of their own
            asks.append(Ask(asked=part[:80], kind="other", surface="none"))
    return Reading(asks=asks)


# ------------------------------------------------------------------ reader 2: the LLM (when the shop has a key)
SYSTEM = """You read what a tile-shop customer wants changed in a photo of their room.
Split it into parts and fill the schema. Our tiles (sku | name | look | finish | where it may go):
{tiles}
Rules: pick a sku only from this list, and only one allowed on that surface. Real wood, panels, cladding, wallpaper,
carpet, vinyl or laminate are not tiles: if one of our tiles has a similar look, match=close, else match=none.
Lamps, wall lights, furniture, plants and other objects are kind=other. A plain colour for the walls is kind=paint."""


def _llm_on() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY")) and os.getenv("LLM_PROVIDER", "anthropic") == "anthropic" \
        and os.getenv("MAKEOVER_READER", "llm") == "llm"


def read_llm(text: str) -> Reading:
    from backend.providers import AnthropicProvider
    tiles = "\n".join(f"{p.sku} | {p.name} | {p.style} | {p.finish} | {'/'.join(p.surfaces)}" for p in CATALOG.values())
    r = AnthropicProvider(effort="low").generate(SYSTEM.format(tiles=tiles), f"Customer wrote: {text}", Reading, 25)
    return Reading.model_validate(json.loads(r.text))


def read(text: str) -> tuple[Reading, str]:
    if _llm_on():
        try:
            return read_llm(text), "llm"
        except Exception as e:                       # no answer, bad answer: the rules still give a safe plan
            log_event("makeover_reader_fallback", error=type(e).__name__)
    return read_rules(text), "rules"


# ------------------------------------------------------------------ the plan
class Line(BaseModel):
    kind: Literal["tile", "swap", "missing", "paint", "light", "left_out"]
    text: str
    sku: Optional[str] = None
    surface: Optional[str] = None
    suggest: list[str] = Field(default_factory=list)


class Plan(BaseModel):
    floor: Optional[str] = None
    wall: Optional[str] = None
    paint: Optional[str] = None
    light: Optional[str] = None
    lines: list[Line] = Field(default_factory=list)
    needs_ok: bool = False             # something was swapped or left out: show it before drawing
    reader: str = "none"

    @property
    def empty(self) -> bool:
        return not (self.floor or self.wall or self.paint or self.light)


def fits(sku: Optional[str], surface: str) -> bool:
    return bool(sku) and sku in CATALOG and surface in CATALOG[sku].surfaces


def suggestions(surface: str, words: str, first: Optional[str] = None, n: int = 3) -> list[str]:
    ours = [s for s, p in CATALOG.items() if surface in p.surfaces]
    tone = next((t for t in TONE if t in words), None)
    ranked = sorted(ours, key=lambda s: (s != first, not (tone and s in TONE[tone]), s))
    return ranked[:n]


def _phrase(asked: str) -> str:
    return asked.strip().rstrip(".!").strip()


_cache: "OrderedDict[tuple, Plan]" = OrderedDict()


def make_plan(text: str, floor: Optional[str] = None, wall: Optional[str] = None) -> Plan:
    text = " ".join((text or "").split())
    key = (text.lower(), floor, wall)
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]
    plan, picked = Plan(floor=floor, wall=wall), {"floor": floor, "wall": wall}
    for s, sku in (("floor", floor), ("wall", wall)):
        if sku:
            plan.lines.append(Line(kind="tile", sku=sku, surface=s, text=f"{CATALOG[sku].name} on the {s}{'s' if s == 'wall' else ''} (your pick)"))
    if text:
        reading, plan.reader = read(text)
        for a in reading.asks:
            asked, where = _phrase(a.asked), ("floor" if a.surface == "floor" else "walls")
            if a.kind == "tile":
                surface = a.surface if a.surface in ("floor", "wall") else "floor"
                if picked[surface]:                      # the tile picked in the grid wins
                    plan.lines.append(Line(kind="left_out", text=f"\"{asked}\": you picked {CATALOG[picked[surface]].name} for the {where}, so we used that."))
                    continue
                if getattr(plan, surface):               # already filled by an earlier part of the words
                    continue
                if a.match in ("same", "close") and fits(a.sku, surface):
                    setattr(plan, surface, a.sku)
                    name = CATALOG[a.sku].name
                    if a.match == "same":
                        plan.lines.append(Line(kind="tile", sku=a.sku, surface=surface, text=f"{name} on the {where}"))
                    else:
                        plan.lines.append(Line(kind="swap", sku=a.sku, surface=surface,
                                               text=f"We don't sell \"{asked}\". Closest from our range: {name}.",
                                               suggest=[s for s in suggestions(surface, asked.lower(), a.sku) if s != a.sku]))
                else:
                    plan.lines.append(Line(kind="missing", surface=surface,
                                           text=f"We don't sell \"{asked}\" for the {where}, so we'll leave the {where} as {'it is' if surface == 'floor' else 'they are'}. Pick one of ours instead:",
                                           suggest=suggestions(surface, asked.lower(), a.sku if fits(a.sku, surface) else None)))
            elif a.kind == "paint":
                colour = (a.colour or "").lower().strip()
                if plan.wall:
                    plan.lines.append(Line(kind="left_out", text=f"\"{asked}\": the walls get {CATALOG[plan.wall].name} tiles, so no paint."))
                elif COLOUR.match(colour) and not plan.paint:
                    plan.paint = colour
                    plan.lines.append(Line(kind="paint", text=f"Walls painted {colour} (paint is not a product we sell)"))
            elif a.kind == "light" and a.light in LIGHTS and not plan.light:
                plan.light = a.light
                plan.lines.append(Line(kind="light", text=f"{LIGHT_WORDS[a.light].capitalize()} (no new lamps are added)"))
            elif a.kind == "other":
                plan.lines.append(Line(kind="left_out", text=f"\"{asked}\" is left out. The AI only puts in our tiles, a wall paint colour or a change of light."))
    plan.needs_ok = any(l.kind in ("swap", "missing", "left_out") for l in plan.lines)
    _cache[key] = plan
    while len(_cache) > 512:
        _cache.popitem(last=False)
    return plan


def instructions(plan: Plan) -> list[str]:
    """The only extra sentences that reach the image AI: written by us from the plan, never the customer's words."""
    out = []
    if plan.paint:
        out.append(f"WALL PAINT: repaint the main painted walls a flat {plan.paint} colour.")
    if plan.light:
        out.append("LIGHT: " + LIGHTS[plan.light])
    return out
