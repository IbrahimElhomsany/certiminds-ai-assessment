"""Data shapes (Pydantic models) for API input and LLM output.

Pydantic checks every field. If the LLM returns something that doesn't fit these
shapes, validation fails and the gateway treats it as an INVALID_RESPONSE error,
so bad output never silently reaches the application (B2).
"""
from typing import Literal, Optional

from pydantic import BaseModel, Field


# ------------------------------------------------------------------ B1 request

class GenerateRequest(BaseModel):
    user_id: str = Field(pattern=r"^USR-\d{3,10}$", examples=["USR-1001"])
    task: Literal["recommendation", "extraction", "objection", "faq"]
    message: str = Field(min_length=1, max_length=2000)
    language: Literal["en", "ar"] = "en"


class ChatRequest(BaseModel):
    user_id: str = Field(pattern=r"^USR-\d{3,10}$", examples=["USR-1001"])
    message: str = Field(min_length=1, max_length=2000)
    session_id: Optional[str] = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{4,64}$")


# ------------------------------------------------------------------ B2 / E.6 structured output

class CustomerRequirements(BaseModel):
    """E.6 fields. Every field is optional (None) - the model must only fill what the
    customer actually said or what can be clearly inferred."""
    application: Optional[str] = None            # e.g. "balcony", "kitchen wall", "restaurant floor"
    indoor_outdoor: Optional[Literal["indoor", "outdoor"]] = None
    floor_or_wall: Optional[Literal["floor", "wall"]] = None
    area_m2: Optional[float] = Field(default=None, gt=0, le=100000)
    budget: Optional[float] = Field(default=None, ge=0)
    preferred_style: Optional[str] = None
    preferred_color: Optional[str] = None
    slip_requirement: Optional[Literal["standard", "high"]] = None
    traffic_level: Optional[Literal["low", "medium", "high"]] = None
    urgency: Optional[Literal["low", "medium", "high"]] = None


Intent = Literal["recommendation_request", "product_inquiry", "quotation_request", "objection",
                 "greeting", "off_topic", "unsafe_request", "other"]


class StructuredAnalysis(BaseModel):
    """The B2 schema (customer_requirements uses the richer E.6 field set)."""
    intent: Intent
    customer_requirements: CustomerRequirements
    missing_information: list[str]
    recommended_actions: list[str]
    confidence: float = Field(ge=0.0, le=1.0)
