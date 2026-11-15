"""Win chance: how likely an open lead is to turn into an order, learned from the company's own won and lost leads.

This is real machine learning, kept small and explainable on purpose:
- The model is a logistic regression (numpy only). It learns one weight per fact about a lead, for example
  "came from a referral" or "the quote is above the budget". A positive weight pushes towards "buys".
- Two models, because we know more about a lead once a quotation is out:
    early  - for leads without a sent quotation: source, customer type, returning customer, room, budget, area.
    quoted - for leads with a sent quotation: the same, plus quote vs budget, discount given or refused,
             how fast we first replied and whether the customer negotiated.
- It learns from CLOSED leads only (won or lost) and only from facts known before the outcome, so it never
  "cheats" by looking at the answer.
- Honesty check: every closed lead is also scored by a version of the model that never saw it (5-fold
  cross-validation). That score is shown to staff. With too few closed leads it says "not enough data yet" instead of guessing.
- It retrains by itself whenever a lead is won or lost (training takes milliseconds), so it keeps learning from
  daily work. Nothing is sent outside: no Claude, no external service.
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.db.database import local_now
from backend.db.models import Activity, DiscountRequest, Lead, Product, Quote, QuoteLine, Tenant, User

OPEN = ("new", "qualified", "quote_sent", "negotiation")
MIN_CLOSED, MIN_EACH = 30, 8          # below this we do not pretend to know
QUIET_DAYS = 14                       # after this long with no contact the chance is lowered
L2 = 4.0                              # keeps weights small, so a rare fact cannot dominate

SOURCE_TEXT = {"referral": "came from a referral", "walk_in": "walked into the showroom", "phone": "phoned in",
               "showroom_chat": "came from the AI chat", "shop": "came from the online shop"}
TYPE_TEXT = {"retail": "retail customer", "contractor": "contractor", "designer": "interior designer"}


@dataclass
class Model:
    kind: str                          # early | quoted
    names: list[str]
    mean: np.ndarray
    std: np.ndarray
    w: np.ndarray
    b: float
    trained_on: int
    won: int
    check: dict                        # results on leads it had not seen
    top: list[dict] = field(default_factory=list)


# ------------------------------------------------------------------ facts about a lead
def _facts(db: Session, tenant_id: int, leads: list[Lead]) -> dict[int, dict]:
    """One dict of facts per lead, using only what was known before it was won or lost."""
    ids = [l.id for l in leads]
    if not ids:
        return {}
    quotes: dict[int, Quote] = {}
    for q in db.scalars(select(Quote).where(Quote.tenant_id == tenant_id, Quote.lead_id.in_(ids),
                                            Quote.sent_at.is_not(None)).order_by(Quote.created_at)):
        quotes.setdefault(q.lead_id, q)                                  # the first quotation sent
    qids = [q.id for q in quotes.values()]
    disc = {d.quote_id: d for d in db.scalars(select(DiscountRequest).where(DiscountRequest.quote_id.in_(qids)))} if qids else {}
    sets = {qid for (qid,) in db.execute(select(QuoteLine.quote_id).join(Product, Product.id == QuoteLine.product_id)
                                         .where(QuoteLine.quote_id.in_(qids), Product.kind == "set"))} if qids else set()
    first_reply: dict[int, datetime] = {}
    negotiated: set[int] = set()
    for lead_id, typ, text, at in db.execute(select(Activity.lead_id, Activity.type, Activity.text, Activity.created_at)
                                             .where(Activity.lead_id.in_(ids)).order_by(Activity.created_at)):
        if typ != "created" and lead_id not in first_reply:
            first_reply[lead_id] = at
        if typ == "stage_change" and text.startswith("Customer asked to revise"):
            negotiated.add(lead_id)
    custs = {l.customer_id for l in leads}
    first_lead = dict(db.execute(select(Lead.customer_id, func.min(Lead.created_at))
                                 .where(Lead.tenant_id == tenant_id, Lead.customer_id.in_(custs)).group_by(Lead.customer_id)).all())
    out = {}
    for l in leads:
        q = quotes.get(l.id)
        # few facts on purpose: with a few hundred closed leads, more facts would learn noise (room, salesperson...)
        f = {"source=" + (l.source or "?"): 1.0, "type=" + (l.customer.type or "retail"): 1.0,
             "returning": 1.0 if first_lead.get(l.customer_id) and first_lead[l.customer_id] < l.created_at else 0.0,
             "log_budget": math.log1p(float(l.budget or 0))}
        if q is not None:
            fit = float(q.total) / float(l.budget) if l.budget else 1.0
            d = disc.get(q.id)
            reply = first_reply.get(l.id)
            f.update({"over_budget": max(0.0, fit - 1.0), "under_budget": max(0.0, 1.0 - fit),
                      "discount_pct": float(q.discount_pct or 0),
                      "discount_refused": 1.0 if d is not None and d.status in ("rejected", "pending") else 0.0,
                      "reply_days": min(5.0, (reply - l.created_at).total_seconds() / 86400) if reply else 2.0,
                      "negotiated": 1.0 if l.id in negotiated else 0.0})
        out[l.id] = f
    return out


EARLY_SKIP = ("over_budget", "under_budget", "discount_pct", "discount_refused", "has_set", "reply_days", "negotiated")


def _names(rows: list[dict], kind: str) -> list[str]:
    keys = sorted({k for r in rows for k in r})
    return [k for k in keys if kind == "quoted" or k not in EARLY_SKIP]


def _matrix(rows: list[dict], names: list[str]) -> np.ndarray:
    return np.array([[r.get(n, 0.0) for n in names] for r in rows], dtype=float)


# ------------------------------------------------------------------ training
def _fit(X: np.ndarray, y: np.ndarray, iters: int = 30) -> tuple[np.ndarray, float]:
    """Logistic regression by Newton's method with a small L2 penalty (the textbook way, no library)."""
    n, k = X.shape
    Xb = np.hstack([X, np.ones((n, 1))])
    w = np.zeros(k + 1)
    reg = np.full(k + 1, L2); reg[-1] = 0.0                       # the intercept is not penalised
    for _ in range(iters):
        p = 1 / (1 + np.exp(-np.clip(Xb @ w, -30, 30)))
        g = Xb.T @ (p - y) + reg * w
        H = (Xb * (p * (1 - p))[:, None]).T @ Xb + np.diag(reg) + 1e-9 * np.eye(k + 1)
        step = np.linalg.solve(H, g)
        w -= step
        if np.abs(step).max() < 1e-6:
            break
    return w[:-1], float(w[-1])


