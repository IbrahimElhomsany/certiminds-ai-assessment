"""Running Nova Studio as a public demo (Render).

Two safeguards for a site anyone with the link can open:
1. A daily cap on Claude calls (DAILY_AI_LIMIT, default 400). When it is reached, Claude calls fail fast and
   every feature falls back to its offline mode (workflow chat, figures-only answers, template drafts),
   so a busy day or a bored visitor can't run up the Anthropic bill.
2. Fresh demo data: AUTO_SEED=1 fills an empty database on start; DEMO_DAILY_RESET=1 re-creates the demo
   company once a day after 03:00 Qatar time, so testers can change anything without breaking it for good.
"""
import os
import threading
import time
from datetime import date

from sqlalchemy import select

from backend.db.database import SessionLocal, local_now
from backend.db.models import AuditLog, Tenant
from backend.observability import log_event


class AIBudgetExceeded(RuntimeError):
    pass


_lock = threading.Lock()
_usage = {"day": None, "calls": 0}


def ai_calls_today() -> int:
    with _lock:
        return _usage["calls"] if _usage["day"] == local_now().date() else 0


class BudgetedClient:
    """Wraps anthropic.Anthropic: same `client.messages.create(...)`, but counts calls per day."""

    def __init__(self, client, daily_limit: int | None = None):
        self._client = client
        self.limit = daily_limit if daily_limit is not None else int(os.getenv("DAILY_AI_LIMIT", "400"))
        self.messages = self

    def create(self, **kwargs):
        today = local_now().date()
        with _lock:
            if _usage["day"] != today:
                _usage["day"], _usage["calls"] = today, 0
            if _usage["calls"] >= self.limit:
                raise AIBudgetExceeded(f"daily AI limit of {self.limit} calls reached")
            _usage["calls"] += 1
        return self._client.messages.create(**kwargs)


# ------------------------------------------------------------------ demo data
def _last_seed_day() -> date | None:
    with SessionLocal() as db:
        if db.scalar(select(Tenant.id)) is None:
            return None
        at = db.scalar(select(AuditLog.created_at).where(AuditLog.action == "seed").order_by(AuditLog.created_at.desc()))
        return at.date() if at else date.min


def _reseed(reason: str) -> None:
    from backend import crm_bridge
    from backend.db.seed import reset_and_seed
    reset_and_seed()
    crm_bridge.refresh_catalog(force=True)
    log_event("demo_reseed", reason=reason)


def start_housekeeping() -> None:
    if os.getenv("AUTO_SEED") == "1" and _last_seed_day() is None:
        _reseed("empty database")
    if os.getenv("DEMO_DAILY_RESET") != "1":
        return

    def loop():
        while True:
            try:
                now = local_now()
                last = _last_seed_day()
                if now.hour >= 3 and (last is None or last < now.date()):
                    _reseed("daily reset")
            except Exception as e:                       # never take the site down over housekeeping
                log_event("demo_reseed_failed", error=type(e).__name__)
            time.sleep(900)

    threading.Thread(target=loop, name="demo-reset", daemon=True).start()
