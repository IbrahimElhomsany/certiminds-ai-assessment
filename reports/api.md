# API documentation

Interactive OpenAPI docs are generated automatically: run the server and open **http://127.0.0.1:8000/docs**.

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Nova AI web page |
| GET | `/api/v1/catalog` | Public product list for the page: tiles and bathroom sets, QAR prices, photo paths (no stock levels) |
| GET | `/images/{file}` | Product and room photos |
| GET | `/health` | Status, configured providers, agent mode |
| POST | `/api/v1/generate` | B1 gateway: structured analysis of one message |
| POST | `/api/v1/chat` | Capstone sales consultant (multi-turn) |
| GET | `/api/v1/quotes/{quotation_id}` | Read your own quotation (header `X-User-Id`) |
| GET | `/api/v1/quotes/{quotation_id}/pdf` | Download your own quotation as a PDF (same ownership check) |
| POST | `/api/v1/quotes` | "PDF quotation" button: create a quote directly from the items on screen (no AI, no discount) |
| GET | `/api/v1/metrics` | B5 metrics snapshot |

## POST /api/v1/generate
Request:
```json
{"user_id": "USR-1001", "task": "recommendation", "message": "I need tiles for my outdoor balcony", "language": "en"}
```
- `user_id`: `USR-` + 3-10 digits; `task`: recommendation | extraction | objection | faq; `message`: 1-2000 chars; `language`: en | ar.

Response 200:
```json
{
  "request_id": "a1b2c3d4e5f6",
  "result": {
    "intent": "recommendation_request",
    "customer_requirements": {"application": "balcony", "indoor_outdoor": "outdoor", "floor_or_wall": "floor",
                              "area_m2": null, "budget": null, "preferred_style": null, "preferred_color": null,
                              "slip_requirement": "high", "traffic_level": null, "urgency": null},
    "missing_information": ["area_m2", "budget"],
    "recommended_actions": ["ask_clarifying_questions"],
    "confidence": 0.8
  },
  "meta": {"model": "claude-opus-5-5", "provider": "anthropic", "cached": false, "fallback_used": false,
           "input_tokens": 950, "output_tokens": 180, "estimated_cost_usd": 0.0074, "latency_ms": 2100.5,
           "attempts": [{"provider": "anthropic", "attempt": 1, "error": null, "ms": 2099.1}]}
}
```
(Values are illustrative; real values depend on the model.)

Errors: `422` invalid input, `429` rate limit exceeded, `503` `{"error": "llm_unavailable", "kind": "TIMEOUT"}` when every provider failed, `500` `{"error": "internal_error"}` (no internals leaked).

## POST /api/v1/chat
Request:
```json
{"user_id": "USR-1001", "message": "25 sqm outdoor balcony, kids, budget $1000", "session_id": null}
```
Send back the returned `session_id` to continue the conversation. A session belongs to the user who created it (another user gets `403`).

Response fields: `session_id`, `reply`, `mode` (`agent` = Claude tool use, `workflow` = deterministic, `workflow_fallback` = agent failed or was blocked by the output guard), `language`, `analysis`, `requirements` (accumulated across turns), `recommendations` (with reasons, stock status, boxes, estimated subtotal), `quote`, `tool_calls` (name, input, output, error flag, ms), `citations`, `guardrails` (`input_flags`, `output_violations`, `quarantined_documents`), `latency_ms`.

## GET /api/v1/quotes/{quotation_id}
Header `X-User-Id: USR-1001`. `200` with the quote if it belongs to that user, `403` if it belongs to someone else, `404` if unknown.
In production the user comes from a verified JWT, not a header.

## POST /api/v1/quotes
Header `X-User-Id: USR-1001`. Body:
```json
{"items": [{"sku": "CN012", "area_m2": 24}, {"sku": "SN001", "quantity": 1}]}
```
Tiles need `area_m2`, bathroom sets need `quantity` (whole units). Returns `{"quote": {...}, "offers": {...}}`; `422` for invalid items (for example a tile without an area, or an empty list). Every number comes from `tools.generate_quote`.

## GET /api/v1/quotes/{quotation_id}/pdf
Same header and ownership check as above. Returns `application/pdf` as an attachment named `Quotation-<id>.pdf`.

## Example curl
```bash
curl -s -X POST localhost:8000/api/v1/chat -H 'Content-Type: application/json' \
  -d '{"user_id":"USR-1001","message":"I want Arctic Pearl for my swimming pool deck."}' | python -m json.tool
```
