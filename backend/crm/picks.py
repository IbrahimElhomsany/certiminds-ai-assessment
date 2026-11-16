"""What customers really choose (D3): tile suggestions that learn from quotes, orders and the room visualiser.

Two things are learned from the company's own data, and both are simple counts anyone can check:
- Popular per room: in how many of the company's bathroom (kitchen, majlis...) projects a tile was chosen.
  A "project" is a quotation for that room; a won order counts double (they actually bought it), and a tile a
  visitor added to their project in the visualiser counts half (they liked it, but did not ask for a price yet).
- Often chosen together: tiles that appear in the same quotation (a floor with a wall tile, a tile with a
  bathroom set) more often than chance would give ("lift" above 1.3, seen together at least 3 times).

How it is used, never overruling safety or the customer's own wishes:
- The assistant's ranking (recommender.py) gets a small bonus, at most +12 points, for a tile popular for that
  room. Hard rules (slip rating, wall-only, outdoor) are applied first, so a popular tile that is unsafe for the
  room is still left out; style, budget and stock count for more.
- The shop's product page and the visualiser show "Often chosen with this", with the real count.
Nothing is sent outside, and visualiser events are anonymous (a random browser id).
"""
from __future__ import annotations

import threading
from collections import Counter, defaultdict
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.db.database import get_db, local_now
from backend.db.models import ChoiceEvent, Lead, Order, Product, Quote, QuoteLine, Tenant
from backend.ratelimit import RateLimiter

MIN_PROJECTS = 10            # fewer projects than this for a room: too little to call anything "popular"
MAX_BONUS = 12.0
PAIR_MIN, PAIR_LIFT = 3, 1.3

# visualiser room ids -> the room words the CRM uses
VIS_ROOMS = {"bath": "bathroom", "spa": "bathroom", "kitchen": "kitchen", "living": "living room", "tv": "living room",
             "lounge": "living room", "bedroom": "bedroom", "office": "office", "terrace": "outdoor", "outdoor": "outdoor",
             "majlis": "majlis"}


def room_word(text: Optional[str]) -> Optional[str]:
    t = (text or "").lower()
    for k, v in VIS_ROOMS.items():
        if k in t:
            return v
    return None


# ------------------------------------------------------------------ learning (counts) with a cache
_cache: dict[int, tuple[tuple, dict]] = {}
_lock = threading.Lock()


