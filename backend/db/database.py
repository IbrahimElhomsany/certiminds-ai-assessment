"""Database connection for Nova Studio.

One database holds every company (tenant). SQLite for the demo; set DATABASE_URL to a
PostgreSQL URL in production and nothing else changes (SQLAlchemy hides the difference).
"""
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_URL = f"sqlite:///{ROOT / 'data' / 'nova_studio.db'}"


# Timestamps are stored as the company's local time (Qatar), without a timezone, so the admin
# shows them as staff expect. A multi-country deployment would store UTC plus a tenant timezone.
LOCAL_TZ = ZoneInfo(os.getenv("APP_TIMEZONE", "Asia/Qatar"))


def local_now() -> datetime:
    return datetime.now(LOCAL_TZ).replace(tzinfo=None, microsecond=0)


class Base(DeclarativeBase):
    pass


def make_engine(url: str | None = None):
    url = url or os.getenv("DATABASE_URL", DEFAULT_URL)
    if url in ("sqlite://", "sqlite:///:memory:"):
        # In-memory database (tests): one shared connection, otherwise every session sees an empty DB.
        return create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    if url.startswith("sqlite"):
        return create_engine(url, connect_args={"check_same_thread": False})
    return create_engine(url, pool_pre_ping=True)


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db(bind=None) -> None:
    """Create any missing tables. (A real deployment would use Alembic migrations.)"""
    from backend.db import models  # noqa: F401  - registers the tables on Base
    Base.metadata.create_all(bind=bind or engine)


def get_db():
    """FastAPI dependency: one session per request, always closed."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
