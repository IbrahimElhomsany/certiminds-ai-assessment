"""Feature 3: follow-up drafts ("autopilot" with a human in charge).

For every quotation waiting for a reply, Nova Studio can write a short follow-up message. It is only a
DRAFT: nothing is sent automatically. The salesperson reads it, edits it, sends it themselves (WhatsApp
or email), then marks it sent, which logs it on the lead's timeline.

Safety: the draft may not offer discounts or new prices. If Claude's draft contains any amount or
percentage that isn't on the quotation, or mentions a discount, it is replaced by the plain template.
"""
import os
import re
from typing import Optional

from sqlalchemy.orm import Session

from backend.auth import audit
from backend.crm import analytics as A
from backend.crm.ask import numbers_in
from backend.crm.operations import OpError, can_see_quote, note
from backend.db.database import local_now
from backend.db.models import Customer, Lead, Product, Quote, Tenant, User

CHANNELS = ("whatsapp", "email")
BANNED = re.compile(r"\b(discount|% off|percent off|reduc|cheaper|lower (the )?price|special (price|offer|deal)|free)\w*", re.I)


def waiting(db: Session, user: User) -> list[dict]:
    return A.stalled_quotes(db, user, local_now())


def facts(db: Session, quote: Quote) -> dict:
    lead = db.get(Lead, quote.lead_id) if quote.lead_id else None
    c = db.get(Customer, quote.customer_id)
    items = [db.get(Product, l.product_id).name for l in quote.lines]
    first = c.name.split()[0] if c.type == "retail" else c.name
    return {"customer": c.name, "first_name": first, "is_company": c.type != "retail", "phone": c.phone, "email": c.email,
            "quote": quote.number, "total": float(quote.total), "items": items[:4],
            "room": lead.room if lead else None, "valid_until": quote.valid_until.strftime("%d %B") if quote.valid_until else None,
            "expired": bool(quote.valid_until and quote.valid_until < local_now()),
            "days_waiting": (local_now() - quote.sent_at).days if quote.sent_at else None,
            "salesperson": lead.owner.name if lead and lead.owner else None,
            "company": db.get(Tenant, quote.tenant_id).name}


def template(f: dict, channel: str) -> str:
    items = " and ".join(f["items"][:2]) if f["items"] else "the tiles"
    hello = f"Hello {f['first_name']}," if not f["is_company"] else f"Hello {f['customer']} team,"
    validity = (f"The prices on it were valid until {f['valid_until']}, so I'm happy to check them again for you."
                if f["expired"] else f"The prices are held until {f['valid_until']}." if f["valid_until"] else "")
    body = (f"{hello}\n\nThis is {f['salesperson'] or 'the team'} from {f['company']}. I wanted to check whether you had a "
            f"chance to look at quotation {f['quote']} for {items}"
            + (f" for your {f['room'].replace('_', ' ')}" if f["room"] else "") + ". "
            f"{validity}\n\nIf you'd like to change any quantities, see samples in the showroom, or book delivery, "
            f"just reply here and I'll arrange it.\n\nBest regards,\n{f['salesperson'] or f['company']}")
    if channel == "email":
        body = f"Subject: Your quotation {f['quote']} from {f['company']}\n\n" + body
    return body.replace("  ", " ").strip()


PROMPT = """Write a short, friendly follow-up message from a tile showroom salesperson in Qatar to a customer
who received a quotation and has not replied. Channel: {channel}{email_note}.

Facts (use only these, don't invent anything else):
{facts}

Rules:
- 60 to 110 words, plain warm English, no emojis, no pressure or fake urgency.
- Mention the quotation number and what it is for. Offer help: change quantities, see samples, book delivery.
- Do NOT offer any discount, new price, free item or promise. Don't state the total unless it helps.
- If the quotation has expired, offer to re-check prices; don't say they changed.
- Sign with the salesperson's name. Output only the message."""


def ai_draft(client, model: str, f: dict, channel: str) -> str:
    lines = "\n".join(f"- {k}: {v}" for k, v in f.items() if k not in ("phone", "email") and v not in (None, [], ""))
    resp = client.messages.create(model=model, max_tokens=500, messages=[{"role": "user", "content": PROMPT.format(
        channel=channel, email_note=" (start with a 'Subject:' line)" if channel == "email" else "", facts=lines)}])
    from backend.observability import METRICS
    from backend.providers import estimate_cost
    METRICS.record_llm(model, resp.usage.input_tokens, resp.usage.output_tokens,
                       estimate_cost(model, resp.usage.input_tokens, resp.usage.output_tokens))
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def safe(text: str, f: dict) -> Optional[str]:
    """Why a draft is not acceptable, or None if it's fine."""
    if not text or len(text) > 1500:
        return "empty or too long"
    if BANNED.search(text):
        return "mentions a discount or offer"
    allowed = {round(f["total"], 2), round(f["total"]), *numbers_in(f["quote"]), *numbers_in(f["valid_until"] or "")}
    for n in numbers_in(text):
        if n <= 31 and float(n).is_integer():
            continue
        if not any(abs(n - a) < 0.6 for a in allowed):
            return f"contains an amount that isn't on the quotation ({n:g})"
    return None


def draft(db: Session, user: User, quote: Quote, channel: str = "whatsapp", client=None) -> dict:
    if channel not in CHANNELS:
        raise OpError(422, "channel must be whatsapp or email")
    if not can_see_quote(db, user, quote):
        raise OpError(404, "Quotation not found.")
    if quote.status not in ("sent", "draft"):
        raise OpError(409, f"Quotation {quote.number} is {quote.status}; no follow-up needed.")
    f = facts(db, quote)
    text, mode, note_ = None, "template", None
    if client is not None:
        model = os.getenv("ADMIN_LLM_MODEL") or os.getenv("LLM_MODEL", "claude-opus-5-5")
        try:
            text = ai_draft(client, model, f, channel)
            problem = safe(text, f)
            if problem:
                note_ = f"The AI draft {problem}, so the standard template is shown instead."
                text = None
            else:
                mode = "claude"
        except Exception as e:
            note_ = f"Claude was not reachable ({type(e).__name__}); this is the standard template."
    text = text or template(f, channel)
    audit(db, quote.tenant_id, "ai.followup_draft", user.id, "quote", quote.number, after={"mode": mode, "channel": channel})
    db.commit()
    phone = re.sub(r"\D", "", f["phone"] or "")
    return {"quote": quote.number, "quote_id": quote.id, "channel": channel, "text": text, "mode": mode, "note": note_,
            "customer": f["customer"], "phone": f["phone"], "email": f["email"],
            "whatsapp_number": phone if len(phone) >= 8 else None}


def mark_sent(db: Session, user: User, quote: Quote, text: str, channel: str) -> None:
    if channel not in CHANNELS:
        raise OpError(422, "channel must be whatsapp or email")
    if not can_see_quote(db, user, quote):
        raise OpError(404, "Quotation not found.")
    text = (text or "").strip()
    if len(text) < 10:
        raise OpError(422, "The message is empty.")
    lead = db.get(Lead, quote.lead_id) if quote.lead_id else None
    if lead is None:
        raise OpError(409, "This quotation has no lead to log the follow-up on.")
    note(db, user, lead, "follow_up", f"Follow-up on {quote.number} sent by {channel.capitalize()}: {text[:600]}")
    audit(db, quote.tenant_id, "followup.sent", user.id, "quote", quote.number, after={"channel": channel})
    db.commit()
