"""Shared declarative mixins for all ORM models.

- ``uuidv7()`` primary keys (native in Postgres 18): time-ordered, so they keep
  B-tree index locality while staying safe to expose (no enumeration).
- ``created_at`` / ``updated_at`` as UTC ``timestamptz``, defaulted in the DB.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column


class UUIDPrimaryKeyMixin:
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("uuidv7()"),
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class SoftDeleteMixin:
    """Marks a row deleted without removing it — nothing legal is silently lost.

    RLS hides soft-deleted rows from the app role; the owner can still restore.
    """

    deleted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
