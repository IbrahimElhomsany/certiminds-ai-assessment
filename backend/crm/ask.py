"""Feature 11: "Ask your data" for Nova Studio staff.

How it stays safe:
- Claude never writes SQL and never sees the database. It can only call the READ-ONLY tools below.
  Each tool runs our own query code, always filtered to the user's company, and to the user's own
  leads when their role only sees their own (salespeople). Nothing here can change data.
- The answer is checked: every number in it must appear in what the tools returned, otherwise the
  answer is replaced by the raw figures. The screen shows which tools were used, so staff can verify.
- Without an Anthropic key the page still works in "offline mode": simple keyword matching picks one
  tool and the figures are shown as they are (clearly labelled, no AI).
"""
import json
import os
import re
import time
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session

from backend.auth import audit, has_permission
from backend.crm import analytics as A
from backend.db.database import local_now
from backend.db.models import Customer, Lead, Product, Quote, Tenant, User

MAX_ROUNDS = 5
MAX_QUESTION = 300
GROUPS_SALES = ("day", "week", "month", "salesperson", "source", "product", "customer_type")
GROUPS_LEADS = ("stage", "source", "salesperson", "room", "lost_reason")


def _days(x) -> int:
    try:
        return max(1, min(int(x or 30), 365))
    except (TypeError, ValueError):
        return 30


def _window(days: int):
    end = local_now()
    return end - timedelta(days=days), end


# ------------------------------------------------------------------ read-only tools
def t_kpis(db: Session, user: User, days=30) -> dict:
    days = _days(days)
    start, end = _window(days)
    return {"period_days": days, "from": start.date().isoformat(), "to": end.date().isoformat(), **A.kpis(db, user, start, end)}


def t_sales_breakdown(db: Session, user: User, days=30, group_by="salesperson") -> dict:
    days, group_by = _days(days), group_by if group_by in GROUPS_SALES else "salesperson"
    start, end = _window(days)
    orders = db.scalars(A.orders_query(user, start, end)).all()
    rows = defaultdict(lambda: {"revenue": Decimal(0), "gross_profit": Decimal(0), "orders": 0})
    if group_by == "product":
        for o in orders:
            for l in o.lines:
                p = db.get(Product, l.product_id)
                r = rows[p.name]
                r["revenue"] += l.line_total
                r["gross_profit"] += l.line_total - l.quantity * l.unit_cost
                r["orders"] += 1
    else:
        for o in orders:
            quote = db.get(Quote, o.quote_id) if o.quote_id else None
            lead = db.get(Lead, quote.lead_id) if quote and quote.lead_id else None
            key = {"day": lambda: o.created_at.date().isoformat(),
                   "week": lambda: f"week of {(o.created_at - timedelta(days=o.created_at.weekday())).date().isoformat()}",
                   "month": lambda: o.created_at.strftime("%Y-%m"),
                   "salesperson": lambda: lead.owner.name if lead and lead.owner else "Unassigned",
                   "source": lambda: lead.source if lead else "unknown",
                   "customer_type": lambda: db.get(Customer, o.customer_id).type}[group_by]()
            r = rows[key]
            r["revenue"] += o.total
            r["gross_profit"] += o.total - o.cost_total
            r["orders"] += 1
    out = sorted(({"group": k, "revenue": A.f(v["revenue"]), "gross_profit": A.f(v["gross_profit"]),
                   "orders": v["orders"],
                   "margin_pct": round(float(v["gross_profit"] / v["revenue"] * 100), 1) if v["revenue"] else 0.0}
                  for k, v in rows.items()), key=lambda r: (-r["revenue"] if group_by not in ("day", "week", "month") else 0, r["group"]))
    return {"period_days": days, "group_by": group_by, "currency": "QAR",
            "total_revenue": A.f(sum(r["revenue"] for r in out)), "rows": out[:25]}


