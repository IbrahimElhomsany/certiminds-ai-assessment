"""D2: the admin AI learns from staff.

Staff rate each "Ask your data" answer and each follow-up draft with 👍 or 👎 (and say why on 👎). When a draft is
sent, what they actually sent is kept too: that is the team's own wording. Nothing here retrains Claude; the
learning is ours and stays inside each company:

1. Similar questions. A new question is compared with the questions staff rated before (TF-IDF word vectors,
   cosine similarity, a small classic text model fitted on the company's own questions each time).
2. Ask your data with Claude: the closest liked answers go into its instructions as "what staff found helpful
   for questions like this" (which figures it looked up and how it answered), the closest disliked ones as
   "staff said this was not helpful because ...". Figures are always looked up again and checked.
3. Ask your data offline (no AI key): the lookup that staff liked for the most similar question is used instead
   of the keyword rules, and a lookup staff disliked for a near-identical question is skipped.
4. Follow-up drafts: the last messages staff really sent on that channel are given to Claude as style examples
   (never their facts), with the reasons staff gave for disliked drafts.
5. Liked questions become the suggested questions at the top of the page.

Everything is per company (tenant). A salesperson's ratings help the whole team, but only questions are shared,
never another person's figures: the answer to every question is looked up again for the person asking.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from datetime import timedelta
from difflib import SequenceMatcher
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.db.database import local_now
from backend.db.models import AiFeedback, User

REASONS = {"wrong_figures": "Wrong or missing figures", "not_asked": "Didn't answer my question",
           "too_long": "Too long", "unclear": "Hard to understand", "tone": "Wrong tone", "other": "Something else"}
STOP = set("""a an the and or of to in on for at by with from is are was were be been do does did have has had i we
you our my me us it its this that these those what which who whom how why when where much many most more
than then there their them they please can could would should will show tell give list me make bring get see
day days week weeks month months quarter year years last past next today yesterday so far right now""".split())
# periods are left out on purpose: "profit this month" and "profit in the last 90 days" are the same question
SIMILAR = 0.35          # cosine above this counts as "a question like this"
SAME = 0.6              # above this it's the same question in other words


def words(text: str) -> list[str]:
    out = []
    for w in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if w in STOP or len(w) < 2 or w.isdigit():
            continue
        for end in ("ing", "ers", "ed", "es", "s"):         # a tiny stemmer: sells/selling/sold stay close enough
            if len(w) > len(end) + 3 and w.endswith(end):
                w = w[: -len(end)]
                break
        out.append(w)
    return out


def similar(question: str, rows: list[AiFeedback]) -> list[tuple[float, AiFeedback]]:
    """Rated rows ranked by how close their question is to this one (TF-IDF cosine), best first."""
    docs = [words(r.question) for r in rows]
    q = words(question)
    if not q or not docs:
        return []
    n = len(docs) + 1
    df = Counter(w for d in docs + [q] for w in set(d))
    idf = {w: math.log((n + 1) / (c + 1)) + 1 for w, c in df.items()}

    def vec(d):
        tf = Counter(d)
        v = {w: (c / len(d)) * idf[w] for w, c in tf.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1
        return {w: x / norm for w, x in v.items()}

    qv = vec(q)
    scored = [(sum(qv.get(w, 0) * x for w, x in vec(d).items()), r) for d, r in zip(docs, rows) if d]
    return sorted((s for s in scored if s[0] > 0), key=lambda s: -s[0])


def rated(db: Session, tenant_id: int, kind: str, limit: int = 400) -> list[AiFeedback]:
    return list(db.scalars(select(AiFeedback).where(AiFeedback.tenant_id == tenant_id, AiFeedback.kind == kind,
                                                    AiFeedback.rating != 0)
                           .order_by(AiFeedback.id.desc()).limit(limit)))


# ------------------------------------------------------------------ Ask your data
def ask_hints(db: Session, tenant_id: int, question: str) -> dict:
    """What earlier ratings say about this question: liked/disliked neighbours and a learned lookup."""
    near = [(s, r) for s, r in similar(question, rated(db, tenant_id, "ask")) if s >= SIMILAR]
    liked = [(s, r) for s, r in near if r.rating > 0 and r.tools][:3]
    disliked = [(s, r) for s, r in near if r.rating < 0][:3]
    route = None
    if liked and liked[0][0] >= SAME * 0.85:
        t = liked[0][1].tools[0]
        route = {"tool": t["tool"], "input": dict(t.get("input") or {}), "from": liked[0][1].question,
                 "similarity": round(liked[0][0], 2)}
    avoid = {r.tools[0]["tool"] for s, r in disliked if s >= SAME and r.tools and r.reason in ("not_asked", "wrong_figures")}
    return {"liked": liked, "disliked": disliked, "route": route, "avoid": avoid}


def prompt_block(h: dict) -> str:
    """Extra instructions for Claude built from the team's ratings ('' when there is nothing to learn from yet)."""
    parts = []
    if h["liked"]:
        parts.append("Staff marked these answers to similar questions as HELPFUL. Look the figures up again (they are "
                     "out of date), but follow the same approach and style:")
        for _, r in h["liked"]:
            lookups = "; ".join(f"{t['tool']}({', '.join(f'{k}={v}' for k, v in (t.get('input') or {}).items())})"
                                for t in r.tools or [])
            parts.append(f'- Question: "{r.question}"\n  Lookups used: {lookups or "none"}\n  Answer: {(r.answer or "")[:400]}')
    if h["disliked"]:
        parts.append("Staff marked these answers to similar questions as NOT helpful. Avoid the same mistake:")
        for _, r in h["disliked"]:
            why = REASONS.get(r.reason or "", "no reason given") + (f" ({r.comment})" if r.comment else "")
            parts.append(f'- Question: "{r.question}"\n  Why it was not helpful: {why}\n  Answer: {(r.answer or "")[:300]}')
    return "\n".join(parts)


