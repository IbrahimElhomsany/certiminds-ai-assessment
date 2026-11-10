"""Readable per-company document numbers: L-0001 (lead), Q-0001 (quote), O-0001 (order)."""
from sqlalchemy import select
from sqlalchemy.orm import Session


def next_number(db: Session, model, tenant_id: int, prefix: str) -> str:
    """Highest existing numeric number + 1. Ignores any number that isn't PREFIX-digits."""
    nums = db.scalars(select(model.number).where(model.tenant_id == tenant_id, model.number.like(f"{prefix}-%")))
    n = max((int(x[len(prefix) + 1:]) for x in nums if x[len(prefix) + 1:].isdigit()), default=0) + 1
    return f"{prefix}-{n:04d}"
