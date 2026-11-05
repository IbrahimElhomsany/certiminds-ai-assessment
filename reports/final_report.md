# Final technical report

## 1. Problem and approach
Build an AI sales consultant for Ceramica Nova that is helpful and persuasive **without** inventing prices, stock, discounts or product facts. My core design principle is a strict split of responsibilities:

- **LLM (Claude):** understand the customer (intent, requirements, language), decide which tool to call, and write a natural reply.
- **Deterministic code:** everything that must be exactly right: product filtering against business rules, quantities, stock status, quote totals, discount approval, authorization.
- **RAG:** descriptive knowledge and rules, with citations; volatile data (stock) deliberately excluded.
- **Fine-tuned small model:** a cheap local model adapted to the sales behaviour and JSON format, evaluated against its base model.

## 2. System components
1. **Deterministic tools** (`backend/tools.py`): calculator (Decimal arithmetic, full-box pricing), inventory (AVAILABLE / PARTIALLY_AVAILABLE / OUT_OF_STOCK), quotation (catalog prices, single currency, validation, >12% gives `approval_required`).
2. **LLM gateway** (`backend/gateway.py`): provider abstraction, timeout, retry with exponential backoff and jitter, error classification, fallback to an offline provider, Pydantic validation of structured output, cache, token/cost/latency metrics.
3. **RAG** (`rag/pipeline.py`): one chunk per product/rule/section, hybrid BM25 + dense retrieval with Reciprocal Rank Fusion, metadata filters, citations, injection quarantine at ingestion, escaped `<document>` context.
4. **Recommender** (`backend/recommender.py`): hard constraints, then soft scoring, then top 3 with reasons.
5. **Sales agent** (`backend/agent.py`): Claude tool-use loop over 7 tools; multi-turn memory of requirements; deterministic workflow mode for offline use and as a fallback.
6. **Guardrails** (`backend/guardrails.py`): input attack classification; output checks for ungrounded money amounts (QAR or $), canary leakage and fake approval claims; ownership checks for sessions and quotes.
7. **UI** (`frontend/index.html`): the Ceramica Nova AI page built from the reviewers' mockup: chat, tile visualiser, product and bathroom-set cards, project panel with budget bar, cart, PDF quotation (`backend/quote_pdf.py`), bill of materials and technical data. A technical view shows the understood requirements, guardrail flags and raw tool calls. Features that would need other systems (room upload, 3D/AR, experts, bookings, samples, brochures) are left out, so every button works.
8. **Catalog changes after the first review:** prices shown in QAR (converted from the official USD catalog at 3.64), three bathroom sets sold per unit with demo prices, a wet-area rule (bathroom and shower floors need R11+), and proposed rewards and delivery rules (`backend/offers.py`).

## 3. Evaluation
A 38-case suite (36 original plus 2 bathroom-set cases) covers correct and incorrect recommendations, missing information, hallucination, inventory, calculations, prompt injection, unavailable products, irrelevant questions, objections and Arabic. It scores nine metrics (requirement extraction, product correctness, rule compliance, groundedness, calculation correctness, inventory correctness, hallucination, security, usefulness) plus latency. Results for offline and Claude modes: `RESULTS.md`.

## 4. Model adaptation
Qwen2.5-0.5B-Instruct (4-bit) + LoRA with MLX on a MacBook Air M4, trained on 238 synthetic examples built from the catalog and rules; compared with the base model on 45 held-out examples with identical decoding. Method, hyperparameters, results and limitations: `training/README.md`, `reports/model_comparison.md`.

## 5. Security
Eleven threats are analysed in `security/threat_model.md`. The central point: the prompt is only one layer. Prices and approvals can't be changed because no tool accepts them, malicious documents are quarantined, and replies with unverified amounts are blocked.

## 6. Limitations (honest)
- Simulated authentication (`user_id` field) and in-memory storage.
- Regex-based input scanning and PII detection are incomplete.
- The output guard verifies numbers, not every factual claim.
- The offline workflow's evaluation score is optimistic because it was built alongside the published scenarios.
- Synthetic LoRA data; small test sets; automatic metrics don't capture tone or persuasiveness. The adapter was trained before the QAR change and the bathroom sets were added.
- Rewards, delivery and the cart are front-end only, not connected systems. Mockup features that would need other systems (room upload, 3D / AR, booking, sharing) are left out.

## 7. Production roadmap
1. Real authentication (OIDC/JWT) and PostgreSQL with row-level security; Redis for sessions, cache and rate limits.
2. Per-task model routing (small model for extraction, large for hard reasoning) and a circuit breaker across providers.
3. Serve the LoRA model locally (`mlx_lm.server` or vLLM) as a cheap first-pass extractor and outage fallback.
4. pgvector/OpenSearch with tenant filters; a cross-encoder reranker; automated RAG evaluation (retrieval recall@k).
5. OpenTelemetry tracing, Grafana dashboards and alerts as described in `reports/design_notes.md`.
6. Evaluation as a CI gate on every prompt/model change; human review of sampled conversations.
7. Human-in-the-loop approval workflow for discounts above 12% (manager UI).
