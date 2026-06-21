"""Short code -> (org, lead, purpose) for clickable SMS/WhatsApp links.

Owner-connection only (pre-auth resolution + funnel creation); codes are
unguessable secrets, so no RLS policy is needed.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ShortLink(Base):
    __tablename__ = "short_links"

    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False
    )
    purpose: Mapped[str] = mapped_column(String(24), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
