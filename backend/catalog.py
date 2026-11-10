"""Authoritative product catalog.

The catalog is the single source of truth for prices, stock and tile sizes.
The LLM never supplies these values; tools always look them up here (BR-04, BR-06).
"""
import json
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

CATALOG_PATH = Path(__file__).resolve().parent.parent / "data" / "catalog.json"

# The official assessment catalog is priced in USD. The shop sells in Qatari riyal, so prices are
# converted ONCE, here, at the fixed peg (1 USD = 3.64 QAR). Everything after this point is QAR.
CURRENCY = "QAR"
USD_TO_QAR = Decimal("3.64")


def usd_to_qar(usd: float) -> float:
    return float((Decimal(str(usd)) * USD_TO_QAR).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


class Product(BaseModel):
    sku: str
    name: str
    material: Literal["porcelain", "ceramic"]
    tile_width_cm: int = Field(gt=0)
    tile_length_cm: int = Field(gt=0)
    application: str                      # original text from the official catalog
    surfaces: list[Literal["floor", "wall"]]
    locations: list[Literal["indoor", "outdoor"]]
    commercial: bool
    finish: str
    slip_rating: Optional[str]            # "R9".."R12", None for wall-only ceramics
    price_per_m2: float = Field(ge=0)     # in QAR; negative prices are rejected at load time
    currency: str
    price_usd: Optional[float] = None     # the official catalog price, kept for traceability
    stock_m2: float = Field(ge=0)
    tiles_per_box: int = Field(gt=0)
    style: str
    largest_batch_m2: Optional[float] = None   # biggest single production batch (None = batches unknown)

    @property
    def slip_value(self) -> int:
        """R12 -> 12, None -> 0. Makes 'R11 or above' a simple number comparison."""
        return int(self.slip_rating[1:]) if self.slip_rating else 0


class UnknownSKUError(ValueError):
    pass


def load_catalog(path: Path = CATALOG_PATH) -> dict[str, Product]:
    with open(path) as f:
        rows = json.load(f)
    for p in rows:
        if p["currency"] == "USD":
            p["price_usd"] = p["price_per_m2"]
            p["price_per_m2"], p["currency"] = usd_to_qar(p["price_per_m2"]), CURRENCY
    return {p["sku"]: Product(**p) for p in rows}


CATALOG: dict[str, Product] = load_catalog()


def get_product(sku: str, catalog: Optional[dict[str, Product]] = None) -> Product:
    catalog = catalog if catalog is not None else CATALOG
    product = catalog.get(sku.strip().upper())
    if product is None:
        raise UnknownSKUError(f"Unknown SKU: {sku}")
    return product


# ------------------------------------------------------------------ bathroom products (sold per set)
# Added for the Qatar showroom demo; not part of the original assessment catalog. They are priced
# directly in QAR and counted in whole units, not square meters.
SANITARY_PATH = CATALOG_PATH.parent / "sanitary.json"


class SanitaryProduct(BaseModel):
    sku: str
    name: str
    category: Literal["basin", "shower", "wc"]
    description: str
    includes: list[str]
    finish: str
    style: str
    rooms: list[str]
    price: float = Field(ge=0)            # QAR per set
    currency: Literal["QAR"]
    unit: Literal["set"]
    stock_units: int = Field(ge=0)


def load_sanitary(path: Path = SANITARY_PATH) -> dict[str, SanitaryProduct]:
    with open(path) as f:
        return {p["sku"]: SanitaryProduct(**p) for p in json.load(f)}


SANITARY: dict[str, SanitaryProduct] = load_sanitary()


def get_sanitary(sku: str) -> SanitaryProduct:
    product = SANITARY.get(sku.strip().upper())
    if product is None:
        raise UnknownSKUError(f"Unknown SKU: {sku}")
    return product


def replace_catalog(tiles: dict[str, Product], sets: dict[str, SanitaryProduct]) -> None:
    """Swap in a fresh snapshot (from the Nova Studio database). The dicts are updated IN PLACE
    because tools, the recommender and the agent all imported these same objects."""
    CATALOG.clear()
    CATALOG.update(tiles)
    SANITARY.clear()
    SANITARY.update(sets)