def _prob(X: np.ndarray, w: np.ndarray, b: float) -> np.ndarray:
    return 1 / (1 + np.exp(-np.clip(X @ w + b, -30, 30)))


def _auc(p: np.ndarray, y: np.ndarray) -> Optional[float]:
    """Chance that a random won lead scores above a random lost one (0.5 = guessing, 1.0 = perfect)."""
    pos, neg = p[y == 1], p[y == 0]
    if not len(pos) or not len(neg):
        return None
    wins = sum((pp > neg).sum() + 0.5 * (pp == neg).sum() for pp in pos)
    return float(wins / (len(pos) * len(neg)))


def _train(kind: str, rows: list[dict], y: np.ndarray, order: list[int]) -> Optional[Model]:
    if len(rows) < MIN_CLOSED or y.sum() < MIN_EACH or (len(y) - y.sum()) < MIN_EACH:
        return None
    names = _names(rows, kind)
    X = _matrix(rows, names)
    # honesty check: every closed lead is scored by a version of the model that never saw it (5 rounds, each
    # leaves out a fifth of the leads, learns from the rest and scores the left-out fifth)
    p = np.zeros(len(y))
    folds = np.array(order) % 5
    for k in range(5):
        tr, te = folds != k, folds == k
        mean, std = X[tr].mean(0), X[tr].std(0) + 1e-6
        w, b = _fit((X[tr] - mean) / std, y[tr])
        p[te] = _prob((X[te] - mean) / std, w, b)
    likely, unlikely = p >= 0.6, p < 0.4
    check = {"tested_on": int(len(y)), "auc": round(_auc(p, y), 2) if _auc(p, y) is not None else None,
             "base_rate": round(float(y.mean()) * 100),
             "likely_n": int(likely.sum()), "likely_won": round(float(y[likely].mean()) * 100) if likely.any() else None,
             "unlikely_n": int(unlikely.sum()), "unlikely_won": round(float(y[unlikely].mean()) * 100) if unlikely.any() else None}
    # the model staff use: learn from all closed leads
    mean, std = X.mean(0), X.std(0) + 1e-6
    w, b = _fit((X - mean) / std, y)
    m = Model(kind, names, mean, std, w, b, trained_on=len(rows), won=int(y.sum()), check=check)
    m.top = [{"fact": n, "text": _text(n, +1 if wt > 0 else -1), "weight": round(float(wt), 2)}
             for n, wt in sorted(zip(names, w), key=lambda t: -abs(t[1]))[:6] if abs(wt) > 0.05]
    return m


# ------------------------------------------------------------------ words for staff
def _text(name: str, sign: int, owners: Optional[dict] = None) -> str:
    key, _, val = name.partition("=")
    if key == "source":
        return SOURCE_TEXT.get(val, val)
    if key == "type":
        return TYPE_TEXT.get(val, val)
    if key == "room":
        return f"{val} project"
    if key == "owner":
        return f"handled by {(owners or {}).get(int(val), 'this salesperson')}"
    return {"returning": "returning customer", "log_budget": "bigger budget",
            "over_budget": "quote above the budget", "under_budget": "quote below the budget",
            "discount_pct": "bigger discount", "discount_refused": "discount refused or still waiting",
            "reply_days": "slow first reply", "negotiated": "customer negotiated"}.get(name, name)


