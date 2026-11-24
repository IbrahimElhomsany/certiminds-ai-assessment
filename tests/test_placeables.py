"""Place products: the true-size 3D products for the visualiser, and the bathroom products added with them."""
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from backend import main
from backend.catalog import SANITARY
from backend.db.database import Base, SessionLocal, engine, init_db
from backend.db.models import Batch, Product
from backend.db.seed import ensure_accessories, seed_tenant

ROOT = Path(__file__).resolve().parent.parent
client = TestClient(main.app)


def test_placeables_have_true_sizes_models_and_pictures():
    body = client.get("/api/v1/placeables").json()
    rows = body["products"]
    assert body["currency"] == "QAR" and len(rows) >= 20
    for r in rows:
        assert len(r["size_cm"]) == 3 and all(0 < v < 400 for v in r["size_cm"]), r["id"]
        assert r["mount"] in ("floor", "wall")
        for key in ("model", "image"):                       # the files are in the repository, next to the shop's images
            assert (ROOT / r[key].lstrip("/")).is_file(), r[key]
    sold = [r for r in rows if r["sold"]]
    assert {r["id"] for r in sold} <= set(SANITARY) and len(sold) == 7
    assert all(r["price"] == SANITARY[r["id"]].price for r in sold)   # the shop's own price, not a copy
    decor = [r for r in rows if not r["sold"]]
    assert decor and all(r["price"] is None and r["source"] for r in decor)
    bath = next(r for r in rows if r["id"] == "SN005")
    assert bath["mount"] == "floor" and abs(bath["size_cm"][0] - 172) <= 1   # the size comes from the 3D model itself


def test_new_bathroom_products_reach_companies_created_before_them():
    Base.metadata.drop_all(bind=engine)
    init_db()
    try:
        with SessionLocal() as db:
            seed_tenant(db, today=date(2026, 11, 10))
            new = ("SN004", "SN005", "SN006", "SN007", "SN008")
            ids = list(db.scalars(select(Product.id).where(Product.sku.in_(new))))
            db.execute(delete(Batch).where(Batch.product_id.in_(ids)))
            db.execute(delete(Product).where(Product.id.in_(ids)))   # as if the company was made before they existed
            db.commit()
            assert ensure_accessories(db) == len(new)
            p = db.scalars(select(Product).where(Product.sku == "SN005")).one()
            assert p.kind == "set" and p.image == "/images/products/SN005.jpg" and float(p.price) == SANITARY["SN005"].price
            assert ensure_accessories(db) == 0                      # only once
    finally:
        Base.metadata.drop_all(bind=engine)
