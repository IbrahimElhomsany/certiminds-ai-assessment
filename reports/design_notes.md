# Design notes

Short explanations of the main engineering decisions, with where to find each one in the code.

## B1. LLM gateway (`backend/gateway.py`, `backend/providers.py`, `backend/main.py`)

| Requirement | How |
|---|---|
| Request validation | Pydantic `GenerateRequest`: user_id format, allowed task values, message 1-2000 chars, language en/ar. Invalid gives HTTP 422 |
| Structured outputs | Claude `messages.parse(output_format=StructuredAnalysis)`, then **re-validated** by the gateway |
| Model abstraction | Every provider implements `generate(system, user, output_model, timeout_s)`: `AnthropicProvider`, `MockProvider`, `FlakyProvider` |
| Timeout | Per-call timeout (`LLM_TIMEOUT_S`, default 30s) passed to the SDK |
| Retry + exponential backoff | Gateway retries temporary errors 3 times: 0.5s, 1s, 2s (+10% jitter, capped at 8s). SDK retries are disabled so the gateway's behaviour can be tested |
| Error classification | `ErrorKind`: TIMEOUT, RATE_LIMIT, SERVER_ERROR, CONNECTION, INVALID_RESPONSE (retryable); AUTH, BAD_REQUEST (not retryable) |
| Fallback | Providers are tried in order. Claude first, then the offline rule-based provider, so the app degrades instead of failing |
| Rate limiting | Sliding window per user, 20 requests/min, returns HTTP 429 |
| Logging | One JSON line per event (request id, provider, tokens, cost, latency, error class). No API keys, no full customer messages |
| Token tracking + cost | Usage tokens from each response multiplied by the price table (`PRICING`) |
| Secrets | `ANTHROPIC_API_KEY` read from the environment (`.env`, git-ignored); generic error handler hides internals |

## B3. Reliability challenge
`FlakyProvider` randomly injects timeouts (15%), HTTP 429 (15%), HTTP 500 (10%) and truncated JSON (15%). `tests/test_gateway.py` runs 200 requests through it with a fixed seed and asserts that every result is either valid, schema-checked output or a classified `GatewayError`, never broken data. Live demo: start the server with `SIMULATE_FLAKY=1` and watch the `llm_error` / retry / fallback log lines and `/api/v1/metrics`.

## B4. Caching and cost (`backend/cache.py`)

| Question | Decision |
|---|---|
| What is cached | The structured analysis of a message (intent + extracted requirements). Same text, task, language, model and prompt version give the same answer |
| Never cached | Prices, stock, quantities, quotes, anything per-customer, and any message that looks like it contains personal data (email/phone). Stock changes, and quotes belong to one person |
| Cache key | `sha256(model | prompt_version | task | language | normalised message)`. Normalised = lowercase, collapsed spaces. The prompt version is in the key, so editing a prompt automatically invalidates old entries |
| Invalidation | TTL of 1 hour; `clear()` on catalog or prompt change; max-size eviction |
| Privacy | No user_id in the key, so generic questions are shared between users. That's only safe because PII-looking messages are skipped. In a multi-tenant production system the key must also include `tenant_id` |
| Expected effect | A cache hit takes about 1 ms instead of 1-3 s for an LLM call and costs $0. FAQ-style traffic often repeats, so even a 20-30% hit rate cuts LLM spend by the same fraction. Provider-side prompt caching of the long, fixed system prompt saves more |

## B5. Observability (`backend/observability.py`, `GET /api/v1/metrics`)
Recorded: request count, errors by class, LLM latency, retrieval latency, overall latency (avg/p50/p95), selected model counts, input/output tokens, estimated cost, cache hits/misses, retries, fallbacks.

**Production dashboards**
1. *Traffic and health:* requests/min, error rate by class, p50/p95/p99 latency (overall, LLM, retrieval).
2. *LLM usage and cost:* tokens and $ per model, per tenant, per feature; cost per conversation; cache hit rate.
3. *Reliability:* retries, fallbacks, circuit-breaker state, provider error rates.
4. *Quality and safety:* guardrail flags per day, output-guard blocks, refusal rate, eval scores from sampled conversations, thumbs-up/down.
5. *Business:* recommendations shown, quotes generated, quotes pending approval, conversion.

