"""Per-call intake context shared between the session and its tools."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field


@dataclass
class IntakeContext:
    organization_id: uuid.UUID
    caller_phone: str
    voice_call_id: uuid.UUID | None = None
    lead_id: uuid.UUID | None = None
    transcript_id: uuid.UUID | None = None
    agent_thread_id: uuid.UUID | None = None
    firm_name: str = "medLegal"
    language: str = "en"  # "en" | "es"

    # Returning caller (same phone): reuse their profile + greet by name.
    returning: bool = False
    known_name: str | None = None

    # Live flags the tools/detector raise during the call.
    emergency: bool = False
    already_represented: bool = False
    ended: bool = False
    end_reason: str | None = None

    # Monotonic transcript ordering.
    seq: int = field(default=0)

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq
