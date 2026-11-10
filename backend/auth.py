"""Staff login, roles and permissions for Nova Studio (/admin).

- Passwords are stored as bcrypt hashes, never as text.
- A successful login returns a signed token (JWT) holding the user id, company id and role.
  Every admin request sends it back; a changed or expired token is rejected.
- What a role may do is checked HERE, in the backend. Hiding a button in the screen is only
  for convenience; it is not security.
"""
import os
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.db.database import get_db, local_now
from backend.db.models import AuditLog, Tenant, User
from backend.observability import log_event

JWT_ALGORITHM = "HS256"
TOKEN_HOURS = 8                       # one working day

_secret = os.getenv("JWT_SECRET")
if not _secret:
    # Dev fallback: a random secret per run, so tokens simply stop working after a restart.
    _secret = secrets.token_urlsafe(32)
    log_event("jwt_secret_missing", note="JWT_SECRET not set; using a temporary secret")
JWT_SECRET = _secret

# ------------------------------------------------------------------ roles -> permissions
_SALES = {"dashboard.view", "leads.view_own", "leads.edit", "quotes.view_own", "quotes.edit",
          "orders.view_own", "orders.create", "inventory.view", "products.view"}
PERMISSIONS: dict[str, set[str]] = {
    "salesperson": _SALES,
    "sales_manager": _SALES | {"leads.view_all", "leads.assign", "quotes.view_all", "orders.view_all",
                               "discounts.approve", "reports.view", "audit.view"},
    "warehouse": {"inventory.view", "inventory.edit", "products.view", "orders.view_all", "orders.deliver"},
    "viewer": {"dashboard.view", "reports.view", "inventory.view", "products.view"},
}
PERMISSIONS["owner"] = set().union(*PERMISSIONS.values()) | {
    "users.manage", "settings.manage", "products.edit", "prices.edit"}


def has_permission(user: User, permission: str) -> bool:
    return permission in PERMISSIONS.get(user.role, set())


# ------------------------------------------------------------------ passwords
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=int(os.getenv("BCRYPT_ROUNDS", "12")))).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


# ------------------------------------------------------------------ tokens
def create_token(user: User, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    payload = {"sub": str(user.id), "tid": user.tenant_id, "role": user.role,
               "iat": now, "exp": now + timedelta(hours=TOKEN_HOURS)}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])   # checks signature AND expiry


# ------------------------------------------------------------------ audit
def audit(db: Session, tenant_id: int, action: str, user_id: int | None = None, entity: str | None = None,
          entity_id: str | int | None = None, before: dict | None = None, after: dict | None = None,
          at: datetime | None = None) -> None:
    db.add(AuditLog(tenant_id=tenant_id, user_id=user_id, action=action, entity=entity,
                    entity_id=None if entity_id is None else str(entity_id), before=before, after=after,
                    created_at=at or local_now()))


# ------------------------------------------------------------------ login
def authenticate(db: Session, tenant_slug: str, email: str, password: str) -> User | None:
    tenant = db.scalar(select(Tenant).where(Tenant.slug == tenant_slug))
    if tenant is None:
        verify_password(password, _DUMMY_HASH)          # same work either way: no timing hint
        return None
    user = db.scalar(select(User).where(User.tenant_id == tenant.id, User.email == email.strip().lower()))
    if user is None or not user.active:
        verify_password(password, _DUMMY_HASH)
        audit(db, tenant.id, "login.failed", after={"email": email.strip().lower()})
        db.commit()
        return None
    if not verify_password(password, user.password_hash):
        audit(db, tenant.id, "login.failed", user_id=user.id)
        db.commit()
        return None
    audit(db, tenant.id, "login", user_id=user.id)
    db.commit()
    return user


_DUMMY_HASH = bcrypt.hashpw(b"not-a-real-password", bcrypt.gensalt(rounds=4)).decode()


# ------------------------------------------------------------------ FastAPI dependencies
def current_user(authorization: str = Header(default=""), db: Session = Depends(get_db)) -> User:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Please log in.")
    try:
        claims = decode_token(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Your session has expired. Please log in again.")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid login token.")
    user = db.get(User, int(claims["sub"]))
    # The token must still match the database: a deactivated user or changed role loses access at once.
    if user is None or not user.active or user.tenant_id != claims["tid"] or user.role != claims["role"]:
        raise HTTPException(status_code=401, detail="Invalid login token.")
    return user


def require(permission: str):
    """Use as a dependency: Depends(require('users.manage'))."""
    def check(user: User = Depends(current_user)) -> User:
        if not has_permission(user, permission):
            raise HTTPException(status_code=403, detail="Your role does not allow this.")
        return user
    return check