def learned_note(h: dict) -> Optional[str]:
    n_up, n_down = len(h["liked"]), len(h["disliked"])
    if not (n_up or n_down):
        return None
    bits = []
    if n_up:
        bits.append(f"{n_up} similar answer{'s' if n_up > 1 else ''} your team found helpful")
    if n_down:
        bits.append(f"{n_down} your team marked not helpful")
    return "Learned from " + " and ".join(bits) + "."


def popular_questions(db: Session, tenant_id: int, n: int = 3) -> list[str]:
    """Questions staff liked most (same question in other words counted together), for the suggestion chips."""
    rows = [r for r in rated(db, tenant_id, "ask") if r.rating > 0]
    groups: list[list[AiFeedback]] = []
    for r in rows:
        for g in groups:
            if similar(r.question, [g[0]]) and similar(r.question, [g[0]])[0][0] >= SAME:
                g.append(r)
                break
        else:
            groups.append([r])
    groups.sort(key=lambda g: -len(g))
    return [g[-1].question for g in groups[:n]]


# ------------------------------------------------------------------ follow-up drafts
def draft_examples(db: Session, tenant_id: int, channel: str) -> dict:
    sent = list(db.scalars(select(AiFeedback).where(AiFeedback.tenant_id == tenant_id, AiFeedback.kind == "draft",
                                                    AiFeedback.channel == channel, AiFeedback.sent_text.is_not(None),
                                                    AiFeedback.rating >= 0)
                           .order_by(AiFeedback.id.desc()).limit(2)))
    down = [r for r in rated(db, tenant_id, "draft", 50) if r.rating < 0][:4]
    return {"sent": [r.sent_text for r in sent], "avoid": [REASONS.get(r.reason or "", "") + (f": {r.comment}" if r.comment else "")
                                                           for r in down if r.reason or r.comment]}


def draft_block(ex: dict) -> str:
    parts = []
    if ex["sent"]:
        parts.append("Messages this team actually sent before (match their length, tone and wording; NEVER copy their "
                     "names, numbers or facts):")
        parts += [f"---\n{t[:700]}\n---" for t in ex["sent"]]
    if ex["avoid"]:
        parts.append("The team disliked earlier drafts because: " + "; ".join(ex["avoid"]) + ". Avoid that.")
    return "\n".join(parts)


