"""Readable per-company document numbers: L-0001 (lead), Q-0001 (quote), O-0001 (order)."""
from sqlalchemy import func, select
from sqlalchemy.orm import Session


def next_number(db: Session, model, tenant_id: int, prefix: str) -> str:
    last = db.scalar(select(func.max(model.number)).where(model.tenant_id == tenant_id,
                                                          model.number.like(f"{prefix}-%")))
    n = int(last.split("-")[1]) + 1 if last else 1
    return f"{prefix}-{n:04d}"
