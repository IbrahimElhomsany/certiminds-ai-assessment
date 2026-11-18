"""D2: staff rate Ask-your-data answers and follow-up drafts, and the AI learns from it (backend/crm/learning.py)."""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from backend import admin_api, catalog, crm_bridge, main
from backend.crm import ask as ask_data
from backend.crm import learning
from backend.db.database import Base, SessionLocal, engine, init_db
from backend.db.models import AiFeedback
from backend.db.seed import DEMO_PASSWORD, seed_tenant
from tests.test_ai_admin import Fake, resp, text, tool

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
    monkeypatch.setattr(admin_api, "ai_client", lambda: None)


_tok = {}


def H(who):
    if who not in _tok:
        _tok[who] = client.post("/api/admin/login", json={"email": f"{who}@ceramicanova.demo",
                                                          "password": DEMO_PASSWORD}).json()["token"]
    return {"Authorization": f"Bearer {_tok[who]}"}


def ask(q, who="manager"):
    return client.post("/api/admin/ask", json={"question": q}, headers=H(who)).json()


def rate(fid, rating, who="manager", **kw):
    return client.post(f"/api/admin/ai/feedback/{fid}", json={"rating": rating, **kw}, headers=H(who))


def test_similar_questions_are_found_in_other_words():
    rows = [AiFeedback(question=q, rating=1) for q in
            ("Which products bring the most profit?", "Why are we losing leads?", "Which quotations are waiting?")]
    best = learning.similar("what products make us the most profit this month", rows)
    assert best[0][1].question.startswith("Which products") and best[0][0] > learning.SAME
    assert learning.similar("hello there", rows) == []


def test_a_liked_answer_teaches_offline_mode_which_figures_to_look_up():
    # keyword rules alone send "products ... profit" to top products (no idea of profit)
    assert ask_data.offline_pick("Which products bring the most profit?")[0] == "top_products"
    # but the demo team liked the profit-per-product breakdown for that question, so that is what is looked up
    first = ask("What products make us the most profit in the last 90 days?")
    assert first["calls"][0]["tool"] == "sales_breakdown"
    assert first["calls"][0]["input"] == {"group_by": "product", "days": 90}
    assert "your team marked helpful" in first["learned"]
    assert rate(first["feedback_id"], 1).status_code == 200
    # liked questions move to the front of the suggestions
    assert client.get("/api/admin/ask", headers=H("manager")).json()["suggestions"][0] == "Which products bring the most profit?"


def test_a_disliked_lookup_is_not_used_again_for_the_same_question():
    r = ask("Show me the stage funnel")
    assert r["calls"][0]["tool"] == "pipeline"
    bad = rate(r["feedback_id"], -1, reason="not_asked", comment="I wanted leads per stage")
    assert bad.status_code == 200 and bad.json()["reason"] == "not_asked"
    r2 = ask("Show me the funnel by stage")
    assert not r2["calls"] or r2["calls"][0]["tool"] != "pipeline"


def test_claude_is_told_what_staff_liked_and_disliked(monkeypatch):
    fake = Fake([resp([tool("t1", "get_kpis", {"days": 30})], "tool_use"), resp([text("Revenue is shown above.")])])
    monkeypatch.setattr(admin_api, "ai_client", lambda: fake)
    r = ask("Which products bring us the most profit this quarter?")
    system = fake.requests[0]["system"]
    assert "HELPFUL" in system and "sales_breakdown(group_by=product" in system
    assert r["learned"] and r["feedback_id"]


def test_only_your_own_answers_can_be_rated_and_reasons_are_checked():
    r = ask("Which quotations are waiting for a reply?")
    assert rate(r["feedback_id"], 1, who="owner").status_code == 404
    bad = rate(r["feedback_id"], -1, reason="because")
    assert bad.status_code == 422 and bad.json()["detail"]["field"] == "reason"
    assert client.post(f"/api/admin/ai/feedback/{r['feedback_id']}", json={"rating": 5}, headers=H("manager")).status_code == 422


def test_drafts_learn_from_what_staff_really_sent(monkeypatch):
    items = client.get("/api/admin/followups", headers=H("owner")).json()["items"]
    qid = items[0]["quote_id"]
    d = client.post(f"/api/admin/followups/{qid}/draft", json={"channel": "whatsapp"}, headers=H("owner")).json()
    assert d["mode"] == "template" and d["feedback_id"]
    mine = "Hi! Just checking you got our quotation. Happy to bring samples to your villa this week. Omar"
    assert client.post(f"/api/admin/followups/{qid}/sent", json={"channel": "whatsapp", "text": mine,
                                                                 "feedback_id": d["feedback_id"]}, headers=H("owner")).status_code == 200
    # the next AI draft gets that message as a style example
    fake = Fake([resp([text("Hello, a short friendly note about your quotation. Omar")])])
    monkeypatch.setattr(admin_api, "ai_client", lambda: fake)
    qid2 = items[1]["quote_id"]
    d2 = client.post(f"/api/admin/followups/{qid2}/draft", json={"channel": "whatsapp"}, headers=H("owner")).json()
    assert "Happy to bring samples to your villa" in fake.requests[0]["messages"][0]["content"]
    assert d2["mode"] == "claude" and "your team sent before" in d2["learned"]
    # a draft sent unchanged counts as liked
    assert client.post(f"/api/admin/followups/{qid2}/sent", json={"channel": "whatsapp", "text": d2["text"],
                                                                  "feedback_id": d2["feedback_id"]}, headers=H("owner")).status_code == 200
    with SessionLocal() as db:
        assert db.get(AiFeedback, d2["feedback_id"]).rating == 1


def test_learning_summary_for_the_team():
    s = client.get("/api/admin/ai/learning", headers=H("manager")).json()
    assert s["ask"]["helpful"] >= 1 and s["ask"]["not_helpful"] >= 1
    assert s["draft"]["sent"] >= 2 and s["draft"]["sent_as_is"] >= 1
    assert "Which products bring the most profit?" in s["favourite_questions"]
    assert any(x["reason"] == "Didn't answer my question" for x in s["reasons"])
    assert s["recent_not_helpful"][0]["comment"] == "I wanted leads per stage"
