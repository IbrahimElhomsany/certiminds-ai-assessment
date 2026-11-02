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
        fails.append(f"not suitable for {surface}s" + (" (wall-only product, BR-02)" if surface == "floor" else ""))
    if req.indoor_outdoor and req.indoor_outdoor not in p.locations:
        fails.append(f"not rated for {req.indoor_outdoor} use")
    outdoor_floor = req.indoor_outdoor == "outdoor" and surface != "wall"
    if (outdoor_floor or req.slip_requirement == "high") and surface != "wall" and p.slip_value < 11:
        fails.append(f"slip rating {p.slip_rating or 'none'} is below R11 (BR-01)")
    if req.traffic_level == "high" and surface != "wall" and p.material != "porcelain":
        fails.append("not porcelain, unsuitable for high-traffic flooring (BR-03)")
    if req.traffic_level == "high" and surface != "wall" and p.slip_value < 10:
        fails.append(f"slip rating {p.slip_rating} is too low for a busy commercial floor")
    return fails


def score_product(p: Product, req: CustomerRequirements, wants_cheap: bool, wants_luxury: bool,
                  required_area: Optional[float]):
    score, reasons = 50.0, []
    if req.traffic_level == "high" and p.commercial:
        score += 25; reasons.append("designed for commercial use (BR-03)")
    if req.slip_requirement == "high" and p.slip_value >= 12:
        score += 8; reasons.append(f"highest slip resistance {p.slip_rating}")
    elif p.slip_value >= 11:
        reasons.append(f"slip rating {p.slip_rating}")
    if req.preferred_style:
        targets = STYLE_SYNONYMS.get(req.preferred_style.lower(), [req.preferred_style.lower()])
        if any(t in p.style.lower() for t in targets):
            score += 20; reasons.append(f"matches the {req.preferred_style} style ({p.style})")
    if req.preferred_color and req.preferred_color.lower() in (p.name + " " + p.style).lower():
        score += 8; reasons.append(f"matches colour preference '{req.preferred_color}'")
    if wants_cheap:
        score += max(0, 40 - p.price_per_m2); reasons.append(f"budget-friendly at ${p.price_per_m2:.0f}/m2")
    if wants_luxury:
        score += p.price_per_m2 / 3; reasons.append("premium product")

    subtotal = boxes = None
    status, available = None, True
    if required_area:
        q = calculate_tile_quantity(p.sku, required_area)
        subtotal, boxes = q.material_subtotal, q.box_count
        inv = check_inventory(p.sku, q.total_purchasable_area_m2)
        status, available = inv.status.value, inv.immediately_available
        if inv.status != StockStatus.AVAILABLE:
            score -= 40 if req.urgency == "high" else 20
            reasons.append(f"only {inv.stock_m2:g} m2 in stock (need {q.total_purchasable_area_m2:g} m2)")
        else:
            reasons.append("in stock for the full quantity")
        if req.budget is not None:
            if subtotal <= req.budget:
                score += 15; reasons.append(f"estimated ${subtotal:,.2f} fits the ${req.budget:,.0f} budget")
            else:
                score -= min(30, (subtotal - req.budget) / req.budget * 50)
                reasons.append(f"estimated ${subtotal:,.2f} is above the ${req.budget:,.0f} budget")
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


def find_mentioned_skus(text: str) -> list[str]:
    t = text.lower()
    return [p.sku for p in CATALOG.values() if p.sku.lower() in t or p.name.lower() in t]