def _reasons(m: Model, row: dict, owners: dict) -> list[dict]:
    """The facts that moved THIS lead's chance most, up or down (weight x how unusual the value is)."""
    x = (np.array([row.get(n, 0.0) for n in m.names]) - m.mean) / m.std
    contrib = x * m.w
    out = []
    for i in np.argsort(-np.abs(contrib)):
        c, n = float(contrib[i]), m.names[i]
        if abs(c) < 0.12 or len(out) >= 3:
            break
        if "=" in n and row.get(n, 0.0) == 0.0:
            continue                                  # "not from a referral" is not a useful reason
        if n == "reply_days":
            text = "fast first reply" if c > 0 else "slow first reply"
        elif n == "log_budget":
            text = "large budget" if row[n] > m.mean[i] else "small budget"
        else:
            text = _text(n, 1, owners)
        out.append({"text": text, "up": c > 0})
    return out


# ------------------------------------------------------------------ cache: retrain only when something closed
_cache: dict[int, tuple[tuple, dict]] = {}
_lock = threading.Lock()


def _models(db: Session, tenant_id: int) -> dict:
    stamp = db.execute(select(func.count(Lead.id), func.max(Lead.stage_changed_at))
                       .where(Lead.tenant_id == tenant_id, Lead.stage.in_(("won", "lost")))).one()
    key = (stamp[0], stamp[1])
    with _lock:
        hit = _cache.get(tenant_id)
        if hit and hit[0] == key:
            return hit[1]
    closed = db.scalars(select(Lead).where(Lead.tenant_id == tenant_id, Lead.stage.in_(("won", "lost")))
                        .order_by(Lead.stage_changed_at)).all()
    facts = _facts(db, tenant_id, closed)
    rows = [facts[l.id] for l in closed]
    y = np.array([1.0 if l.stage == "won" else 0.0 for l in closed])
    early = _train("early", rows, y, list(range(len(rows))))
    qi = [i for i, r in enumerate(rows) if "reply_days" in r]
    quoted = _train("quoted", [rows[i] for i in qi], y[qi], list(range(len(qi)))) if qi else None
    tenant = db.get(Tenant, tenant_id)
    out = {"early": early, "quoted": quoted, "closed": len(closed), "won": int(y.sum()),
           "demo": bool(tenant and tenant.is_demo), "trained_at": datetime.now().isoformat(timespec="seconds")}
    with _lock:
        _cache[tenant_id] = (key, out)
    return out


def score(db: Session, tenant_id: int, leads: list[Lead]) -> dict[int, Optional[dict]]:
    """{lead id: {"pct", "model", "reasons"}} for open leads; None for closed ones or when there is too little data."""
    open_leads = [l for l in leads if l.stage in OPEN]
    res: dict[int, Optional[dict]] = {l.id: None for l in leads}
    if not open_leads:
        return res
    ms = _models(db, tenant_id)
    owners = {u.id: u.name for u in db.scalars(select(User).where(User.tenant_id == tenant_id))}
    facts = _facts(db, tenant_id, open_leads)
    last = dict(db.execute(select(Activity.lead_id, func.max(Activity.created_at))
                           .where(Activity.lead_id.in_([l.id for l in open_leads])).group_by(Activity.lead_id)).all())
    now = local_now()
    for l in open_leads:
        row = facts[l.id]
        m = ms["quoted"] if "reply_days" in row and ms["quoted"] else ms["early"]
        if m is None:
            continue
        x = (np.array([row.get(n, 0.0) for n in m.names]) - m.mean) / m.std
        pct = int(round(float(_prob(x[None, :], m.w, m.b)[0]) * 100))
        reasons = _reasons(m, row, owners)
        # A lead nobody has heard from in weeks is going cold, whatever the model says. The model cannot see this
        # (closed leads have no "waiting" time to learn from), so it is a plain rule on top, shown as a reason.
        quiet = (now - (last.get(l.id) or l.created_at)).days
        if quiet > QUIET_DAYS:
            pct = round(pct * max(0.25, QUIET_DAYS / quiet))
            reasons = [{"text": f"no contact for {quiet} days", "up": False}] + reasons[:2]
        res[l.id] = {"pct": max(1, min(99, pct)), "model": m.kind, "reasons": reasons}
    return res


def card(db: Session, tenant_id: int) -> dict:
    """What staff see under "How win chance works": what it learned from and how well it did on unseen leads."""
    ms = _models(db, tenant_id)
    owners = {u.id: u.name for u in db.scalars(select(User).where(User.tenant_id == tenant_id))}

    def one(m: Optional[Model]):
        if m is None:
            return None
        return {"trained_on": m.trained_on, "won": m.won, "check": m.check,
                "top": [{"text": _text(t["fact"], 1, owners), "up": t["weight"] > 0} for t in m.top]}
    return {"ready": bool(ms["early"] or ms["quoted"]), "closed": ms["closed"], "won": ms["won"], "demo": ms["demo"],
            "min_closed": MIN_CLOSED, "trained_at": ms["trained_at"],
            "early": one(ms["early"]), "quoted": one(ms["quoted"])}
