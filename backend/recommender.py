"""Product recommendation engine (E.7).

Two stages:
1. HARD CONSTRAINTS remove unsuitable products completely:
   floor/wall, indoor/outdoor, slip rating (BR-01), wall-only never on floors (BR-02).
2. SOFT PREFERENCES give a score to rank what is left:
   style, colour, budget fit, price preference, commercial suitability (BR-03), stock.
Then the top 3 are returned (BR-09), each with human-readable reasons.

This is deterministic code, so the same requirements always give the same ranking
and every decision can be explained and tested. The LLM only phrases the result.
"""
from dataclasses import dataclass, field
from typing import Optional

import re

from backend.catalog import CATALOG, Product
from backend.schemas import CustomerRequirements
from backend.tools import StockStatus, calculate_tile_quantity, check_inventory

STYLE_SYNONYMS = {
    "marble": ["marble", "luxury marble"], "luxury": ["luxury", "luxury marble", "marble"],
    "wood": ["wood"], "stone": ["stone", "natural", "slate", "rustic"], "modern": ["modern", "contemporary", "minimal", "industrial"],
    "rustic": ["rustic", "stone"], "minimal": ["minimal", "contemporary"], "industrial": ["industrial", "contemporary"],
    "slate": ["slate", "stone"], "subway": ["subway"], "contemporary": ["contemporary", "modern"],
    "decorative": ["decorative"], "natural": ["natural", "stone"],
}


def style_targets(style: str) -> set[str]:
    """'wood-look', 'Wood effect', 'luxury marble style' -> the catalog style words to match."""
    s = re.sub(r"[-_]", " ", style.lower())
    for w in (" look", " effect", " style", " finish"):
        s = s.replace(w, "")
    s = s.strip()
    targets = set(STYLE_SYNONYMS.get(s, [s]))
    for key, syns in STYLE_SYNONYMS.items():
        if re.search(rf"\b{key}\b", s):
            targets.update(syns)
    return targets


@dataclass
class Recommendation:
    sku: str
    name: str
    score: float
    price_per_m2: float
    slip_rating: Optional[str]
    stock_status: str
    immediately_available: bool
    estimated_subtotal: Optional[float]
    boxes: Optional[int]
    reasons: list[str] = field(default_factory=list)


@dataclass
class Rejection:
    sku: str
    name: str
    reasons: list[str]


def hard_constraint_failures(p: Product, req: CustomerRequirements) -> list[str]:
    """Returns the list of rules a product breaks. Empty list = allowed."""
    fails = []
    surface = req.floor_or_wall
    if surface and surface not in p.surfaces:
        fails.append(f"it isn't made for {surface}s" + (" (it's a wall-only tile, BR-02)" if surface == "floor" else ""))
    if req.indoor_outdoor and req.indoor_outdoor not in p.locations:
        fails.append(f"it isn't made for {req.indoor_outdoor} use")
    outdoor_floor = req.indoor_outdoor == "outdoor" and surface != "wall"
    # bathroom and shower floors are wet areas: the slip-rating guide says R11 or higher
    wet_room = any(w in (req.application or "").lower() for w in ("bath", "shower", "pool", "wet"))
    if (outdoor_floor or wet_room or req.slip_requirement == "high") and surface != "wall" and p.slip_value < 11:
        fails.append(f"its slip rating ({p.slip_rating or 'none'}) is too slippery for this area, which needs R11 or higher (BR-01)")
    if req.traffic_level == "high" and surface != "wall" and p.material != "porcelain":
        fails.append("it's not porcelain, so it won't last on a busy floor (BR-03)")
    if req.traffic_level == "high" and surface != "wall" and p.slip_value < 10:
        fails.append(f"its slip rating ({p.slip_rating}) is too low for a busy commercial floor")
    return fails