def learned(db: Session, tenant_id: int) -> dict:
    key = (db.scalar(select(func.count(Quote.id)).where(Quote.tenant_id == tenant_id)),
           db.scalar(select(func.count(Order.id)).where(Order.tenant_id == tenant_id)),
           db.scalar(select(func.max(ChoiceEvent.id)).where(ChoiceEvent.tenant_id == tenant_id)))
    with _lock:
        hit = _cache.get(tenant_id)
        if hit and hit[0] == key:
            return hit[1]
    skus = dict(db.execute(select(Product.id, Product.sku).where(Product.tenant_id == tenant_id)).all())
    names = dict(db.execute(select(Product.sku, Product.name).where(Product.tenant_id == tenant_id)).all())
    lines: dict[int, set[str]] = defaultdict(set)
    for qid, pid in db.execute(select(QuoteLine.quote_id, QuoteLine.product_id).join(Quote, Quote.id == QuoteLine.quote_id)
                               .where(Quote.tenant_id == tenant_id)):
        lines[qid].add(skus[pid])
    room_of = dict(db.execute(select(Quote.id, Lead.room).join(Lead, Lead.id == Quote.lead_id)
                              .where(Quote.tenant_id == tenant_id)).all())
    won = {qid for (qid,) in db.execute(select(Order.quote_id).where(Order.tenant_id == tenant_id, Order.status != "cancelled",
                                                                       Order.quote_id.is_not(None)))}
    rooms: dict[str, dict] = defaultdict(lambda: {"projects": 0.0, "chosen": Counter(), "bought": Counter(), "liked": Counter(),
                                                  "quotes": 0, "quoted": Counter()})
    for qid, items in lines.items():
        r = room_of.get(qid)
        if not r:
            continue
        w = 2.0 if qid in won else 1.0
        rooms[r]["projects"] += w
        rooms[r]["quotes"] += 1
        for s in items:
            rooms[r]["quoted"][s] += 1
            rooms[r]["chosen"][s] += w
            rooms[r]["bought"][s] += 1 if qid in won else 0
    since = local_now() - timedelta(days=180)
    events = db.execute(select(ChoiceEvent.visitor_id, ChoiceEvent.sku, ChoiceEvent.room, ChoiceEvent.with_sku)
                        .where(ChoiceEvent.tenant_id == tenant_id, ChoiceEvent.kind == "added",
                               ChoiceEvent.created_at >= since)).all()
    for _, sku, room, other in events:
        if room:
            rooms[room]["projects"] += 0.5
            rooms[room]["chosen"][sku] += 0.5
            rooms[room]["liked"][sku] += 1
    # often chosen together: quotations plus the floor + wall a visitor put in their project
    baskets = [items for items in lines.values() if len(items) > 1]
    baskets += [{s, o} for _, s, _, o in events if o and o != s]
    n = len(lines) + len(events)
    single, both = Counter(), Counter()
    for items in lines.values():
        single.update(items)
    for _, s, _, _ in events:
        single[s] += 1
    for b in baskets:
        for a in b:
            for c in b:
                if a != c:
                    both[(a, c)] += 1
    pairs: dict[str, list] = defaultdict(list)
    for (a, c), k in both.items():
        lift = (k / single[a]) / (single[c] / n) if single[a] and single[c] and n else 0
        if k >= PAIR_MIN and lift >= PAIR_LIFT:
            pairs[a].append({"sku": c, "name": names.get(c, c), "together": k, "of": single[a], "lift": round(lift, 2)})
    for a in pairs:
        pairs[a].sort(key=lambda x: (-x["together"], -x["lift"]))
        pairs[a] = pairs[a][:3]
    out = {"rooms": {r: {"projects": v["projects"], "chosen": dict(v["chosen"]), "bought": dict(v["bought"]),
                         "liked": dict(v["liked"]), "quotes": v["quotes"], "quoted": dict(v["quoted"])}
                     for r, v in rooms.items()},
           "pairs": dict(pairs), "names": names, "events": len(events),
           "demo": bool(getattr(db.get(Tenant, tenant_id), "is_demo", False))}
    with _lock:
        _cache[tenant_id] = (key, out)
    return out


def popularity(model: dict, room: Optional[str], sku: str) -> Optional[tuple[float, str]]:
    """(bonus points, reason) for the assistant's ranking, or None when there is too little to go on."""
    r = model["rooms"].get(room or "")
    if not r or r["projects"] < MIN_PROJECTS:
        return None
    share = r["chosen"].get(sku, 0.0) / r["projects"]
    typical = sum(r["chosen"].values()) / r["projects"] / max(1, len(r["chosen"]))   # an average tile's share
    bonus = max(-4.0, min(MAX_BONUS, (share - typical) * 40))
    if bonus < 3:
        return (bonus, "") if bonus < 0 else None
    n, total = r["quoted"].get(sku, 0), r["quotes"]
    return bonus, f"popular for {room} projects here (in {n} of {total} quotations)"


# ------------------------------------------------------------------ live snapshot for the assistant (shop company)
_live: dict = {}


def refresh_live(db: Session, tenant_id: int) -> None:
    _live["model"] = learned(db, tenant_id)


def live_bonus(req, sku: str) -> Optional[tuple[float, str]]:
    """Called by recommender.py for every allowed tile. Uses the last snapshot (refreshed with the catalog)."""
    m = _live.get("model")
    if not m:
        return None
    from backend.crm_bridge import room_of
    return popularity(m, room_of(req), sku)


