"""B1: the HTTP endpoint - validation, rate limiting, structured response, no secret leakage."""
from fastapi.testclient import TestClient

from backend import main

client = TestClient(main.app)
GOOD = {"user_id": "USR-1001", "task": "recommendation",
        "message": "I need tiles for my outdoor balcony", "language": "en"}


def test_generate_returns_structured_output():
    r = client.post("/api/v1/generate", json=GOOD)
    assert r.status_code == 200
    body = r.json()
    assert body["result"]["customer_requirements"]["indoor_outdoor"] == "outdoor"
    assert "area_m2" in body["result"]["missing_information"]
    assert {"model", "input_tokens", "estimated_cost_usd", "latency_ms"} <= body["meta"].keys()


def test_request_validation_rejects_bad_input():
    assert client.post("/api/v1/generate", json={**GOOD, "user_id": "hacker"}).status_code == 422
    assert client.post("/api/v1/generate", json={**GOOD, "task": "delete_db"}).status_code == 422
    assert client.post("/api/v1/generate", json={**GOOD, "message": ""}).status_code == 422
    assert client.post("/api/v1/generate", json={**GOOD, "message": "x" * 2001}).status_code == 422


def test_rate_limit_returns_429():
    main.rate_limiter._hits.clear()
    codes = [client.post("/api/v1/generate", json={**GOOD, "user_id": "USR-9999"}).status_code
             for _ in range(main.rate_limiter.max_requests + 1)]
    assert codes[-1] == 429 and codes[0] == 200


def test_metrics_endpoint_reports_usage():
    m = client.get("/api/v1/metrics").json()
    assert m["request_count"] >= 1 and "latency_ms" in m and "estimated_cost_usd" in m


def test_no_secrets_in_responses(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SECRET-TEST")
    for path in ["/health", "/api/v1/metrics"]:
        assert "sk-ant" not in client.get(path).text


def test_quote_pdf_download_only_for_the_owner():
    from backend.store import save_quote
    from backend.tools import QuoteItemRequest, generate_quote
    q = generate_quote("USR-1001", [QuoteItemRequest(sku="CN002", area_m2=25)], discount_percent=20)
    save_quote(q)
    url = f"/api/v1/quotes/{q.quotation_id}/pdf"
    r = client.get(url, headers={"X-User-Id": "USR-1001"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf" and r.content.startswith(b"%PDF")
    assert "attachment" in r.headers["content-disposition"]
    assert client.get(url, headers={"X-User-Id": "USR-2002"}).status_code == 403
    assert client.get("/api/v1/quotes/Q-NOPE/pdf", headers={"X-User-Id": "USR-1001"}).status_code == 404


def test_pdf_button_creates_quote_instantly_without_chat():
    body = {"items": [{"sku": "CN012", "area_m2": 24}, {"sku": "SN001", "quantity": 1}]}
    r = client.post("/api/v1/quotes", json=body, headers={"X-User-Id": "USR-3003"})
    assert r.status_code == 200
    q = r.json()["quote"]
    assert q["customer_id"] == "USR-3003" and q["discount_percent"] == 0 and len(q["products"]) == 2
    assert r.json()["offers"]["proposed"] is True
    pdf = client.get(f"/api/v1/quotes/{q['quotation_id']}/pdf", headers={"X-User-Id": "USR-3003"})
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    # bad input is rejected, not guessed: a tile without an area, an empty list
    assert client.post("/api/v1/quotes", json={"items": [{"sku": "CN012"}]}, headers={"X-User-Id": "USR-3003"}).status_code == 422
    assert client.post("/api/v1/quotes", json={"items": []}, headers={"X-User-Id": "USR-3003"}).status_code == 422


def test_catalog_endpoint_lists_tiles_and_bathroom_sets_in_qar():
    body = client.get("/api/v1/catalog").json()
    kinds = {p["kind"] for p in body["products"]}
    assert body["currency"] == "QAR" and kinds == {"tile", "set"} and len(body["products"]) == 18
    assert "stock_m2" not in str(body)                       # stock is only given out by the tools


def test_offers_are_calculated_by_code():
    from backend.offers import project_offers
    o = project_offers(7650)
    assert o["reward_points"] == 382 and o["reward_credit_qar"] == 38.2 and o["free_delivery"] and o["proposed"]
    assert project_offers(499.99)["delivery_fee_qar"] == 50.0
