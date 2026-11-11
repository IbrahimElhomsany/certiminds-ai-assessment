"""Nova Studio step 5: Ask your data (feature 11) and follow-up drafts (feature 3).

Claude is replaced by a fake client so the tests are free and repeatable; offline mode is tested too."""
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend import admin_api, catalog, crm_bridge, main
from backend.crm import ask as ask_data
from backend.db.database import Base, SessionLocal, engine, init_db
from backend.db.models import Activity, AuditLog, Lead, Quote
from backend.db.seed import DEMO_PASSWORD, seed_tenant

client = TestClient(main.app)


@pytest.fixture(scope="module", autouse=True)
def demo_db():
    Base.metadata.drop_all(bind=engine)
    init_db()
    with SessionLocal() as db:
        seed_tenant(db, today=date(2026, 11, 10))
    crm_bridge.refresh_catalog(force=True)
    yield
    Base.metadata.drop_all(bind=engine)
    catalog.replace_catalog(catalog.load_catalog(), catalog.load_sanitary())


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    admin_api.login_limiter._hits.clear()
    admin_api.ai_limiter._hits.clear()
    monkeypatch.setattr(admin_api, "ai_client", lambda: None)      # offline unless a test plugs in a fake


_tok = {}


def H(who):
    if who not in _tok:
        _tok[who] = client.post("/api/admin/login", json={"email": f"{who}@ceramicanova.demo",
                                                          "password": DEMO_PASSWORD}).json()["token"]
    return {"Authorization": f"Bearer {_tok[who]}"}


class Fake:
    def __init__(self, responses):
        self.responses, self.requests = list(responses), []
        self.messages = self

    def create(self, **kw):
        self.requests.append(kw)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def resp(blocks, stop="end_turn"):
    return SimpleNamespace(content=blocks, stop_reason=stop, usage=SimpleNamespace(input_tokens=900, output_tokens=120))


def text(t):
    return SimpleNamespace(type="text", text=t)


def tool(i, name, args):
    return SimpleNamespace(type="tool_use", id=i, name=name, input=args)


# ------------------------------------------------------------------ ask your data
def test_offline_mode_answers_simple_questions_from_real_data():
    r = client.post("/api/admin/ask", json={"question": "Why are we losing leads in the last 90 days?"}, headers=H("manager")).json()
    assert r["mode"] == "offline" and r["answer"] is None
    c = r["calls"][0]
    assert c["tool"] == "leads_breakdown" and c["input"] == {"group_by": "lost_reason", "days": 90}
    assert {row["group"] for row in c["output"]["rows"]} <= {"price", "stock", "competitor", "timing", "no_response", "not given"}
    unknown = client.post("/api/admin/ask", json={"question": "What's the weather?"}, headers=H("manager")).json()
    assert unknown["calls"] == [] and "Offline mode" in unknown["note"]


def test_claude_answer_uses_tools_and_numbers_are_checked(monkeypatch):
    with SessionLocal() as db:
        from backend.db.models import User
        mgr = db.scalar(select(User).where(User.email == "manager@ceramicanova.demo"))
        k = ask_data.t_kpis(db, mgr, 30)
    good = f"In the last 30 days revenue was QAR {k['revenue']:,.0f} from {k['orders']} orders."
    fake = Fake([resp([tool("t1", "get_kpis", {"days": 30})], "tool_use"), resp([text(good)])])
    monkeypatch.setattr(admin_api, "ai_client", lambda: fake)
    r = client.post("/api/admin/ask", json={"question": "How much did we sell this month?"}, headers=H("manager")).json()
    assert r["mode"] == "claude" and r["answer"] == good and r["calls"][0]["tool"] == "get_kpis"
    assert fake.requests[0]["tools"] == ask_data.TOOL_DEFINITIONS          # only the read-only tools are offered
    assert r["usage"]["input_tokens"] == 1800

    made_up = Fake([resp([tool("t1", "get_kpis", {"days": 30})], "tool_use"), resp([text("Revenue was QAR 987,654.")])])
    monkeypatch.setattr(admin_api, "ai_client", lambda: made_up)
    r = client.post("/api/admin/ask", json={"question": "Revenue this month?"}, headers=H("manager")).json()
    assert r["mode"] == "checked_fallback" and r["answer"] is None and "987654" in r["note"] and r["calls"]


def test_salesperson_only_gets_their_own_figures(monkeypatch):
    r_omar = client.post("/api/admin/ask", json={"question": "revenue last 90 days"}, headers=H("omar")).json()
    r_mgr = client.post("/api/admin/ask", json={"question": "revenue last 90 days"}, headers=H("manager")).json()
    assert r_omar["scope"] == "my leads"
    assert 0 < r_omar["calls"][0]["output"]["revenue"] < r_mgr["calls"][0]["output"]["revenue"]
    rows = client.post("/api/admin/ask", json={"question": "sales by salesperson"}, headers=H("omar")).json()
    assert [x["group"] for x in rows["calls"][0]["output"]["rows"]] == ["Omar Khalil"]
    assert client.post("/api/admin/ask", json={"question": "sales"}, headers=H("warehouse")).status_code == 403


