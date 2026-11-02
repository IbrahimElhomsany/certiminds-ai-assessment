"""FastAPI application. Run with:  uvicorn backend.main:app --reload
Interactive API docs (API documentation deliverable): http://127.0.0.1:8000/docs
"""
import os
import time

from dotenv import load_dotenv
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse

load_dotenv()   # reads .env into environment variables (the API key never appears in code)

from backend.cache import TTLCache                                  # noqa: E402
from backend.gateway import GatewayError, LLMGateway                 # noqa: E402
from backend.observability import METRICS, log_event                 # noqa: E402
from backend.prompts import ANALYSIS_SYSTEM, PROMPT_VERSION          # noqa: E402
from backend.providers import AnthropicProvider, FlakyProvider, MockProvider  # noqa: E402
from backend.ratelimit import RateLimiter                            # noqa: E402
from backend.schemas import ChatRequest, GenerateRequest, StructuredAnalysis  # noqa: E402
from backend.agent import SalesAgent                                 # noqa: E402
from backend.store import AccessDenied, get_quote                    # noqa: E402


def use_claude() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY")) and os.getenv("LLM_PROVIDER", "anthropic") == "anthropic"


def build_providers():
    """Primary = Claude (if a key is configured). Last fallback = offline rule-based mock,
    so the app degrades gracefully instead of failing when Claude is unavailable."""
    providers = []
    if use_claude():
        providers.append(AnthropicProvider())
    providers.append(MockProvider())
    if os.getenv("SIMULATE_FLAKY") == "1":            # B3 demo: inject random failures into the primary
        providers[0] = FlakyProvider(providers[0])
    return providers


app = FastAPI(title="Ceramica Nova AI Sales API", version="1.0")
gateway = LLMGateway(build_providers(), timeout_s=float(os.getenv("LLM_TIMEOUT_S", "30")),
                     cache=TTLCache(ttl_seconds=3600), prompt_version=PROMPT_VERSION)
agent_client = None
if use_claude():
    import anthropic
    agent_client = anthropic.Anthropic(max_retries=2, timeout=60)
agent = SalesAgent(gateway, client=agent_client)
rate_limiter = RateLimiter(max_requests=int(os.getenv("RATE_LIMIT_PER_MIN", "20")), window_seconds=60)


@app.exception_handler(Exception)
async def unhandled(request, exc):
    # Never leak stack traces, keys or internals to the client.
    log_event("unhandled_error", path=str(request.url.path), error=type(exc).__name__)
    return JSONResponse(status_code=500, content={"error": "internal_error"})


@app.get("/health")
def health():
    return {"status": "ok", "providers": [p.name for p in gateway.providers],
            "agent_mode": "claude tool-use" if agent_client else "workflow (no LLM)"}


@app.get("/")
def ui():
    return FileResponse(Path(__file__).resolve().parent.parent / "frontend" / "index.html")


@app.post("/api/v1/chat")
def chat(req: ChatRequest):
    """Capstone endpoint: the AI sales consultant. In production user_id comes from an
    authentication token (JWT), not from the request body."""
    if not rate_limiter.allow(req.user_id):
        raise HTTPException(status_code=429, detail="Too many requests, please slow down.")
    try:
        return agent.chat(req.user_id, req.message, req.session_id)
    except AccessDenied:
        raise HTTPException(status_code=403, detail="This session belongs to another user.")


@app.get("/api/v1/quotes/{quotation_id}")
def read_quote(quotation_id: str, x_user_id: str = Header(...)):
    """Authorization boundary: a customer can only read their own quotes."""
    try:
        q = get_quote(quotation_id, x_user_id)
    except AccessDenied:
        raise HTTPException(status_code=403, detail="Not your quotation.")
    if q is None:
        raise HTTPException(status_code=404, detail="Quotation not found.")
    return q


@app.post("/api/v1/generate")
def generate(req: GenerateRequest):
    start = time.perf_counter()
    METRICS.incr("requests")
    if not rate_limiter.allow(req.user_id):
        METRICS.error("RATE_LIMITED_CLIENT")
        raise HTTPException(status_code=429, detail="Too many requests, please slow down.")
    try:
        result = gateway.generate_structured(system=ANALYSIS_SYSTEM, user=req.message,
                                             output_model=StructuredAnalysis, task=req.task,
                                             language=req.language)
    except GatewayError as e:
        raise HTTPException(status_code=503, detail={"error": "llm_unavailable", "kind": e.kind.value})
    finally:
        METRICS.record_latency("total", (time.perf_counter() - start) * 1000)
    return {
        "request_id": result.request_id,
        "result": result.output.model_dump(),
        "meta": {"model": result.model, "provider": result.provider, "cached": result.cached,
                 "fallback_used": result.fallback_used, "input_tokens": result.input_tokens,
                 "output_tokens": result.output_tokens, "estimated_cost_usd": result.estimated_cost_usd,
                 "latency_ms": result.latency_ms, "attempts": result.attempts},
    }


@app.get("/api/v1/metrics")
def metrics():
    return METRICS.snapshot()
