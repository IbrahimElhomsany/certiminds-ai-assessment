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
