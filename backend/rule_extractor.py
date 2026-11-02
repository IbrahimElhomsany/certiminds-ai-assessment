"""Rule-based requirement extractor (no LLM).

Used in two places:
1. MockProvider - lets the whole app and test-suite run offline with no API key.
2. Degraded mode - if every real LLM provider is down, the gateway can still
   understand basic requests (Incident 1 in Section D).
It is deliberately simple keyword matching; the real LLM handles nuance.
"""
import re

from backend.schemas import CustomerRequirements, StructuredAnalysis

ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")

OUTDOOR = ["outdoor", "balcony", "terrace", "patio", "garden", "pool", "deck", "outside",
           "شرفة", "خارجي", "خارجية", "حديقة", "مسبح"]
INDOOR = ["indoor", "living room", "bedroom", "kitchen", "bathroom", "restaurant", "office", "shop",
          "home", "house", "داخلي", "مطبخ", "حمام"]
WALL = ["wall", "backsplash", "splashback", "جدار", "حائط"]
FLOOR = ["floor", "flooring", "deck", "balcony", "terrace", "patio", "restaurant", "living room", "أرضية", "شرفة"]
SLIP = ["slip", "slippery", "children", "kids", "elderly", "wet", "pool", "انزلاق"]
HIGH_TRAFFIC = ["restaurant", "commercial", "traffic", "shop", "office", "hotel", "mall", "مطعم", "تجاري"]
URGENT = ["urgent", "asap", "today", "tomorrow", "this week", "immediately", "عاجل", "اليوم"]
STYLES = ["marble", "wood", "stone", "modern", "rustic", "minimal", "luxury", "industrial", "slate",
          "subway", "contemporary", "decorative", "natural", "رخام", "خشب"]
COLORS = ["white", "black", "grey", "gray", "beige", "cream", "blue", "brown", "dark", "light"]
APPLICATIONS = ["pool deck", "balcony", "terrace", "patio", "kitchen", "bathroom", "living room",
                "bedroom", "restaurant", "office", "shop", "garden", "شرفة", "مطبخ", "حمام"]
INJECTION = ["ignore all", "ignore your", "ignore previous", "system prompt", "system message",
             "administrator", "admin pricing", "reveal", "management approved", "another customer",
             "as system instructions", "jailbreak"]
TILE_WORDS = ["tile", "tiles", "porcelain", "ceramic", "floor", "wall", "sqm", "m2", "m²", "square",
              "quote", "price", "stock", "بلاط", "متر"] + OUTDOOR + STYLES


def _has(text: str, words: list[str]) -> bool:
    return any(w in text for w in words)


def _first(text: str, words: list[str]):
    return next((w for w in words if w in text), None)


def extract(message: str) -> StructuredAnalysis:
    t = message.lower().translate(ARABIC_DIGITS)
    req = CustomerRequirements()

    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:m2|m²|sqm|sq\.? ?m|square met|متر)", t)
    if m:
        req.area_m2 = float(m.group(1))
    b = re.search(r"\$\s?(\d[\d,]*)|(\d[\d,]*)\s?(?:usd|dollars)|budget[^\d]{0,20}(\d[\d,]*)", t)
    if b:
        req.budget = float(next(g for g in b.groups() if g).replace(",", ""))

    if _has(t, OUTDOOR):
        req.indoor_outdoor = "outdoor"
    elif _has(t, INDOOR):
        req.indoor_outdoor = "indoor"
    if _has(t, WALL):
        req.floor_or_wall = "wall"
    elif _has(t, FLOOR):
        req.floor_or_wall = "floor"

    app = _first(t, APPLICATIONS)
    req.application = {"شرفة": "balcony", "مطبخ": "kitchen", "حمام": "bathroom"}.get(app, app)
    req.preferred_style = _first(t, STYLES)
    req.preferred_color = _first(t, COLORS)
    if _has(t, SLIP) or req.indoor_outdoor == "outdoor":
        req.slip_requirement = "high"
    if _has(t, HIGH_TRAFFIC):
        req.traffic_level = "high"
    if _has(t, URGENT):
        req.urgency = "high"

    if _has(t, INJECTION):
        intent = "unsafe_request"
    elif _has(t, ["competitor", "cheaper", "% less", "why should i buy"]):
        intent = "objection"
    elif _has(t, ["quote", "quotation", "invoice"]):
        intent = "quotation_request"
    elif not _has(t, TILE_WORDS) and _has(t, ["hello", "hi ", "hey", "مرحبا"]):
        intent = "greeting"
    elif not _has(t, TILE_WORDS):
        intent = "off_topic"
    else:
        intent = "recommendation_request"

    missing = []
    if intent == "recommendation_request":
        if req.application is None:
            missing.append("application")
        if req.floor_or_wall is None:
            missing.append("floor_or_wall")
        if req.area_m2 is None:
            missing.append("area_m2")
        if req.budget is None:
            missing.append("budget")

    actions = {"unsafe_request": ["refuse_and_redirect"], "off_topic": ["redirect_to_tiles"],
               "greeting": ["ask_project_details"], "objection": ["explain_value_with_facts"],
               "quotation_request": ["generate_quote"]}.get(
        intent, ["ask_clarifying_questions"] if missing else ["recommend_products", "calculate_quantity"])

    return StructuredAnalysis(intent=intent, customer_requirements=req, missing_information=missing,
                              recommended_actions=actions, confidence=0.5)
