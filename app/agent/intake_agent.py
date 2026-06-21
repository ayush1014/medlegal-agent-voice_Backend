"""The text-driven intake session: a LangGraph ReAct agent (DeepSeek + tools)
wrapped in a turn-by-turn driver that persists transcript, thread, and events.

Scripted, compliance-critical lines (greeting + recording/AI disclosure + the
language prompt, and the emergency 911 advice) are deterministic; the rest of the
intake is LLM-driven. The same driver powers both tests (inject a fake model)
and the live LiveKit worker (Phase D).
"""

from __future__ import annotations

import uuid

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent

from app.agent.context import IntakeContext
from app.agent.llm import build_chat_model
from app.config import settings
from app.agent.prompt import EMERGENCY_REPLY, GREETING, render_system_prompt
from app.agent.safety import (
    detect_already_represented,
    detect_emergency,
    detect_language_choice,
)
from app.agent.tools import build_tools
from app.database import session_scope
from app.security.context import system_context
from app.services import intake_service


class IntakeSession:
    def __init__(self, ctx: IntakeContext, model: BaseChatModel | None = None):
        self.ctx = ctx
        # Non-thinking model for low realtime turn latency (extraction uses the
        # thinking model post-call where latency doesn't matter).
        self._model = model or build_chat_model(model=settings.deepseek_realtime_model)
        self._graph = None
        self._system_sent = False
        self._stage = "language"  # language -> intake -> done
        self._lines: list[str] = []
        self._thread_cfg = {
            "configurable": {"thread_id": str(ctx.voice_call_id or uuid.uuid4())}
        }

    def _build_graph(self):
        return create_react_agent(
            self._model, build_tools(self.ctx), checkpointer=MemorySaver()
        )

    async def start(self) -> str:
        """Create DB records and return the scripted greeting + disclosure + language prompt."""
        async with session_scope(system_context(self.ctx.organization_id)) as db:
            await intake_service.create_session_records(db, self.ctx)
        greeting = GREETING["en"].format(firm=self.ctx.firm_name)
        await self._persist("agent", greeting)
        return greeting

    async def respond(self, user_text: str) -> str:
        await self._persist("caller", user_text)

        # --- Live safety: react immediately, before any model round-trip ---
        if detect_emergency(user_text):
            self.ctx.emergency = True
            self.ctx.ended = True
            self.ctx.end_reason = "emergency"
            async with session_scope(system_context(self.ctx.organization_id)) as db:
                await intake_service.log_event(db, self.ctx, event_type="decision", name="emergency_detected")
            reply = EMERGENCY_REPLY[self.ctx.language]
            await self._persist("agent", reply)
            return reply

        if detect_already_represented(user_text) and not self.ctx.already_represented:
            self.ctx.already_represented = True
            async with session_scope(system_context(self.ctx.organization_id)) as db:
                await intake_service.log_event(db, self.ctx, event_type="decision", name="already_represented")

        # --- Language selection turn (deterministic) ---
        if self._stage == "language":
            self.ctx.language = detect_language_choice(user_text)
            async with session_scope(system_context(self.ctx.organization_id)) as db:
                await intake_service.set_language(db, self.ctx)
            self._graph = self._build_graph()
            self._stage = "intake"

        # --- LLM-driven intake turn ---
        messages: list = []
        if not self._system_sent:
            messages.append(SystemMessage(render_system_prompt(self.ctx.firm_name, self.ctx.language)))
            self._system_sent = True
        messages.append(HumanMessage(user_text))

        result = await self._graph.ainvoke({"messages": messages}, self._thread_cfg)
        reply = result["messages"][-1].content or ""
        await self._persist("agent", reply)
        return reply

    async def finalize(self, status: str = "complete") -> None:
        async with session_scope(system_context(self.ctx.organization_id)) as db:
            await intake_service.finalize_transcript(
                db, self.ctx, status=status, full_text="\n".join(self._lines)
            )

    async def _persist(self, speaker: str, content: str) -> None:
        self._lines.append(f"{'Agent' if speaker == 'agent' else 'Caller'}: {content}")
        async with session_scope(system_context(self.ctx.organization_id)) as db:
            await intake_service.add_segment(db, self.ctx, speaker, content)
