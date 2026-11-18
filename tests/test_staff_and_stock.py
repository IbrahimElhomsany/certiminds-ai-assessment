"""Owner manages staff accounts; warehouse/owner add stock and correct quantities (backend/admin_api.py)."""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from backend import admin_api, main
from backend.db.database import Base, SessionLocal, engine, init_db
from backend.db.seed import DEMO_PASSWORD, seed_tenant

client = TestClient(main.app)


@pytest.fixture(scope="module", autouse=True)
def demo_db():
    Base.metadata.drop_all(bind=engine)
    init_db()
    with SessionLocal() as db:
        seed_tenant(db, today=date(2026, 11, 10))
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def no_limits():
    admin_api.login_limiter._hits.clear()


def login(email, password=DEMO_PASSWORD):
    return client.post("/api/admin/login", json={"company": "ceramica-nova", "email": email, "password": password})


def token(email, password=DEMO_PASSWORD):
    return {"Authorization": f"Bearer {login(email, password).json()['token']}"}


def test_owner_adds_edits_switches_off_and_resets_a_staff_member():
    owner = token("owner@ceramicanova.demo")
    new = {"name": "Nour Saleh", "email": "Nour@CeramicaNova.demo", "role": "salesperson", "password": "Tiles2026"}
    r = client.post("/api/admin/users", json=new, headers=owner)
    assert r.status_code == 200, r.text
    u = r.json()
    assert u["email"] == "nour@ceramicanova.demo" and u["discount_limit_pct"] == 5 and u["active"]
    # the same email again, a weak password, a bad email: each error names its box
    again = client.post("/api/admin/users", json=new, headers=owner)
    assert again.status_code == 409 and again.json()["detail"]["field"] == "email"
    weak = client.post("/api/admin/users", json=new | {"email": "x@ceramicanova.demo", "password": "short"}, headers=owner)
    assert weak.status_code == 422 and weak.json()["detail"]["field"] == "password"
    bad = client.post("/api/admin/users", json=new | {"email": "not-an-email", "password": "Tiles2026"}, headers=owner)
    assert bad.json()["detail"]["field"] == "email"
    # the new person can log in
    nour = token("nour@ceramicanova.demo", "Tiles2026")
    assert client.get("/api/admin/me", headers=nour).json()["user"]["role"] == "salesperson"
    # promoted: the role's usual limit, and the old login stops working (role is in the token)
    r = client.patch(f"/api/admin/users/{u['id']}", json={"role": "sales_manager"}, headers=owner).json()
    assert r["role"] == "sales_manager" and r["discount_limit_pct"] == 10
    assert client.get("/api/admin/me", headers=nour).status_code == 401
    # new password
    assert client.post(f"/api/admin/users/{u['id']}/password", json={"password": "Marble2027"}, headers=owner).status_code == 200
    assert login("nour@ceramicanova.demo", "Tiles2026").status_code == 401
    assert login("nour@ceramicanova.demo", "Marble2027").status_code == 200
    # switched off: can't log in any more; switched on again: can
    assert client.patch(f"/api/admin/users/{u['id']}", json={"active": False}, headers=owner).json()["active"] is False
    assert login("nour@ceramicanova.demo", "Marble2027").status_code == 401
    client.patch(f"/api/admin/users/{u['id']}", json={"active": True}, headers=owner)
    assert login("nour@ceramicanova.demo", "Marble2027").status_code == 200
    actions = [a["action"] for a in client.get("/api/admin/audit?limit=50", headers=owner).json()]
    assert {"user.create", "user.update", "user.password_reset", "user.deactivate", "user.reactivate"} <= set(actions)
    assert "Marble2027" not in str(client.get("/api/admin/audit?limit=50", headers=owner).json())


def test_owner_cannot_lock_themselves_out_and_others_cannot_manage_staff():
    owner = token("owner@ceramicanova.demo")
    me = client.get("/api/admin/me", headers=owner).json()["user"]["id"]
    assert client.patch(f"/api/admin/users/{me}", json={"active": False}, headers=owner).status_code == 409
    assert client.patch(f"/api/admin/users/{me}", json={"role": "viewer"}, headers=owner).status_code == 409
    manager = token("manager@ceramicanova.demo")
    assert client.post("/api/admin/users", json={"name": "X Y", "email": "xy@ceramicanova.demo", "role": "owner",
                                                 "password": "Tiles2026"}, headers=manager).status_code == 403
    assert client.patch(f"/api/admin/users/{me}", json={"active": False}, headers=manager).status_code == 403


def test_stock_is_received_corrected_and_reorder_point_set():
    wh = token("warehouse@ceramicanova.demo")
    before = client.get("/api/admin/inventory/CN001", headers=wh).json()
    b = client.post("/api/admin/inventory/CN001/receive", json={"qty": 50, "shade": "W2"}, headers=wh).json()
    p = client.get("/api/admin/inventory/CN001", headers=wh).json()
    assert p["level"]["stock"] == pytest.approx(before["level"]["stock"] + 50)
    r = client.post("/api/admin/inventory/CN001/adjust", json={"batch": b["code"], "counted": 47.5, "reason": "damaged",
                                                               "note": "2 boxes broken"}, headers=wh)
    assert r.status_code == 200 and r.json()["left"] == 47.5
    p = client.get("/api/admin/inventory/CN001", headers=wh).json()
    assert p["level"]["stock"] == pytest.approx(before["level"]["stock"] + 47.5)
    assert p["movements"][0]["qty"] == -2.5 and p["movements"][0]["reason"] == "adjust"
    same = client.post("/api/admin/inventory/CN001/adjust", json={"batch": b["code"], "counted": 47.5, "reason": "count"}, headers=wh)
    assert same.status_code == 422
    assert client.post("/api/admin/inventory/CN001/adjust", json={"batch": "NOPE", "counted": 1, "reason": "count"}, headers=wh).status_code == 404
    assert client.post("/api/admin/inventory/CN001/reorder", json={"reorder_point": 120}, headers=wh).json()["reorder_point"] == 120
    sales = token("lina@ceramicanova.demo")
    assert client.post("/api/admin/inventory/CN001/adjust", json={"batch": b["code"], "counted": 1, "reason": "count"}, headers=sales).status_code == 403
    owner = token("owner@ceramicanova.demo")
    assert "stock.adjust" in [a["action"] for a in client.get("/api/admin/audit?limit=20", headers=owner).json()]
