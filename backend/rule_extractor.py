"""Rule-based requirement extractor (no LLM).

Used in two places:
1. MockProvider - lets the whole app and test-suite run offline with no API key.
2. Degraded mode - if every real LLM provider is down, the gateway can still
   understand basic requests (Incident 1 in Section D).
It is deliberately simple keyword matching; the real LLM handles nuance.
"""
import re

from backend.catalog import usd_to_qar

from backend.schemas import CustomerRequirements, StructuredAnalysis

ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٬٫", "01234567890123456789,.")
_TASHKEEL = re.compile(r"[\u064B-\u0652\u0640]")          # vowel marks and tatweel


def normalize(text: str) -> str:
    """Lower-case, Western digits, and one spelling for Arabic letters (أ إ آ -> ا, ى -> ي, ة -> ه), no vowel marks.
    Arabic words in the lists below are matched after the same normalising."""
    t = _TASHKEEL.sub("", text.lower().translate(ARABIC_DIGITS))
    return t.translate(str.maketrans("أإآىة", "ااايه"))


OUTDOOR = ["outdoor", "balcony", "terrace", "patio", "garden", "pool", "deck", "outside",
           "شرفة", "بلكونة", "بلكونه", "خارجي", "خارجية", "حديقة", "مسبح", "حوض السباحة", "تراس", "سطح", "فناء", "حوش"]
INDOOR = ["indoor", "living room", "bedroom", "kitchen", "bathroom", "restaurant", "office", "shop",
          "home", "house", "داخلي", "داخلية", "مطبخ", "حمام", "دورة مياه", "صالة", "صالون", "مجلس", "غرفة نوم",
          "غرفة المعيشة", "مكتب", "مطعم", "محل", "فيلا", "شقة", "بيت", "منزل"]
WALL = ["wall", "backsplash", "splashback", "جدار", "حائط", "حيطان", "جدران", "الحيط"]
FLOOR = ["floor", "flooring", "deck", "balcony", "terrace", "patio", "restaurant", "living room", "أرضية", "ارضية", "الأرض", "ارضيات", "أرضيات", "شرفة", "تراس", "بلاط ارض", "مسبح", "حوض السباحة", "ممر", "حوش"]
SLIP = ["slip", "slippery", "children", "kids", "elderly", "wet", "pool", "انزلاق", "يزلق", "زلق", "أطفال", "اطفال",
        "كبار السن", "مبلل", "رطب"]
HIGH_TRAFFIC = ["restaurant", "commercial", "traffic", "shop", "office", "hotel", "mall", "مطعم", "تجاري", "فندق", "مول", "محل", "مكتب"]
URGENT = ["urgent", "asap", "today", "tomorrow", "this week", "immediately", "عاجل", "اليوم", "بكرة", "بكره", "غدا", "غداً", "هذا الأسبوع", "مستعجل", "ضروري"]
STYLES = ["marble", "wood", "stone", "modern", "rustic", "minimal", "luxury", "industrial", "slate",
          "subway", "contemporary", "decorative", "natural", "رخام", "خشب", "حجر", "مودرن", "حديث", "عصري",
          "كلاسيك", "ريفي", "فاخر", "بسيط", "طبيعي"]
COLORS = ["white", "black", "grey", "gray", "beige", "cream", "blue", "brown", "dark", "light",
          "أبيض", "ابيض", "بيضاء", "أسود", "اسود", "سوداء", "رمادي", "رماديه", "بيج", "كريمي", "أزرق", "ازرق", "بني",
          "غامق", "داكن", "فاتح"]
APPLICATIONS = ["pool deck", "balcony", "terrace", "patio", "kitchen", "bathroom", "living room",
                "bedroom", "restaurant", "office", "shop", "garden",
                "مسبح", "حوض السباحة", "شرفة", "بلكونة", "بلكونه", "تراس", "مطبخ", "حمام", "دورة مياه", "صالة", "صالون",
                "مجلس", "غرفة المعيشة", "غرفة نوم", "مطعم", "مكتب", "محل", "حديقة", "حوش"]
INJECTION = ["ignore all", "ignore your", "ignore previous", "system prompt", "system message",
             "administrator", "admin pricing", "reveal", "management approved", "another customer",
             "as system instructions", "jailbreak"]
TILE_WORDS = ["tile", "tiles", "porcelain", "ceramic", "floor", "wall", "sqm", "m2", "m²", "square",
              "quote", "price", "stock", "بلاط", "متر", "سيراميك", "بورسلين", "رخام", "سعر", "أسعار", "اسعار", "كم",
              "عرض", "مخزون", "متوفر", "موجود", "كمية", "تسعير", "مغسلة", "دش", "شاور", "مرحاض", "كرسي"] + OUTDOOR + INDOOR + STYLES