def t_leads_breakdown(db: Session, user: User, days=30, group_by="stage") -> dict:
    days, group_by = _days(days), group_by if group_by in GROUPS_LEADS else "stage"
    start, end = _window(days)
    leads = db.scalars(A.leads_query(user).where(Lead.created_at >= start, Lead.created_at < end)).all()
    counts = defaultdict(lambda: {"leads": 0, "won": 0, "lost": 0})
    for l in leads:
        key = {"stage": l.stage, "source": l.source, "room": l.room or "unknown", "lost_reason": l.lost_reason,
               "salesperson": l.owner.name if l.owner else "Unassigned"}[group_by]
        if group_by == "lost_reason" and l.stage != "lost":
            continue
        c = counts[key or "not given"]
        c["leads"] += 1
        c["won"] += l.stage == "won"
        c["lost"] += l.stage == "lost"
    rows = [{"group": k, **v, "win_rate_pct": round(v["won"] / v["leads"] * 100, 1) if v["leads"] else 0.0}
            for k, v in sorted(counts.items(), key=lambda kv: -kv[1]["leads"])]
    return {"period_days": days, "group_by": group_by, "leads_created": len(leads), "rows": rows}


def t_top_products(db: Session, user: User, days=30, limit=5) -> dict:
    days = _days(days)
    start, end = _window(days)
    return {"period_days": days, "currency": "QAR",
            "products": A.top_products(db, user, start, end, limit=max(1, min(int(limit or 5), 15)))}


def t_stock(db: Session, user: User, status: Optional[str] = None, sku: Optional[str] = None) -> dict:
    rows = A.stock_levels(db, user.tenant_id)
    if sku:
        rows = [r for r in rows if r["sku"].lower() == str(sku).lower() or str(sku).lower() in r["name"].lower()]
    if status in ("out", "critical", "low", "ok"):
        rows = [r for r in rows if r["status"] == status]
    keep = ("sku", "name", "unit", "stock", "largest_batch", "batches", "sold_per_day", "days_of_stock", "status")
    return {"note": "days_of_stock = stock / average sold per day over the last 30 days",
            "products": [{k: r[k] for k in keep} for r in rows[:20]]}


def t_waiting_quotes(db: Session, user: User) -> dict:
    rows = A.stalled_quotes(db, user, local_now())
    return {"rule": f"sent more than {A.STALLED_DAYS} days ago, lead still open, nothing logged since",
            "count": len(rows), "total_value": A.f(sum(r["total"] for r in rows)),
            "quotes": [{k: r[k] for k in ("quote", "lead", "customer", "owner", "total", "days_waiting")} for r in rows[:15]]}


def t_pipeline(db: Session, user: User, days=30) -> dict:
    days = _days(days)
    start, end = _window(days)
    return {"period_days": days, **A.pipeline(db, user, start, end), "projects_now": A.projects(db, user, start)}


TOOLS = {
    "get_kpis": (t_kpis, "Revenue, gross profit, orders, new leads, conversion rate and margin for the last N days, "
                         "each compared with the N days before (change = % change; conversion_pp = percentage points).",
                 {"days": {"type": "integer", "description": "Period length in days, e.g. 7, 30, 90"}}),
    "sales_breakdown": (t_sales_breakdown, "Confirmed/delivered sales for the last N days grouped by one dimension.",
                        {"days": {"type": "integer"},
                         "group_by": {"type": "string", "enum": list(GROUPS_SALES)}}),
    "leads_breakdown": (t_leads_breakdown, "Leads created in the last N days grouped by one dimension, with won, lost "
                                           "and win rate. Use group_by=lost_reason to see why leads were lost.",
                        {"days": {"type": "integer"}, "group_by": {"type": "string", "enum": list(GROUPS_LEADS)}}),
    "top_products": (t_top_products, "Best-selling products by revenue for the last N days, with change vs the period before.",
                     {"days": {"type": "integer"}, "limit": {"type": "integer"}}),
    "stock": (t_stock, "Current stock per product: quantity, largest single batch, sold per day, days of stock left, status "
                       "(out/critical/low/ok). Optional filter by status or by SKU/name.",
              {"status": {"type": "string", "enum": ["out", "critical", "low", "ok"]}, "sku": {"type": "string"}}),
    "waiting_quotes": (t_waiting_quotes, "Quotations waiting for a customer reply with no follow-up logged.", {}),
    "pipeline": (t_pipeline, "Sales funnel of leads created in the last N days (how many reached each stage), open "
                             "pipeline value, and where live projects are now.", {"days": {"type": "integer"}}),
}
TOOL_DEFINITIONS = [{"name": n, "description": d, "input_schema": {"type": "object", "properties": props}}
                    for n, (_, d, props) in TOOLS.items()]


