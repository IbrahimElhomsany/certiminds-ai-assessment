"""Deterministic business tools (C2, E.8, E.9, E.10).

Rules:
- All arithmetic happens here, never in the LLM (BR-06).
- Prices, sizes and stock come only from the catalog (BR-04, quote rule 7).
- Decimal is used instead of float so that e.g. 100 tiles never becomes 100.0000001 and rounds up to 101.
"""
import math
import uuid
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field

from backend.catalog import Product, get_product

CURRENCY = "USD"
MAX_DISCOUNT_WITHOUT_APPROVAL = Decimal("12")          # BR-07
WASTAGE_BY_LAYOUT = {"simple": Decimal("10"), "diagonal": Decimal("15"), "complex": Decimal("15")}  # BR-05


def _money(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _area(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


# ------------------------ E.8 quantity calculator

class QuantityResult(BaseModel):
    sku: Optional[str] = None
    required_area_m2: float
    wastage_percent: float
    area_with_wastage_m2: float
    tile_area_m2: float
    tiles_before_rounding: float
    tiles_after_rounding: int
    tiles_per_box: int
    box_count: int
    purchasable_tiles: int          # full boxes only
    total_purchasable_area_m2: float
    price_per_m2: Optional[float] = None
    material_subtotal: Optional[float] = None
    currency: str = CURRENCY
    pricing_basis: str = "total_purchasable_area_m2 x catalog price_per_m2 (customer pays for full boxes)"


def compute_quantity(area_m2: float, tile_width_cm: float, tile_length_cm: float,
                     tiles_per_box: int, wastage_percent: float,
                     price_per_m2: Optional[float] = None) -> QuantityResult:
    """Pure maths. Matches the input example in E.8."""
    if area_m2 <= 0:
        raise ValueError("area_m2 must be positive")
    if tile_width_cm <= 0 or tile_length_cm <= 0 or tiles_per_box <= 0:
        raise ValueError("tile size and tiles_per_box must be positive")
    if not 0 <= wastage_percent <= 50:
        raise ValueError("wastage_percent must be between 0 and 50")
    if price_per_m2 is not None and price_per_m2 < 0:
        raise ValueError("price_per_m2 cannot be negative")

    area = Decimal(str(area_m2))
    wastage = Decimal(str(wastage_percent))
    tile_area = Decimal(str(tile_width_cm)) * Decimal(str(tile_length_cm)) / Decimal("10000")

    area_with_wastage = area * (1 + wastage / 100)
    tiles_exact = area_with_wastage / tile_area
    tiles_rounded = math.ceil(tiles_exact)                 # can't buy part of a tile
    boxes = math.ceil(Decimal(tiles_rounded) / tiles_per_box)   # can't buy part of a box
    purchasable_tiles = boxes * tiles_per_box
    purchasable_area = purchasable_tiles * tile_area

    subtotal = None
    if price_per_m2 is not None:
        subtotal = float(_money(purchasable_area * Decimal(str(price_per_m2))))

    return QuantityResult(
        required_area_m2=float(area),
        wastage_percent=float(wastage),
        area_with_wastage_m2=float(_area(area_with_wastage)),
        tile_area_m2=float(_area(tile_area)),
        tiles_before_rounding=float(_area(tiles_exact)),
        tiles_after_rounding=tiles_rounded,
        tiles_per_box=tiles_per_box,
        box_count=boxes,
        purchasable_tiles=purchasable_tiles,
        total_purchasable_area_m2=float(_area(purchasable_area)),
        price_per_m2=price_per_m2,
        material_subtotal=subtotal,
    )


def calculate_tile_quantity(sku: str, area_m2: float,
                            layout: Literal["simple", "diagonal", "complex"] = "simple",
                            wastage_percent: Optional[float] = None) -> QuantityResult:
    """Tool exposed to the LLM. The LLM gives only SKU, area and layout;
    tile size, box size and price are looked up from the catalog."""
    p: Product = get_product(sku)
    wastage = wastage_percent if wastage_percent is not None else float(WASTAGE_BY_LAYOUT[layout])
    result = compute_quantity(area_m2, p.tile_width_cm, p.tile_length_cm,
                              p.tiles_per_box, wastage, p.price_per_m2)
    result.sku = p.sku
    return result


# ----------------- E.9 inventory

class StockStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    PARTIALLY_AVAILABLE = "PARTIALLY_AVAILABLE"
    OUT_OF_STOCK = "OUT_OF_STOCK"


class InventoryResult(BaseModel):
    sku: str
    product_name: str
    required_area_m2: float
    stock_m2: float
    status: StockStatus
    shortfall_m2: float
    immediately_available: bool     # BR-08


def check_inventory(sku: str, required_area_m2: float) -> InventoryResult:
    if required_area_m2 <= 0:
        raise ValueError("required_area_m2 must be positive")
    p = get_product(sku)
    if p.stock_m2 == 0:
        status = StockStatus.OUT_OF_STOCK
    elif p.stock_m2 < required_area_m2:
        status = StockStatus.PARTIALLY_AVAILABLE
    else:
        status = StockStatus.AVAILABLE
    return InventoryResult(
        sku=p.sku, product_name=p.name, required_area_m2=required_area_m2, stock_m2=p.stock_m2,
        status=status, shortfall_m2=max(0.0, round(required_area_m2 - p.stock_m2, 4)),
        immediately_available=status == StockStatus.AVAILABLE,
    )


# ---------------------------------------------------------------- E.10 quotation

class QuoteItemRequest(BaseModel):
    sku: str
    area_m2: float = Field(gt=0)        # negative / zero quantities rejected (rule 2)
    layout: Literal["simple", "diagonal", "complex"] = "simple"


class QuoteLine(BaseModel):
    sku: str
    product_name: str
    quantity_boxes: int
    quantity_m2: float
    unit_price_per_m2: float
    line_subtotal: float
    stock_status: StockStatus


class Quote(BaseModel):
    quotation_id: str
    customer_id: str
    currency: str
    products: list[QuoteLine]
    subtotal: float
    discount_percent: float
    discount_amount: float
    total: float
    approval_required: bool
    approval_reason: Optional[str]
    status: Literal["DRAFT", "PENDING_HUMAN_APPROVAL"]


def generate_quote(customer_id: str, items: list[QuoteItemRequest],
                   discount_percent: float = 0, currency: str = CURRENCY) -> Quote:
    """There is deliberately no 'approved' parameter: approval can only be granted
    by a human outside this function, so the LLM cannot fabricate it (rule 6)."""
    if currency != CURRENCY:
        raise ValueError(f"Only {CURRENCY} quotations are supported (rule 1)")
    if not items:
        raise ValueError("A quotation needs at least one product")
    if not 0 <= discount_percent <= 100:
        raise ValueError("discount_percent must be between 0 and 100")

    lines, subtotal = [], Decimal("0")
    for item in items:
        p = get_product(item.sku)
        if p.currency != currency:
            raise ValueError("Mixed currencies in one quotation are not allowed (rule 1)")
        qty = calculate_tile_quantity(p.sku, item.area_m2, item.layout)
        line_total = Decimal(str(qty.material_subtotal))
        subtotal += line_total
        lines.append(QuoteLine(
            sku=p.sku, product_name=p.name, quantity_boxes=qty.box_count,
            quantity_m2=qty.total_purchasable_area_m2, unit_price_per_m2=p.price_per_m2,
            line_subtotal=float(line_total),
            stock_status=check_inventory(p.sku, qty.total_purchasable_area_m2).status,
        ))

    discount = Decimal(str(discount_percent))
    discount_amount = _money(subtotal * discount / 100)
    needs_approval = discount > MAX_DISCOUNT_WITHOUT_APPROVAL
    return Quote(
        quotation_id=f"Q-{uuid.uuid4().hex[:10].upper()}",
        customer_id=customer_id,
        currency=currency,
        products=lines,
        subtotal=float(_money(subtotal)),
        discount_percent=float(discount),
        discount_amount=float(discount_amount),
        total=float(_money(subtotal - discount_amount)),
        approval_required=needs_approval,
        approval_reason=(f"Discount {discount}% exceeds the {MAX_DISCOUNT_WITHOUT_APPROVAL}% limit (BR-07); "
                         "a human manager must approve." if needs_approval else None),
        status="PENDING_HUMAN_APPROVAL" if needs_approval else "DRAFT",
    )
