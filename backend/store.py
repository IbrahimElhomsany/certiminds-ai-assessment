"""In-memory session and quote storage with ownership checks (authorization boundary).

Every session and quote belongs to one user_id. Reading someone else's data raises
AccessDenied - this check is in code, not in the prompt (E.15: "Show another customer's quote").
Production: PostgreSQL with row-level security on tenant_id/user_id, plus real authentication (JWT).
"""
from dataclasses import dataclass, field
from typing import Optional

from backend.schemas import CustomerRequirements
from backend.tools import Quote


class AccessDenied(Exception):
    pass


@dataclass
class Session:
    session_id: str
    user_id: str
    requirements: CustomerRequirements = field(default_factory=CustomerRequirements)
    history: list = field(default_factory=list)        # [{"role": "user"|"assistant", "content": str}]
    last_recommended: list = field(default_factory=list)
    quote_ids: list = field(default_factory=list)
    grounded_numbers: set = field(default_factory=set)  # amounts from earlier tool results in this conversation


SESSIONS: dict[str, Session] = {}
QUOTES: dict[str, Quote] = {}


def get_or_create_session(session_id: str, user_id: str) -> Session:
    s = SESSIONS.get(session_id)
    if s is None:
        s = SESSIONS[session_id] = Session(session_id, user_id)
    if s.user_id != user_id:
        raise AccessDenied("session belongs to another user")
    return s


def save_quote(q: Quote) -> None:
    QUOTES[q.quotation_id] = q


def get_quote(quotation_id: str, user_id: str) -> Optional[Quote]:
    q = QUOTES.get(quotation_id)
    if q is None:
        return None
    if q.customer_id != user_id:
        raise AccessDenied("quote belongs to another customer")
    return q


def merge_requirements(old: CustomerRequirements, new: CustomerRequirements) -> CustomerRequirements:
    """Multi-turn memory: new non-empty values overwrite, earlier answers are kept."""
    data = old.model_dump()
    data.update({k: v for k, v in new.model_dump().items() if v is not None})
    return CustomerRequirements(**data)