def run_tool(db: Session, user: User, name: str, args: dict) -> dict:
    if name not in TOOLS:
        return {"error": f"unknown tool {name}"}
    fn, _, props = TOOLS[name]
    clean = {k: v for k, v in (args or {}).items() if k in props}
    return json.loads(json.dumps(fn(db, user, **clean), default=str))


# ------------------------------------------------------------------ checking the answer
NUM = re.compile(r"(?<![A-Za-z])-?\d[\d,]*(?:\.\d+)?")


def numbers_in(text: str) -> list[float]:
    out = []
    for m in NUM.findall(text):
        try:
            out.append(float(m.replace(",", "")))
        except ValueError:
            pass
    return out


def ungrounded(answer: str, outputs: list, question: str) -> list[float]:
    """Numbers in the answer that don't come from the tool results (or the question itself)."""
    known = set()
    for v in numbers_in(json.dumps(outputs) + " " + question):
        known |= {v, round(v), round(v, 1), abs(v)}
        if v >= 1000:
            known |= {round(v / 1000, 1), round(v / 1000), round(v / 1e6, 2), round(v / 1e6, 1)}   # "QAR 12.5k", "1.2M"
    bad = []
    for n in numbers_in(answer):
        if n <= 31 and float(n).is_integer():
            continue                         # small counts, dates, "top 5", "7 days": too noisy to check
        if not any(abs(n - k) <= max(0.051, abs(k) * 0.001) for k in known):
            bad.append(n)
    return bad


SYSTEM = """You answer questions from {company} staff about their own business data in Nova Studio.
Today is {today}. The user is {name} ({role}); {scope}. Currency is QAR. All data is demo data.

Rules:
- Get every figure from the tools. Never estimate, invent or do arithmetic the tools did not give you,
  except simple differences or totals of numbers the tools returned.
- If the tools can't answer (for example the data isn't recorded), say so plainly and suggest what they can ask.
- Keep it short: 2 to 5 sentences or a few bullet points. Lead with the answer. Write money as "QAR 12,345".
- Say which period the figures cover. Don't mention tool names, JSON or SQL.
- You can only read data. If asked to change something, say that is done in the admin screens."""


def _system(db: Session, user: User) -> str:
    t = db.get(Tenant, user.tenant_id)
    scope = ("figures cover the whole company" if A.own_leads(user) is None
             else "figures cover only this salesperson's own leads and orders")
    return SYSTEM.format(company=t.name, today=local_now().strftime("%A %d %B %Y"), name=user.name,
                         role=user.role.replace("_", " "), scope=scope)


# ------------------------------------------------------------------ offline mode
OFFLINE_RULES = [
    (r"\b(lost|lose|losing)\b", "leads_breakdown", {"group_by": "lost_reason"}),
    (r"\b(stock|inventory|run(ning)? out|reorder|batch)", "stock", {}),
    (r"\b(follow|waiting|stalled|no reply|chase)", "waiting_quotes", {}),
    (r"\b(funnel|pipeline|stage)", "pipeline", {}),
    (r"\b(source|channel|where .* come|chat|walk.?in)", "leads_breakdown", {"group_by": "source"}),
    (r"\b(best|top|sell(ing)?|popular) .*product|\bproducts?\b", "top_products", {}),
    (r"\b(salesperson|salespeople|team|who sold|staff|omar|lina|yusuf)", "sales_breakdown", {"group_by": "salesperson"}),
    (r"\b(week|weekly)", "sales_breakdown", {"group_by": "week"}),
    (r"\b(revenue|sales|profit|margin|conversion|orders?|kpi|how .* doing)", "get_kpis", {}),
]


