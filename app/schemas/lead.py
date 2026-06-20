"""Lead read models for the dashboard. camelCase out to match the frontend."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class LeadOut(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str
    full_name: str
    phone: str
    email: str | None = None
    case_type: str
    incident_date: date | None = None
    qualification_status: str
    lead_score: int
    lead_temperature: str
    settlement_expected: Decimal | None = None
    pipeline_status: str
    retainer_status: str
    missing_documents: int
    ai_summary: str | None = None
    updated_at: datetime