def test_api_failure_falls_back_and_unknown_tools_are_refused(monkeypatch):
    monkeypatch.setattr(admin_api, "ai_client", lambda: Fake([TimeoutError("slow")]))
    r = client.post("/api/admin/ask", json={"question": "Which products will run out first?"}, headers=H("owner")).json()
    assert r["mode"] == "offline" and "not reachable" in r["note"] and r["calls"][0]["tool"] == "stock"
    with SessionLocal() as db:
        from backend.db.models import User
        owner = db.scalar(select(User).where(User.email == "owner@ceramicanova.demo"))
        assert "error" in ask_data.run_tool(db, owner, "delete_everything", {})
        assert db.scalar(select(AuditLog).where(AuditLog.action == "ai.ask"))


def test_ungrounded_check():
    out = [{"revenue": 569832.4, "orders": 43, "margin_pct": 37.9}]
    assert ask_data.ungrounded("QAR 569,832 from 43 orders, 37.9% margin (about QAR 570k).", out, "") == []
    assert ask_data.ungrounded("Revenue was QAR 600,000.", out, "") == [600000.0]


# ------------------------------------------------------------------ follow-up drafts
def waiting_for(who):
    items = client.get("/api/admin/followups", headers=H(who)).json()["items"]
    assert items, "the demo data has quotations waiting for a reply"
    return items[0]


def test_template_draft_and_mark_sent_logs_on_the_timeline():
    w = waiting_for("lina")
    d = client.post(f"/api/admin/followups/{w['quote_id']}/draft", json={"channel": "whatsapp"}, headers=H("lina")).json()
    assert d["mode"] == "template" and w["quote"] in d["text"] and "Lina Mansour" in d["text"]
    assert "discount" not in d["text"].lower()
    r = client.post(f"/api/admin/followups/{w['quote_id']}/sent", json={"channel": "whatsapp", "text": d["text"]}, headers=H("lina"))
    assert r.status_code == 200
    with SessionLocal() as db:
        assert db.scalar(select(Activity).where(Activity.lead_id == w["lead_id"], Activity.type == "follow_up"))
    left = [x["quote_id"] for x in client.get("/api/admin/followups", headers=H("lina")).json()["items"]]
    assert w["quote_id"] not in left                                    # followed up: no longer waiting


def test_ai_draft_with_a_discount_is_replaced_by_the_template(monkeypatch):
    w = waiting_for("yusuf")
    monkeypatch.setattr(admin_api, "ai_client", lambda: Fake([resp([text(
        f"Hi! Quotation {w['quote']} is ready. Reply today and get 10% discount!")])]))
    d = client.post(f"/api/admin/followups/{w['quote_id']}/draft", json={}, headers=H("yusuf")).json()
    assert d["mode"] == "template" and "discount" in d["note"]
    nice = f"Hello, this is Yusuf from Ceramica Nova. Did you get a chance to look at quotation {w['quote']}? " \
           "Happy to change quantities or book a showroom visit. Best regards, Yusuf Rahman"
    monkeypatch.setattr(admin_api, "ai_client", lambda: Fake([resp([text(nice)])]))
    d = client.post(f"/api/admin/followups/{w['quote_id']}/draft", json={"channel": "email"}, headers=H("yusuf")).json()
    assert d["mode"] == "claude" and d["text"] == nice


def test_followups_respect_roles_and_ownership():
    w = waiting_for("omar")
    assert client.post(f"/api/admin/followups/{w['quote_id']}/draft", json={}, headers=H("lina")).status_code == 404
    assert client.get("/api/admin/followups", headers=H("viewer")).status_code == 403
    with SessionLocal() as db:
        q = db.scalar(select(Quote).where(Quote.status == "accepted"))
        lead = db.get(Lead, q.lead_id)
        who = lead.owner.email.split("@")[0]
    assert client.post(f"/api/admin/followups/{q.id}/draft", json={}, headers=H(who)).status_code == 409


def test_daily_ai_budget_stops_claude_and_features_fall_back(monkeypatch):
    from backend import deploy
    monkeypatch.setattr(deploy, "_usage", {"day": None, "calls": 0})
    inner = Fake([resp([text("one")]), resp([text("two")])])
    capped = deploy.BudgetedClient(inner, daily_limit=1)
    assert capped.messages.create(model="m").content[0].text == "one"
    with pytest.raises(deploy.AIBudgetExceeded):
        capped.messages.create(model="m")
    monkeypatch.setattr(admin_api, "ai_client", lambda: capped)
    r = client.post("/api/admin/ask", json={"question": "Which products will run out first?"}, headers=H("owner")).json()
    assert r["mode"] == "offline" and "AIBudgetExceeded" in r["note"]
