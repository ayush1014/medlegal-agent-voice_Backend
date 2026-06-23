"""DB-free conversation harness for evaluating the intake agent's *behavior*.

This drives the SAME system prompt the live LiveKit worker uses
(``render_system_prompt``) against a real chat model — by default the worker's
own model (gpt-4o-mini) — with NO database, NO LiveKit, and NO tools, so the
"to-and-fro" evals need only an LLM key. We seed the scripted greeting as the
first assistant turn (the worker says it before the model ever runs) so the model
knows it has already greeted, then play scripted caller turns and collect every
agent reply for assertions.

Two kinds of checks build on this:
  * measurable helpers (sentence/word/question counts, money detection,
    letter-by-letter spell-back detection, phrase counts) — deterministic, cheap;
  * ``judge`` — an independent LLM grader (DeepSeek "thinking" model by default,
    so it isn't the model under test) that answers a yes/no question about a
    transcript as strict JSON.

Not collected by pytest (no ``test_`` prefix); imported by the eval test module.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agent.prompt import GREETING, render_system_prompt
from app.config import settings

# langchain-core + pydantic 2.11 leave ChatOpenAI's forward refs (BaseCache,
# Callbacks) unresolved, so a direct ``ChatOpenAI(...)`` fails to validate until
# the model is rebuilt. app/agent/llm.py does this for its own path; replicate it
# here so the harness is self-contained (the OpenAI branch doesn't import llm.py).
try:  # pragma: no cover - one-time import-time fix
    from langchain_core.caches import BaseCache  # noqa: F401
    from langchain_core.callbacks import Callbacks  # noqa: F401
    from langchain_openai import ChatOpenAI as _ChatOpenAI

    _ChatOpenAI.model_rebuild()
except Exception:  # noqa: BLE001
    pass

# --- Provider gate ----------------------------------------------------------
# The evals can run on either provider; skip cleanly when neither key is set.
HAVE_OPENAI = bool(settings.openai_api_key)
HAVE_DEEPSEEK = bool(settings.deepseek_api_key)
HAVE_LLM = HAVE_OPENAI or HAVE_DEEPSEEK


def build_agent_model(provider: str | None = None, temperature: float = 0.3) -> BaseChatModel:
    """Chat model standing in for the agent under test.

    Defaults to the worker's production model (``voice_llm_model`` = gpt-4o-mini)
    so the evals exercise exactly what produced the real-call transcript. Falls
    back to DeepSeek's realtime model when only that key is present. Temperature
    is held a touch below the worker's 0.4 for eval stability.
    """
    provider = provider or ("openai" if HAVE_OPENAI else "deepseek")
    if provider == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=settings.voice_llm_model,
            api_key=settings.openai_api_key,
            temperature=temperature,
        )
    from app.agent.llm import build_chat_model

    return build_chat_model(model=settings.deepseek_realtime_model, temperature=temperature)


@dataclass
class ConversationRunner:
    """Plays a scripted caller against the live system prompt, no DB/tools."""

    model: BaseChatModel
    firm: str = "medLegal"
    seed_greeting: bool = True
    _messages: list = field(default_factory=list)
    agent_turns: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._messages.append(SystemMessage(render_system_prompt(self.firm, "en")))
        if self.seed_greeting:
            # The worker speaks this scripted line before the model runs; the model
            # sees it as its own prior turn (so it doesn't re-greet / re-disclose).
            self._messages.append(AIMessage(GREETING["en"].format(firm=self.firm)))

    async def say(self, caller_text: str) -> str:
        """Feed one caller turn; return the agent's reply text."""
        self._messages.append(HumanMessage(caller_text))
        reply = await self.model.ainvoke(self._messages)
        text = (reply.content or "").strip() if isinstance(reply.content, str) else str(reply.content)
        self._messages.append(AIMessage(text))
        self.agent_turns.append(text)
        return text

    def transcript(self) -> str:
        """Render the call as Agent:/Caller: lines (skip the system prompt)."""
        lines = []
        for m in self._messages:
            if isinstance(m, AIMessage):
                lines.append(f"Agent: {m.content}")
            elif isinstance(m, HumanMessage):
                lines.append(f"Caller: {m.content}")
        return "\n".join(lines)