**Alerts:** error rate above 5% for 5 min; p95 latency above 8 s; fallback rate above 10% (provider problem); daily cost above budget or 2x the 7-day average; spike in injection flags; any output-guard block for ungrounded prices (possible model regression); eval score drop after a deploy.

## C1. RAG choices (`rag/pipeline.py`)

| Choice | Decision and reason |
|---|---|
| Documents | Catalog product cards, the 10 business rules, the quotation/ethics policy, a slip-rating guide, supplier notes |
| Chunking | One chunk per product, per rule and per guide section. The corpus is small and each unit is self-contained; splitting a product card would separate its name from its price or slip rating |
| Metadata | `type` (product/rule/policy/guide/note), `sku`, `source`, `trust` (catalog/internal/external). Used for filtering (e.g. only products) and shown with citations |
| Embedding model | `BAAI/bge-small-en-v1.5` via fastembed: 384 dimensions, ~130 MB, runs on CPU, no PyTorch, strong retrieval for its size. Arabic queries work because the LLM first extracts requirements in English and those are added to the query |
| Hybrid retrieval | BM25 catches exact terms ("R12", "CN004"); dense vectors catch meaning ("not slippery" is close to "slip resistant"). Merged with Reciprocal Rank Fusion, which needs no score tuning. Falls back to BM25 if the embedding model can't load |
| Vector store | In-memory numpy matrix: about 30 chunks, so a database would be over-engineering. Production: pgvector/OpenSearch with tenant filter |
| Top-k | 5 for grounding context, 4 for the `search_knowledge` tool: enough to include the relevant products and the matching rule without flooding the prompt |
| Reranking | Retrieval finds candidate knowledge, but **product ranking is done by the deterministic recommender** (hard rules, then soft scores), which acts as a domain reranker. A cross-encoder reranker is a future improvement |
| Citations | Every chunk has an id (`[CN004]`, `[business_rules#1]`); the agent is told to cite SKUs and the API returns the citation list |
| Stock not indexed | Stock changes constantly, so it is only available through `check_inventory` (E.13) |

## C2 + E.9/E.10. Tools (`backend/tools.py`, `backend/agent.py`)
Claude receives tool definitions (`recommend_products`, `check_inventory`, `calculate_tile_quantity`, `check_product_suitability`, `generate_quote`, `get_my_quote`, `search_knowledge`) and decides when to call them. Our code executes them and returns the real result. The model never sees a way to produce a tool result itself, and the output guard verifies that every amount in the final reply came from a tool. Tool errors are returned to the model as `is_error` results, not raised.

## E.7. Recommendation engine (`backend/recommender.py`)
Hard constraints remove products: surface (floor/wall), location (indoor/outdoor), slip at least R11 for outdoor or high-slip needs (BR-01), wall-only never on floors (BR-02), porcelain and at least R10 for high-traffic commercial floors (BR-03). Soft preferences give a score: commercial suitability, style match (with synonyms), colour, budget fit (deterministic estimate), price preference, stock (penalised harder when urgent). Top 3 are returned (BR-09) with reasons. It's deterministic, so it's explainable and testable.

## E.8. Pricing basis
Material subtotal = **total purchasable area (full boxes) x catalog price per m2**, because the customer receives and pays for whole boxes. Example: 40 m2 at 60x60 with 10% wastage gives 44 m2, 122.2 tiles rounded up to 123, 31 boxes (124 tiles, 44.64 m2), x $31 = $1,383.84.

## E.14. Sales conversion signals
| Signal | What the system does |
|---|---|
| Price sensitivity ("cheap", objections) | Ranks cheaper suitable products higher; objection reply lists cheaper alternatives and offers up to 12% |
| Urgency | Stock shortfalls get a bigger penalty, so available products rank first |
| Design uncertainty / missing info | Asks targeted questions (BR-10) instead of guessing |
| Durability concern | Hard rules keep only technically suitable products (slip, porcelain for traffic) |
| Maintenance concern | Prompt guidance to prefer matt/textured or easy-care options only when the catalog supports it (no invented claims) |
| Purchase readiness ("quote") | Generates a quotation via the tool |

## A4 in practice
The gateway takes an ordered list of providers, and `LLM_MODEL` selects the Claude model. Per-task routing (a smaller model for extraction, a larger one for difficult reasoning) is a configuration change in `build_providers()`.