def _period(question: str) -> Optional[int]:
    q = question.lower()
    m = re.search(r"(\d{1,3})\s*(day|week|month)", q)
    if m:
        return int(m.group(1)) * {"day": 1, "week": 7, "month": 30}[m.group(2)]
    for word, d in (("today", 1), ("this week", 7), ("last week", 7), ("quarter", 90), ("this month", 30),
                    ("last month", 30), ("year", 365)):
        if word in q:
            return d
    return None


def offline_pick(question: str) -> tuple[Optional[str], dict]:
    q = question.lower()
    for pattern, tool, args in OFFLINE_RULES:
        if re.search(pattern, q):
            args = dict(args)
            if "days" in TOOLS[tool][2]:
                args["days"] = _period(question) or 30
            return tool, args
    return None, {}


# ------------------------------------------------------------------ main entry
def ask(db: Session, user: User, question: str, client=None, model: Optional[str] = None) -> dict:
    question = " ".join((question or "").split())[:MAX_QUESTION]
    if not question:
        raise ValueError("Type a question first.")
    calls: list[dict] = []

    def call(name, args):
        out = run_tool(db, user, name, args)
        calls.append({"tool": name, "input": args, "output": out})
        return out

    mode, answer, note, usage = "offline", None, None, None
    if client is not None:
        model = model or os.getenv("ADMIN_LLM_MODEL") or os.getenv("LLM_MODEL", "claude-opus-5-5")
        try:
            answer, usage = _claude(client, model, _system(db, user), question, call)
            mode = "claude"
            bad = ungrounded(answer, [c["output"] for c in calls], question)
            if bad:
                note = ("The AI answer contained figures that don't match the data "
                        f"({', '.join(f'{b:g}' for b in bad[:3])}), so it was hidden. Here are the figures it looked up.")
                answer, mode = None, "checked_fallback"
        except Exception as e:                       # API down, timeout, bad key: fall back, never break the page
            note = f"Claude was not reachable ({type(e).__name__}), so this is the offline answer."
            calls.clear()
    if mode == "offline":
        tool, args = offline_pick(question)
        if tool:
            call(tool, args)
        else:
            note = note or ("Offline mode only understands simple questions about sales, profit, products, stock, "
                            "leads, lost reasons, the pipeline and quotations waiting for a reply.")
    audit(db, user.tenant_id, "ai.ask", user.id, after={"question": question, "mode": mode,
                                                         "tools": [c["tool"] for c in calls]})
    db.commit()
    return {"question": question, "answer": answer, "mode": mode, "note": note, "calls": calls, "usage": usage,
            "scope": "company" if A.own_leads(user) is None else "my leads"}


def _claude(client, model: str, system: str, question: str, call) -> tuple[str, dict]:
    from backend.observability import METRICS
    from backend.providers import estimate_cost
    messages = [{"role": "user", "content": question}]
    total_in = total_out = 0
    for _ in range(MAX_ROUNDS):
        t0 = time.perf_counter()
        resp = client.messages.create(model=model, max_tokens=1200, system=system, tools=TOOL_DEFINITIONS,
                                      messages=messages)
        total_in += resp.usage.input_tokens
        total_out += resp.usage.output_tokens
        METRICS.record_latency("llm", (time.perf_counter() - t0) * 1000)
        METRICS.record_llm(model, resp.usage.input_tokens, resp.usage.output_tokens,
                           estimate_cost(model, resp.usage.input_tokens, resp.usage.output_tokens))
        if resp.stop_reason != "tool_use":
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            if not text:
                raise ValueError("empty answer")
            return text, {"model": model, "input_tokens": total_in, "output_tokens": total_out,
                          "cost_usd": estimate_cost(model, total_in, total_out)}
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for b in resp.content:
            if b.type == "tool_use":
                out = call(b.name, dict(b.input))
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(out, default=str)})
        messages.append({"role": "user", "content": results})
    raise RuntimeError("too many tool rounds")


SUGGESTIONS = [
    "How are we doing this month compared with last month?",
    "Which salesperson sold the most in the last 30 days?",
    "Why are we losing leads?",
    "Which products will run out first?",
    "Where do our leads come from, and which source wins most often?",
    "Which quotations are waiting for a reply?",
]
