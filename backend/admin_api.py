"""Nova Studio admin API (/api/admin/...). Staff only: every route needs a valid login token.

Two rules hold on every route:
1. Company isolation: queries always filter on the logged-in user's tenant_id.
2. Role permissions: checked here with require(...), never only in the screen.
"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.auth import (PERMISSIONS, authenticate, create_token, current_user, has_permission, require)
from backend.db.database import get_db
from backend.db.models import LEAD_STAGES, AuditLog, Lead, Tenant, User
from backend.ratelimit import RateLimiter

router = APIRouter(prefix="/api/admin", tags=["Nova Studio admin"])
login_limiter = RateLimiter(max_requests=10, window_seconds=60)   # slows down password guessing


class LoginRequest(BaseModel):
    company: str = Field(default="ceramica-nova", min_length=2, max_length=60)
    email: str = Field(min_length=3, max_length=120)
    password: str = Field(min_length=1, max_length=200)


class UserOut(BaseModel):
    id: int
    name: str
    email: str
    role: str
    branch: Optional[str]
    discount_limit_pct: float


def user_out(u: User) -> UserOut:
    return UserOut(id=u.id, name=u.name, email=u.email, role=u.role, branch=u.branch,
                   discount_limit_pct=float(u.discount_limit_pct))


@router.post("/login")
def login(req: LoginRequest, request: Request, db: Session = Depends(get_db)):
    client = request.client.host if request.client else "unknown"
    if not login_limiter.allow(f"{client}:{req.email.lower()}"):
        raise HTTPException(status_code=429, detail="Too many login attempts. Please wait a minute.")
    user = authenticate(db, req.company, req.email, req.password)
    if user is None:
        # Same message whether the email or the password is wrong: don't reveal which accounts exist.
        raise HTTPException(status_code=401, detail="Wrong email or password.")
    return {"token": create_token(user), "token_type": "bearer", "user": user_out(user)}


@router.get("/me")
def me(user: User = Depends(current_user), db: Session = Depends(get_db)):
    tenant = db.get(Tenant, user.tenant_id)
    return {"user": user_out(user),
            "company": {"name": tenant.name, "slug": tenant.slug, "currency": tenant.currency,
                        "is_demo": tenant.is_demo},
            "permissions": sorted(PERMISSIONS[user.role])}


@router.get("/users", response_model=list[UserOut])
def list_users(user: User = Depends(require("users.manage")), db: Session = Depends(get_db)):
    rows = db.scalars(select(User).where(User.tenant_id == user.tenant_id).order_by(User.id))
    return [user_out(u) for u in rows]


class LeadOut(BaseModel):
    id: int
    number: str
    customer: str
    owner: Optional[str]
    stage: str
    source: str
    room: Optional[str]
    area_m2: Optional[float]
    budget: Optional[float]
    summary: Optional[str]
    created_at: datetime


def visible_leads(user: User):
    """Base query for the leads this user may see: own company always; own leads only for salespeople."""
    q = select(Lead).where(Lead.tenant_id == user.tenant_id)
    if not has_permission(user, "leads.view_all"):
        q = q.where(Lead.owner_id == user.id)
    return q


@router.get("/leads", response_model=list[LeadOut])
def list_leads(stage: Optional[str] = None, limit: int = 50,
               user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not (has_permission(user, "leads.view_all") or has_permission(user, "leads.view_own")):
        raise HTTPException(status_code=403, detail="Your role does not allow this.")
    if stage is not None and stage not in LEAD_STAGES:
        raise HTTPException(status_code=422, detail=f"stage must be one of {', '.join(LEAD_STAGES)}")
    q = visible_leads(user)
    if stage:
        q = q.where(Lead.stage == stage)
    rows = db.scalars(q.order_by(Lead.created_at.desc()).limit(max(1, min(limit, 200))))
    return [LeadOut(id=l.id, number=l.number, customer=l.customer.name, owner=l.owner.name if l.owner else None,
                    stage=l.stage, source=l.source, room=l.room,
                    area_m2=float(l.area_m2) if l.area_m2 is not None else None,
                    budget=float(l.budget) if l.budget is not None else None,
                    summary=l.summary, created_at=l.created_at) for l in rows]


@router.get("/audit")
def audit_log(limit: int = 50, user: User = Depends(require("audit.view")), db: Session = Depends(get_db)):
    rows = db.scalars(select(AuditLog).where(AuditLog.tenant_id == user.tenant_id)
                      .order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(max(1, min(limit, 200))))
    names = {u.id: u.name for u in db.scalars(select(User).where(User.tenant_id == user.tenant_id))}
    return [{"at": a.created_at, "user": names.get(a.user_id), "action": a.action, "entity": a.entity,
             "entity_id": a.entity_id, "after": a.after} for a in rows]
