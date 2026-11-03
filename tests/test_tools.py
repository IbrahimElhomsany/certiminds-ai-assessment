"""Unit tests for the deterministic tools. These run without any LLM or API key."""
import pytest

from backend.catalog import CATALOG, UnknownSKUError
from backend.tools import (QuoteItemRequest, StockStatus, calculate_tile_quantity,
                           check_inventory, compute_quantity, generate_quote)


# ---------- E.8 calculator
def test_assessment_example_40m2_60x60():
    r = compute_quantity(40, 60, 60, 4, 10, price_per_m2=112.84)     # $31 x 3.64
    assert r.area_with_wastage_m2 == 44.0
    assert r.tile_area_m2 == 0.36
    assert r.tiles_before_rounding == pytest.approx(122.2222, abs=1e-4)
    assert r.tiles_after_rounding == 123
    assert r.box_count == 31                 # 123 / 4 = 30.75 -> 31 boxes
    assert r.purchasable_tiles == 124
    assert r.total_purchasable_area_m2 == pytest.approx(44.64)
    assert r.material_subtotal == 5037.18    # 44.64 m2 x QAR 112.84 (= the brief's $1,383.84 x 3.64)


def test_exact_division_does_not_overcount():
    # 36 m2 / 0.36 = exactly 100 tiles; float maths can give 100.00000001 -> 101
    r = compute_quantity(36, 60, 60, 4, 0)
    assert r.tiles_after_rounding == 100 and r.box_count == 25


def test_layout_sets_wastage_and_price_from_catalog():
    simple = calculate_tile_quantity("CN004", 25, "simple")
    diagonal = calculate_tile_quantity("CN004", 25, "diagonal")
    assert simple.wastage_percent == 10 and diagonal.wastage_percent == 15
    assert simple.price_per_m2 == CATALOG["CN004"].price_per_m2


@pytest.mark.parametrize("area", [0, -5])
def test_calculator_rejects_non_positive_area(area):
    with pytest.raises(ValueError):
        calculate_tile_quantity("CN001", area)


def test_unknown_sku():
    with pytest.raises(UnknownSKUError):
        calculate_tile_quantity("CN999", 10)


# ---------- E.9 inventory
def test_inventory_available():
    assert check_inventory("CN001", 25).status == StockStatus.AVAILABLE


def test_inventory_partial_royal_calacatta_200m2():
    r = check_inventory("CN006", 200)       # scenario 8: only 190 m2 in stock
    assert r.status == StockStatus.PARTIALLY_AVAILABLE
    assert r.shortfall_m2 == 10 and r.immediately_available is False


def test_inventory_out_of_stock(monkeypatch):
    monkeypatch.setitem(CATALOG, "CN001", CATALOG["CN001"].model_copy(update={"stock_m2": 0}))
    assert check_inventory("CN001", 5).status == StockStatus.OUT_OF_STOCK


def test_inventory_boundary_exact_stock_is_available():
    assert check_inventory("CN006", 190).status == StockStatus.AVAILABLE


# ---------- E.10 quotation
def test_quote_totals_are_deterministic():
    q = generate_quote("USR-1001", [QuoteItemRequest(sku="CN001", area_m2=40)], discount_percent=10)
    assert q.subtotal == 5037.18
    assert q.discount_amount == 503.72
    assert q.total == 4533.46
    assert q.currency == "QAR" and q.approval_required is False and q.status == "DRAFT"


def test_discount_above_12_requires_human_approval():
    q = generate_quote("USR-1001", [QuoteItemRequest(sku="CN001", area_m2=40)], discount_percent=15)
    assert q.approval_required is True
    assert q.status == "PENDING_HUMAN_APPROVAL" and "12" in q.approval_reason


def test_exactly_12_percent_does_not_need_approval():
    q = generate_quote("USR-1001", [QuoteItemRequest(sku="CN001", area_m2=40)], discount_percent=12)
    assert q.approval_required is False


def test_negative_quantity_rejected():
    with pytest.raises(ValueError):
        QuoteItemRequest(sku="CN001", area_m2=-3)


def test_other_currency_rejected():
    with pytest.raises(ValueError):
        generate_quote("USR-1001", [QuoteItemRequest(sku="CN001", area_m2=10)], currency="EUR")


def test_quote_uses_catalog_price_not_caller_price():
    # There is no price argument at all: Royal Calacatta is always $61/m2 = QAR 222.04 (scenario 7)
    q = generate_quote("USR-1001", [QuoteItemRequest(sku="CN006", area_m2=10)])
    assert q.products[0].unit_price_per_m2 == 222.04


# ---------- QAR catalog and bathroom products
def test_catalog_prices_are_converted_once_to_qar():
    from decimal import Decimal
    from backend.catalog import CATALOG, USD_TO_QAR
    p = CATALOG["CN001"]
    assert p.currency == "QAR" and p.price_usd == 31 and p.price_per_m2 == 112.84 and USD_TO_QAR == Decimal("3.64")
    assert all(x.currency == "QAR" for x in CATALOG.values())


def test_bathroom_products_load_and_unknown_sku_is_refused():
    import pytest
    from backend.catalog import SANITARY, UnknownSKUError, get_sanitary
    assert {p.category for p in SANITARY.values()} == {"basin", "shower", "wc"}
    assert get_sanitary("sn001").price == 1250 and get_sanitary("SN001").unit == "set"
    with pytest.raises(UnknownSKUError):
        get_sanitary("SN999")


def test_budget_in_dollars_is_converted_and_riyal_budget_is_kept():
    from backend.rule_extractor import extract
    budget = lambda msg: extract(msg).customer_requirements.budget
    assert budget("budget $1,000 for 20 m2") == 3640
    assert budget("my budget is QAR 5,000") == 5000
    assert budget("about 8000 riyals for the bathroom") == 8000


def test_quote_mixes_tiles_and_bathroom_sets():
    q = generate_quote("USR-1001", [QuoteItemRequest(sku="CN001", area_m2=40),
                                    QuoteItemRequest(sku="SN002", quantity=2)])
    assert q.subtotal == 5037.18 + 3300 and q.products[1].unit == "set" and q.products[1].quantity_units == 2
    with pytest.raises(ValueError):          # a set needs a quantity, a tile needs an area
        generate_quote("USR-1001", [QuoteItemRequest(sku="SN002", area_m2=5)])
    with pytest.raises(ValueError):
        generate_quote("USR-1001", [QuoteItemRequest(sku="CN001", quantity=3)])
    with pytest.raises(ValueError):          # zero / negative sets rejected (rule 2)
        QuoteItemRequest(sku="SN001", quantity=0)