def live_pairs(sku: str) -> list[dict]:
    m = _live.get("model")
    return (m or {}).get("pairs", {}).get(sku, [])


# ------------------------------------------------------------------ public API: record a visualiser choice, read pairs
router = APIRouter(tags=["Choices"])
limiter = RateLimiter(max_requests=60, window_seconds=60)


class ChoiceIn(BaseModel):
    sku: str = Field(min_length=3, max_length=20)
    kind: str = Field(pattern="^(tried|added)$")
    room: Optional[str] = Field(default=None, max_length=40)       # the visualiser room id
    with_sku: Optional[str] = Field(default=None, max_length=20)


def _tenant(db: Session) -> Tenant:
    from backend.shop_api import SHOP_TENANT
    t = db.scalar(select(Tenant).where(Tenant.slug == SHOP_TENANT))
    if t is None:
        raise HTTPException(status_code=404, detail="Shop not set up.")
    return t


@router.post("/api/v1/choices", status_code=204)
def record_choice(c: ChoiceIn, request: Request, x_user_id: Optional[str] = Header(default=None),
                  db: Session = Depends(get_db)):
    visitor = (x_user_id or "")[:40] or "anon-" + (request.client.host if request.client else "x")
    if not limiter.allow(visitor):
        raise HTTPException(status_code=429, detail="Too many requests.")
    t = _tenant(db)
    sku = c.sku.strip().upper()
    if not db.scalar(select(Product.id).where(Product.tenant_id == t.id, Product.sku == sku)):
        raise HTTPException(status_code=404, detail="Unknown product.")
    other = (c.with_sku or "").strip().upper() or None
    room = room_word(c.room)
    day = local_now().replace(hour=0, minute=0, second=0, microsecond=0)
    dup = db.scalar(select(ChoiceEvent.id).where(ChoiceEvent.tenant_id == t.id, ChoiceEvent.visitor_id == visitor,
                                                 ChoiceEvent.sku == sku, ChoiceEvent.kind == c.kind,
                                                 ChoiceEvent.created_at >= day))
    if dup is None:                      # one vote per visitor, tile and kind per day: clicking around can't stuff it
        db.add(ChoiceEvent(tenant_id=t.id, visitor_id=visitor, sku=sku, kind=c.kind, room=room, with_sku=other,
                           created_at=local_now()))
        db.commit()


@router.get("/api/v1/choices/pairs/{sku}")
def pairs_for(sku: str, db: Session = Depends(get_db)) -> dict:
    t = _tenant(db)
    m = learned(db, t.id)
    return {"sku": sku.upper(), "often_with": m["pairs"].get(sku.strip().upper(), []), "demo": m["demo"]}


def summary(db: Session, tenant_id: int) -> dict:
    """For the admin: per room, the most chosen tiles, and the strongest pairs."""
    m = learned(db, tenant_id)
    rooms = []
    for r, v in sorted(m["rooms"].items(), key=lambda kv: -kv[1]["projects"]):
        top = sorted(v["chosen"].items(), key=lambda kv: -kv[1])[:3]
        rooms.append({"room": r, "projects": v["quotes"], "enough": v["projects"] >= MIN_PROJECTS,
                      "top": [{"sku": s, "name": m["names"].get(s, s), "chosen": v["quoted"].get(s, 0), "bought": v["bought"].get(s, 0),
                               "liked": v["liked"].get(s, 0)} for s, n in top]})
    seen, pairs = set(), []
    for a, lst in m["pairs"].items():
        for p in lst:
            k = tuple(sorted((a, p["sku"])))
            if k not in seen:
                seen.add(k)
                pairs.append({"a": m["names"].get(a, a), "b": p["name"], "together": p["together"], "lift": p["lift"]})
    pairs.sort(key=lambda p: (-p["together"], -p["lift"]))
    return {"rooms": rooms, "pairs": pairs[:6], "events": m["events"], "demo": m["demo"], "min_projects": MIN_PROJECTS}
