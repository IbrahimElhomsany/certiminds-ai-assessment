"""FastAPI application. Run with:  uvicorn backend.main:app --reload
Interactive API docs (API documentation deliverable): http://127.0.0.1:8000/docs
"""
import os
import time

from dotenv import load_dotenv
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

load_dotenv()   # reads .env into environment variables (the API key never appears in code)

from backend.cache import TTLCache                                  # noqa: E402
from backend.gateway import GatewayError, LLMGateway                 # noqa: E402
from backend.observability import METRICS, log_event                 # noqa: E402
from backend.prompts import ANALYSIS_SYSTEM, PROMPT_VERSION          # noqa: E402
from backend.providers import AnthropicProvider, FlakyProvider, MockProvider  # noqa: E402
from backend.ratelimit import RateLimiter                            # noqa: E402
from backend.schemas import ChatRequest, GenerateRequest, StructuredAnalysis  # noqa: E402
from backend.agent import SalesAgent                                 # noqa: E402
from backend.store import AccessDenied, get_quote, save_quote        # noqa: E402
from backend.tools import QuoteItemRequest, generate_quote           # noqa: E402
from backend.offers import project_offers                            # noqa: E402
from pydantic import BaseModel, Field                                # noqa: E402
from backend.quote_pdf import build_quote_pdf                        # noqa: E402
from backend.catalog import CATALOG, SANITARY                         # noqa: E402


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


ROOT = Path(__file__).resolve().parent.parent
app.mount("/images", StaticFiles(directory=ROOT / "images", check_dir=False), name="images")


@app.get("/api/v1/catalog")
def catalog():
    """Public product list for the showroom page: names, sizes, prices (QAR) and photos.
    Stock levels are not included; they are checked by the tools when a customer asks."""
    def photo(name: str):
        return f"/images/{name}.jpg" if (ROOT / "images" / f"{name}.jpg").exists() else None
    tiles = [{"sku": p.sku, "name": p.name, "kind": "tile", "material": p.material, "finish": p.finish,
              "size_cm": f"{p.tile_width_cm}x{p.tile_length_cm}", "surfaces": p.surfaces, "locations": p.locations,
              "slip_rating": p.slip_rating, "style": p.style, "price": p.price_per_m2, "unit": "m2",
              "currency": p.currency, "image": photo(p.sku)} for p in CATALOG.values()]
    names = {"basin": "BASIN", "shower": "SHOWER", "wc": "WC"}
    sets = [{"sku": s.sku, "name": s.name, "kind": "set", "category": s.category, "finish": s.finish,
             "includes": s.includes, "price": s.price, "unit": "set", "currency": s.currency,
             "image": photo(names[s.category])} for s in SANITARY.values()]
    return {"currency": "QAR", "products": tiles + sets,
            "rooms": {"bathroom": photo("room_bathroom"), "kitchen": photo("room_kitchen")}}


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


@app.get("/api/v1/quotes/{quotation_id}/pdf")
def download_quote_pdf(quotation_id: str, x_user_id: str = Header(...)):
    """Same ownership check as above, returned as a downloadable PDF."""
    q = read_quote(quotation_id, x_user_id)
    return Response(content=build_quote_pdf(q), media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="Quotation-{q.quotation_id}.pdf"'})


class DirectQuoteRequest(BaseModel):
    items: list[QuoteItemRequest] = Field(min_length=1, max_length=20)


@app.post("/api/v1/quotes")
def create_quote(req: DirectQuoteRequest, x_user_id: str = Header(...)):
    """The "PDF quotation" button: builds the quote straight from the items on screen.
    No AI call and no discount, so it is instant; every number comes from tools.generate_quote."""
    if not rate_limiter.allow(x_user_id):
        raise HTTPException(status_code=429, detail="Too many requests, please slow down.")
    try:
        q = generate_quote(x_user_id, req.items)
    except (ValueError, KeyError) as e:
        raise HTTPException(status_code=422, detail=str(e))
    save_quote(q)
    return {"quote": q.model_dump(), "offers": project_offers(q.total)}


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