def sent(db: Session, row: AiFeedback, text: str) -> float:
    """Keep what was really sent; a draft sent almost unchanged counts as liked. Returns how much was kept (0..1)."""
    kept = SequenceMatcher(None, row.answer or "", text).ratio()
    row.sent_text = text
    if row.rating == 0 and kept >= 0.9:
        row.rating, row.reason, row.rated_at = 1, "sent_as_is", local_now()
    return kept


# ------------------------------------------------------------------ recording and the summary
def record(db: Session, user: User, kind: str, question: str, answer: Optional[str], mode: str,
           tools: Optional[list] = None, channel: Optional[str] = None) -> AiFeedback:
    row = AiFeedback(tenant_id=user.tenant_id, user_id=user.id, kind=kind, question=question[:500], answer=answer,
                     tools=tools, mode=mode, channel=channel, created_at=local_now())
    db.add(row)
    db.flush()
    return row


def rate(db: Session, user: User, row: AiFeedback, rating: int, reason: Optional[str], comment: Optional[str]) -> AiFeedback:
    row.rating = 1 if rating > 0 else -1
    row.reason = reason if row.rating < 0 and reason in REASONS else None
    row.comment = (comment or "").strip()[:300] or None
    row.rated_at = local_now()
    return row


def summary(db: Session, tenant_id: int) -> dict:
    now = local_now()
    week, two = now - timedelta(days=7), now - timedelta(days=14)

    def rate_between(kind, a, b):
        q = select(AiFeedback.rating, func.count()).where(AiFeedback.tenant_id == tenant_id, AiFeedback.kind == kind,
                                                          AiFeedback.rating != 0, AiFeedback.rated_at >= a,
                                                          AiFeedback.rated_at < b).group_by(AiFeedback.rating)
        c = dict(db.execute(q).all())
        n = c.get(1, 0) + c.get(-1, 0)
        return {"rated": n, "helpful_pct": round(100 * c.get(1, 0) / n) if n else None}

    def totals(kind):
        c = dict(db.execute(select(AiFeedback.rating, func.count()).where(AiFeedback.tenant_id == tenant_id,
                                                                          AiFeedback.kind == kind)
                            .group_by(AiFeedback.rating)).all())
        return {"helpful": c.get(1, 0), "not_helpful": c.get(-1, 0), "not_rated": c.get(0, 0)}

    reasons = Counter(r.reason for r in rated(db, tenant_id, "ask") + rated(db, tenant_id, "draft") if r.rating < 0)
    recent_down = [{"kind": r.kind, "question": r.question, "reason": REASONS.get(r.reason or "", ""), "comment": r.comment,
                    "when": r.rated_at.isoformat() if r.rated_at else None}
                   for r in sorted((r for r in rated(db, tenant_id, "ask", 100) + rated(db, tenant_id, "draft", 100)
                                    if r.rating < 0), key=lambda r: r.rated_at or now, reverse=True)[:5]]
    unanswered = [r.question for r in db.scalars(
        select(AiFeedback).where(AiFeedback.tenant_id == tenant_id, AiFeedback.kind == "ask", AiFeedback.answer.is_(None),
                                 AiFeedback.tools.is_(None)).order_by(AiFeedback.id.desc()).limit(5))]
    drafts_sent = list(db.scalars(select(AiFeedback).where(AiFeedback.tenant_id == tenant_id, AiFeedback.kind == "draft",
                                                           AiFeedback.sent_text.is_not(None))))
    as_is = sum(1 for r in drafts_sent if SequenceMatcher(None, r.answer or "", r.sent_text).ratio() >= 0.9)
    return {"ask": {**totals("ask"), "this_week": rate_between("ask", week, now), "last_week": rate_between("ask", two, week)},
            "draft": {**totals("draft"), "sent": len(drafts_sent), "sent_as_is": as_is,
                      "this_week": rate_between("draft", week, now), "last_week": rate_between("draft", two, week)},
            "favourite_questions": popular_questions(db, tenant_id, 5),
            "reasons": [{"reason": REASONS.get(k, k), "count": v} for k, v in reasons.most_common() if k],
            "recent_not_helpful": recent_down, "unanswered": unanswered}
