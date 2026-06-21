"""Intake agent tools. Each closes over the call's IntakeContext, runs under the
firm's system context, and logs to agent_events (the debugging trail)."""

from __future__ import annotations

from langchain_core.tools import BaseTool, tool

from app.agent.context import IntakeContext
from app.database import session_scope
from app.models.enums import CASE_TYPES
from app.security.context import system_context
from app.services import intake_service


def build_tools(ctx: IntakeContext) -> list[BaseTool]:
    @tool
    async def save_partial_lead(
        full_name: str | None = None,
        case_type: str | None = None,
        email: str | None = None,
        summary: str | None = None,
    ) -> str:
        """Persist known caller facts so nothing is lost if the call drops. Call this
        as you learn the caller's name, the kind of accident/case, their email, or a
        one-line summary of what happened. case_type should be one of the firm's
        supported categories."""
        if case_type and case_type not in CASE_TYPES:
            case_type = "Other Personal Injury"
        async with session_scope(system_context(ctx.organization_id)) as db:
            await intake_service.update_partial_lead(
                db, ctx,
                {"full_name": full_name, "case_type": case_type, "email": email, "ai_summary": summary},
            )
            await intake_service.log_event(
                db, ctx, event_type="tool_call", name="save_partial_lead",
                payload={"set": [k for k, v in
                                 {"full_name": full_name, "case_type": case_type,
                                  "email": email, "summary": summary}.items() if v]},
            )
        return "saved"

    @tool
    async def lookup_lead_by_phone() -> str:
        """Check whether this caller already has a case on file with the firm."""
        async with session_scope(system_context(ctx.organization_id)) as db:
            prior = await intake_service.lookup_prior_lead(db, ctx)
            await intake_service.log_event(
                db, ctx, event_type="tool_call", name="lookup_lead_by_phone",
                payload={"found": prior is not None},
            )
        if not prior:
            return "No prior case on file."
        return f"Prior case found: {prior['full_name']} — {prior['case_type']}."

    @tool
    async def flag_emergency() -> str:
        """Flag that the caller is describing a medical emergency. After calling this,
        advise them to hang up and dial 911, then end the call."""
        ctx.emergency = True
        async with session_scope(system_context(ctx.organization_id)) as db:
            await intake_service.log_event(db, ctx, event_type="decision", name="flag_emergency")
        return "Emergency flagged."

    @tool
    async def end_call(reason: str = "complete") -> str:
        """End the call after a brief wrap-up, or for emergencies / wrong numbers /
        clearly non-PI calls. reason is a short label (complete|emergency|non_pi|wrong_number|represented)."""
        ctx.ended = True
        ctx.end_reason = reason
        async with session_scope(system_context(ctx.organization_id)) as db:
            await intake_service.log_event(
                db, ctx, event_type="decision", name="end_call", payload={"reason": reason}
            )
        return "Call ended."

    return [save_partial_lead, lookup_lead_by_phone, flag_emergency, end_call]