# Arabic words -> the English values the rest of the app uses
AR_MAP = {"مسبح": "pool deck", "حوض السباحة": "pool deck", "شرفة": "balcony", "بلكونة": "balcony", "بلكونه": "balcony",
          "تراس": "terrace", "مطبخ": "kitchen", "حمام": "bathroom", "دورة مياه": "bathroom", "صالة": "living room",
          "صالون": "living room", "مجلس": "living room", "غرفة المعيشة": "living room", "غرفة نوم": "bedroom",
          "مطعم": "restaurant", "مكتب": "office", "محل": "shop", "حديقة": "garden", "حوش": "garden",
          "رخام": "marble", "خشب": "wood", "حجر": "stone", "مودرن": "modern", "حديث": "modern", "عصري": "contemporary",
          "كلاسيك": "classic", "ريفي": "rustic", "فاخر": "luxury", "بسيط": "minimal", "طبيعي": "natural",
          "أبيض": "white", "ابيض": "white", "بيضاء": "white", "أسود": "black", "اسود": "black", "سوداء": "black",
          "رمادي": "grey", "رماديه": "grey", "بيج": "beige", "كريمي": "cream", "أزرق": "blue", "ازرق": "blue",
          "بني": "brown", "غامق": "dark", "داكن": "dark", "فاتح": "light"}
QUOTE_WORDS = ["quote", "quotation", "invoice", "عرض سعر", "عرض أسعار", "عرض اسعار", "تسعيرة", "تسعيره", "فاتورة",
               "سوي لي عرض", "جهز لي عرض", "جهز العرض", "اطلب", "أطلب"]
GREETING_AR = ["مرحبا", "السلام", "اهلا", "أهلا", "هلا", "صباح الخير", "مساء الخير", "هاي"]
OBJECTION = ["competitor", "cheaper", "% less", "why should i buy", "ارخص", "أرخص عند", "منافس", "غالي", "غالية"]


for _name in ("OUTDOOR", "INDOOR", "WALL", "FLOOR", "SLIP", "HIGH_TRAFFIC", "URGENT", "STYLES", "COLORS",
              "APPLICATIONS", "TILE_WORDS", "QUOTE_WORDS", "GREETING_AR", "OBJECTION"):
    globals()[_name] = [normalize(w) for w in globals()[_name]]
AR_MAP = {normalize(k): v for k, v in AR_MAP.items()}


def _has(text: str, words: list[str]) -> bool:
    return any(w in text for w in words)


def _first(text: str, words: list[str]):
    return next((w for w in words if w in text), None)


def extract(message: str) -> StructuredAnalysis:
    t = normalize(message)
    req = CustomerRequirements()

    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:m2|m²|sqm|sq\.? ?m|square met|متر|م2|م²|م\b)", t)
    if m:
        req.area_m2 = float(m.group(1))
    usd = re.search(r"\$\s?(\d[\d,]*)|(\d[\d,]*)\s?(?:usd|dollars?)\b", t)
    qar = re.search(r"(?:qar|qr|ر\.ق)\s?(\d[\d,]*)|(\d[\d,]*)\s?(?:qar|qr|riyals?|ريال)", t)
    if usd:          # prices are in QAR, so a dollar budget is converted at the fixed 3.64 peg
        req.budget = usd_to_qar(float(next(g for g in usd.groups() if g).replace(",", "")))
    elif qar:
        req.budget = float(next(g for g in qar.groups() if g).replace(",", ""))
    else:
        b = re.search(r"budget[^\d]{0,20}(\d[\d,]*)", t)
        if b:
            req.budget = float(b.group(1).replace(",", ""))

    if _has(t, OUTDOOR):
        req.indoor_outdoor = "outdoor"
    elif _has(t, INDOOR):
        req.indoor_outdoor = "indoor"
    if _has(t, WALL):
        req.floor_or_wall = "wall"
    elif _has(t, FLOOR):
        req.floor_or_wall = "floor"

    app = _first(t, APPLICATIONS)
    req.application = AR_MAP.get(app, app)
    style, color = _first(t, STYLES), _first(t, COLORS)
    req.preferred_style = AR_MAP.get(style, style)
    req.preferred_color = AR_MAP.get(color, color)
    if _has(t, SLIP) or req.indoor_outdoor == "outdoor":
        req.slip_requirement = "high"
    if _has(t, HIGH_TRAFFIC):
        req.traffic_level = "high"
    if _has(t, URGENT):
        req.urgency = "high"

    if _has(t, INJECTION):
        intent = "unsafe_request"
    elif _has(t, OBJECTION):
        intent = "objection"
    elif _has(t, QUOTE_WORDS):
        intent = "quotation_request"
    elif not _has(t, TILE_WORDS) and (re.search(r"\b(hi|hello|hey|good (morning|afternoon|evening))\b", t)
                                      or _has(t, GREETING_AR)):
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