def score_product(p: Product, req: CustomerRequirements, wants_cheap: bool, wants_luxury: bool,
                  required_area: Optional[float]):
    score, reasons = 50.0, []
    if req.traffic_level == "high" and p.commercial:
        score += 25; reasons.append("made for busy commercial floors")
    if req.slip_requirement == "high" and p.slip_value >= 12:
        score += 8; reasons.append(f"top anti-slip rating ({p.slip_rating})")
    elif p.slip_value >= 11:
        reasons.append(f"anti-slip ({p.slip_rating})")
    if req.preferred_style:
        targets = style_targets(req.preferred_style)
        if any(t in p.style.lower() for t in targets):
            score += 20; reasons.append(f"the {req.preferred_style.lower()} look you asked for")
    if req.preferred_color and req.preferred_color.lower() in (p.name + " " + p.style).lower():
        score += 8; reasons.append(f"in the {req.preferred_color.lower()} tone you asked for")
    if wants_cheap:
        score += max(0, 40 - p.price_per_m2 / 3.64); reasons.append(f"budget-friendly at QAR {p.price_per_m2:.0f} per m²")
    if wants_luxury:
        score += p.price_per_m2 / 3.64 / 3; reasons.append("premium finish")

    subtotal = boxes = None
    status, available = None, True
    if required_area:
        q = calculate_tile_quantity(p.sku, required_area)
        subtotal, boxes = q.material_subtotal, q.box_count
        inv = check_inventory(p.sku, q.total_purchasable_area_m2)
        status, available = inv.status.value, inv.immediately_available
        if inv.status != StockStatus.AVAILABLE:
            score -= 40 if req.urgency == "high" else 20
            reasons.append(f"only {inv.stock_m2:g} m² in stock (you need {q.total_purchasable_area_m2:g} m²)")
        else:
            reasons.append("in stock")
        if req.budget is not None:
            if subtotal <= req.budget:
                score += 15; reasons.append(f"estimated QAR {subtotal:,.2f} fits the QAR {req.budget:,.0f} budget")
            else:
                score -= min(30, (subtotal - req.budget) / req.budget * 50)
                reasons.append(f"estimated QAR {subtotal:,.2f} is above the QAR {req.budget:,.0f} budget")
    return score, reasons, subtotal, boxes, status, available


def recommend(req: CustomerRequirements, wants_cheap: bool = False, wants_luxury: bool = False,
              max_results: int = 3) -> tuple[list[Recommendation], list[Rejection]]:
    recs, rejected = [], []
    for p in CATALOG.values():
        fails = hard_constraint_failures(p, req)
        if fails:
            rejected.append(Rejection(p.sku, p.name, fails))
            continue
        score, reasons, subtotal, boxes, status, available = score_product(
            p, req, wants_cheap, wants_luxury, req.area_m2)
        recs.append(Recommendation(p.sku, p.name, round(score, 1), p.price_per_m2, p.slip_rating,
                                   status or "NOT_CHECKED", available, subtotal, boxes, reasons))
    recs.sort(key=lambda r: (-r.score, r.price_per_m2, r.sku))     # ties broken deterministically
    return recs[:max_results], rejected


def check_suitability(sku: str, req: CustomerRequirements) -> list[str]:
    """For requests like 'I want Arctic Pearl for my pool deck' (scenario 5)."""
    return hard_constraint_failures(CATALOG[sku], req)


SET_KEYWORDS = {      # bathroom sets (sold per set) and the words customers use for them
    "SN001": [r"luna", r"\bbasins?\b", r"wash ?basins?", r"\bsinks?\b", r"مغسلة"],
    "SN002": [r"aria", r"shower sets?", r"hand ?shower", r"shower ?heads?", r"rain ?shower", r"shower mixer", r"\bدش\b"],
    "SN003": [r"\bwc\b", r"toilets?", r"water closet", r"مرحاض"],
}
BUNDLE_WORDS = r"complete bathroom|full bathroom|bathroom bundle|whole bathroom|everything for the bathroom"
NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5}


def find_mentioned_sets(text: str) -> dict[str, int]:
    """{sku: quantity} for bathroom sets named in the text. "2 basins" -> 2, otherwise 1.
    "complete bathroom" means one of each."""
    t = text.lower()
    if re.search(BUNDLE_WORDS, t):
        return {sku: 1 for sku in SET_KEYWORDS}
    found = {}
    for sku, patterns in SET_KEYWORDS.items():
        for pat in patterns:
            m = re.search(pat, t)
            if m:
                before = t[:m.start()].split()[-2:]          # "2 basins", "two luna basins"
                qty = 1
                for w in reversed(before):
                    if w.isdigit() and 0 < int(w) <= 50:
                        qty = int(w)
                        break
                    if w in NUMBER_WORDS:
                        qty = NUMBER_WORDS[w]
                        break
                found[sku] = qty
                break
    return found


def find_mentioned_skus(text: str) -> list[str]:
    t = text.lower()
    return [p.sku for p in CATALOG.values() if p.sku.lower() in t or p.name.lower() in t]