async def run_script(caller_turns: list[str], *, provider: str | None = None,
                     temperature: float = 0.3) -> ConversationRunner:
    """Convenience: build a runner and play every caller turn in order."""
    runner = ConversationRunner(model=build_agent_model(provider, temperature))
    for turn in caller_turns:
        await runner.say(turn)
    return runner


# --- Measurable helpers (deterministic; no model) ---------------------------

def sentences(text: str) -> list[str]:
    """Sentence-ish segments — split on . ! ? (and newlines), keep ones with a letter."""
    parts = re.split(r"[.!?]+|\n+", text or "")
    return [p.strip() for p in parts if re.search(r"[a-zA-Z]", p)]


def sentence_count(text: str) -> int:
    return len(sentences(text))


def word_count(text: str) -> int:
    return len(re.findall(r"\S+", text or ""))


def question_count(text: str) -> int:
    return (text or "").count("?")


def count_phrase(text: str, phrase: str) -> int:
    """Case-insensitive count of a phrase (overlaps not counted)."""
    return (text or "").lower().count(phrase.lower())


_MONEY = re.compile(
    r"\$\s?\d"                                              # $200, $ 5
    r"|\b\d[\d,]*(?:\.\d+)?\s*(?:dollars|bucks|grand|k\b|thousand|million)\b",
    re.IGNORECASE,
)


def contains_money(text: str) -> bool:
    """True if the text states a money figure (a payout/value, not a policy #)."""
    return bool(_MONEY.search(text or ""))


def max_letter_run(text: str) -> int:
    """Longest run of single-letter tokens — how a spelled-out string looks after
    speech-to-text ('y-u-v-r-a-j' / 'a y u s h'). High = the agent is reciting a
    full letter-by-letter spell-back."""
    tokens = re.findall(r"[A-Za-z]+", text or "")
    best = run = 0
    for t in tokens:
        run = run + 1 if len(t) == 1 else 0
        best = max(best, run)
    return best


def is_full_spellback(text: str, threshold: int = 6) -> bool:
    """The agent recited a long letter-by-letter spell-back (the email death-loop)."""
    return max_letter_run(text) >= threshold


# --- LLM-as-judge (independent grader) --------------------------------------

@dataclass
class Verdict:
    passed: bool
    reason: str


_JUDGE_SYSTEM = (
    "You grade a transcript of a phone call between an AI legal-intake agent and a caller. "
    "You will be asked ONE yes/no question about the AGENT's behavior. "
    "Answer ONLY with a JSON object: {\"pass\": true|false, \"reason\": \"<one sentence>\"}. "
    "pass=true means the agent's behavior satisfies the question. Be strict and literal."
)


def build_judge_model() -> BaseChatModel:
    """Independent grader. Prefer DeepSeek's thinking model so the judge is NOT the
    same model under test; fall back to OpenAI when only that key is present."""
    if HAVE_DEEPSEEK:
        from app.agent.llm import build_chat_model

        return build_chat_model(model=settings.deepseek_model, temperature=0)
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(model="gpt-4o", api_key=settings.openai_api_key, temperature=0)


async def judge(transcript: str, question: str, model: BaseChatModel | None = None) -> Verdict:
    """Ask an independent model a yes/no question about the transcript."""
    model = model or build_judge_model()
    prompt = (
        f"TRANSCRIPT:\n{transcript}\n\n"
        f"QUESTION (answer about the AGENT): {question}\n\n"
        'Respond with JSON only, e.g. {"pass": true, "reason": "..."}.'
    )
    resp = await model.ainvoke([SystemMessage(_JUDGE_SYSTEM), HumanMessage(prompt)])
    raw = resp.content if isinstance(resp.content, str) else str(resp.content)
    text = raw.strip()
    # Tolerate ```json fences / stray prose around the object.
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise AssertionError(f"Judge did not return JSON. Raw: {raw!r}")
    data = json.loads(m.group(0))
    return Verdict(passed=bool(data.get("pass")), reason=str(data.get("reason", "")))
