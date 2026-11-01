"""Authoritative product catalog.

The catalog is the single source of truth for prices, stock and tile sizes.
The LLM never supplies these values; tools always look them up here (BR-04, BR-06).
"""
import json
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

CATALOG_PATH = Path(__file__).resolve().parent.parent / "data" / "catalog.json"


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
    price_per_m2: float = Field(ge=0)     # negative prices are rejected at load time
    currency: str
    stock_m2: float = Field(ge=0)
    tiles_per_box: int = Field(gt=0)
    style: str

    @property
    def slip_value(self) -> int:
        """R12 -> 12, None -> 0. Makes 'R11 or above' a simple number comparison."""
        return int(self.slip_rating[1:]) if self.slip_rating else 0


class UnknownSKUError(ValueError):
    pass


def load_catalog(path: Path = CATALOG_PATH) -> dict[str, Product]:
    with open(path) as f:
        return {p["sku"]: Product(**p) for p in json.load(f)}


CATALOG: dict[str, Product] = load_catalog()


def get_product(sku: str, catalog: Optional[dict[str, Product]] = None) -> Product:
    catalog = catalog if catalog is not None else CATALOG
    product = catalog.get(sku.strip().upper())
    if product is None:
        raise UnknownSKUError(f"Unknown SKU: {sku}")
    return product
