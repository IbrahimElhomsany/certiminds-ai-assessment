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
from backend.store import SESSIONS, AccessDenied, get_or_create_session, get_quote, save_quote  # noqa: E402
from backend.tools import QuoteItemRequest, generate_quote           # noqa: E402
from backend.offers import project_offers                            # noqa: E402
from typing import Optional                                          # noqa: E402
from pydantic import BaseModel, Field, field_validator               # noqa: E402
from backend import crm_bridge                                       # noqa: E402
from backend.quote_pdf import build_quote_pdf                        # noqa: E402
from backend.catalog import CATALOG, SANITARY                         # noqa: E402
from backend.admin_api import router as admin_router                 # noqa: E402
from backend.shop_api import router as shop_router                   # noqa: E402
from backend.visual import router as visual_router                   # noqa: E402
from backend.db.database import init_db                               # noqa: E402


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


app = FastAPI(title="Ceramica Nova AI Sales API + Nova Studio admin", version="1.1")
init_db()                      # creates missing tables; demo data comes from `python -m backend.db.seed`
app.include_router(admin_router)
app.include_router(shop_router)
app.include_router(visual_router)
gateway = LLMGateway(build_providers(), timeout_s=float(os.getenv("LLM_TIMEOUT_S", "30")),
                     cache=TTLCache(ttl_seconds=3600), prompt_version=PROMPT_VERSION)
agent_client = None
if use_claude():
    import anthropic
    from backend.deploy import BudgetedClient
    agent_client = BudgetedClient(anthropic.Anthropic(max_retries=2, timeout=60))   # daily cap on Claude calls
agent = SalesAgent(gateway, client=agent_client)
from backend.deploy import start_housekeeping  # noqa: E402
start_housekeeping()           # demo hosting: seed an empty database, optional daily reset (env flags, off by default)
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
def shop():
    """The public shop (no login)."""
    return FileResponse(Path(__file__).resolve().parent.parent / "frontend" / "shop.html")


@app.get("/assistant")
def ui():
    """Ceramica Nova AI, the design and quotation assistant."""
    return FileResponse(Path(__file__).resolve().parent.parent / "frontend" / "index.html")


@app.get("/admin")
def admin_ui():
    """Nova Studio, the staff admin. Not linked from the public shop; every API call needs a staff login."""
    return FileResponse(Path(__file__).resolve().parent.parent / "frontend" / "admin.html",
                        headers={"X-Robots-Tag": "noindex"})


@app.get("/admin/showroom.jpg")
def admin_showroom_photo():
    """Photo for the admin menu and login page: images/room_living.jpg if the shop adds one, else the bathroom photo."""
    images = Path(__file__).resolve().parent.parent / "images"
    living = images / "room_living.jpg"
    return FileResponse(living if living.exists() else images / "room_bathroom.jpg",
                        headers={"Cache-Control": "public, max-age=3600"})


ROOT = Path(__file__).resolve().parent.parent
app.mount("/images", StaticFiles(directory=ROOT / "images", check_dir=False), name="images")
app.mount("/assets", StaticFiles(directory=ROOT / "frontend" / "assets", check_dir=False), name="assets")


@app.get("/api/v1/catalog")
def catalog():
    """Public product list for the showroom page: names, sizes, prices (QAR) and photos.
    Stock levels are not included; they are checked by the tools when a customer asks."""
    crm_bridge.refresh_catalog()
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
    session_id: Optional[str] = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{4,64}$")   # links it to the chat lead


@app.post("/api/v1/quotes")
def create_quote(req: DirectQuoteRequest, x_user_id: str = Header(...)):
    """The "PDF quotation" button: builds the quote straight from the items on screen.
    No AI call and no discount, so it is instant; every number comes from tools.generate_quote."""
    if not rate_limiter.allow(x_user_id):
        raise HTTPException(status_code=429, detail="Too many requests, please slow down.")
    crm_bridge.refresh_catalog()
    try:
        q = generate_quote(x_user_id, req.items)
    except (ValueError, KeyError) as e:
        raise HTTPException(status_code=422, detail=str(e))
    if req.session_id:                       # save it in Nova Studio > Quotations, on this chat's lead
        try:
            session = get_or_create_session(req.session_id, x_user_id)
        except AccessDenied:
            raise HTTPException(status_code=403, detail="This session belongs to another user.")
        number = crm_bridge.save_quote(session, q)
        if number:
            q.quotation_id = number
        session.quote_ids.append(q.quotation_id)
    save_quote(q)
    return {"quote": q.model_dump(), "offers": project_offers(q.total)}


class CallbackRequest(BaseModel):
    user_id: str = Field(pattern=r"^USR-\d{3,10}$")
    session_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{4,64}$")
    name: str = Field(min_length=2, max_length=80)
    phone: str = Field(min_length=8, max_length=20)

    @field_validator("phone")
    @classmethod
    def phone_ok(cls, v: str) -> str:
        if not 8 <= sum(c.isdigit() for c in v) <= 15 or any(c not in "+0123456789 -()" for c in v):
            raise ValueError("enter a valid phone number")
        return v.strip()


@app.post("/api/v1/chat/callback")
def chat_callback(req: CallbackRequest):
    """The visitor leaves a name and phone in the chat: the lead gets their contact details and
    the salesperson sees the whole conversation summary before calling."""
    if not rate_limiter.allow(req.user_id):
        raise HTTPException(status_code=429, detail="Too many requests, please slow down.")
    session = SESSIONS.get(req.session_id)
    if session is None or session.user_id != req.user_id:
        raise HTTPException(status_code=404, detail="Please send Nova a message about your project first.")
    result = crm_bridge.request_callback(session, req.name.strip(), req.phone)
    if result is None:
        raise HTTPException(status_code=503, detail="Sorry, we couldn't save that right now. Please call the showroom.")
    return {"ok": True, "salesperson": result["salesperson"]}


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
