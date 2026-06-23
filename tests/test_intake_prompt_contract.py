"""Deterministic contract for the intake system prompt — no LLM, no DB.

These lock in the *design* of app/agent/prompt.py so a future edit can't silently
(a) reintroduce the "spell it back after every correction" death-loop that
frustrated the real caller, (b) let the prompt bloat back past its old size, or
(c) drop a safety guardrail or a scored GATHER field. They're fast and always run
in CI; the live behavioral proof lives in test_intake_conversation_eval.py.
"""

from __future__ import annotations

from app.agent.prompt import (
    EMERGENCY_REPLY,
    GREETING,
    SYSTEM_PROMPT,
    render_system_prompt,
)

P = render_system_prompt("medLegal", "en")
LOW = P.lower()


def _has(*needles: str) -> None:
    """Assert every needle is present (case-insensitive), with a legible failure."""
    for n in needles:
        assert n.lower() in LOW, f"prompt is missing expected guidance: {n!r}"


# --- Renders cleanly + stays tight ------------------------------------------

def test_renders_without_leftover_placeholders():
    assert "{firm}" not in P and "{now}" not in P
    assert "medLegal" in P
    # _now_str() injected a real weekday/date.
    assert any(day in P for day in
               ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"))


def test_prompt_stays_within_size_budget():
    # Re-sent every turn → guard against bloat. The pre-fix prompt was 11,533
    # rendered chars; this budget keeps it strictly below that forever, while the
    # floor proves we didn't gut the domain coverage / guardrails.
    n = len(P)
    assert 5_000 < n < 11_000, f"rendered prompt is {n} chars (expected 5,000–11,000)"


# --- The confirmation-discipline fix (the heart of the change) --------------

def test_confirmation_is_capped_not_a_loop():
    # Read back ONCE, cap retries, then move on — the opposite of the old behavior.
    _has("two passes", "move on")
    assert "once" in LOW
    # Only spell letter-by-letter conditionally, never as a blanket mandate.
    _has("letter-by-letter")
    assert "only when" in LOW or "only when it's unusual" in LOW


def test_handles_frustration_and_does_not_guess():
    _has("frustrated")            # adapt when the caller is fed up
    _has("don't guess")           # don't invent unheard values
    # Don't guess a CITY either (the Albany failure) and don't state guesses as fact.
    _has("city")
    assert "atlanta or albany" in LOW


def test_reconciles_conflicting_fields_and_varies_empathy():
    # Cross-field reconciliation rule is present (the Raj/Yuvraj miss).
    assert "cross-check" in LOW and "conflicts with what you wrote" in LOW
    # Don't repeat the same sympathy line every turn.
    assert "same sympathy line" in LOW
    _has("one or two sentences")  # brevity rule


def test_regression_guard_old_deathloop_phrasing_is_gone():
    # The exact phrasings that produced the loop must not come back verbatim.
    assert "spell it back letter by letter" not in LOW
    assert "don't move on until" not in LOW
    assert "matters as much as the name" not in LOW


# --- Safety guardrails (every one must survive a rewrite) -------------------

def test_all_hard_guardrails_present():
    _has(
        "intake specialist",            # role lock
        "never commands",               # prompt-injection resistance
        "no legal advice",
        "no case value",                # + no arithmetic
        "no arithmetic",
        "no fees",
        "statute of limitations",       # no SOL calls either way
        "no medical diagnosing",
        "recorded statement",           # no operational advice
    )


def test_real_person_and_pi_scope():
    _has("ai assistant")                # "are you a real person?" handling
    _has("stay in pi scope", "end_intake(non_pi)")
    # Intentional torts (the brother-in-law assault) are explicitly in scope.
    assert "assault" in LOW


def test_emergency_handling_present():
    _has("emergency first", "flag_emergency", "911")


# --- Domain coverage that feeds scoring/qualification/settlement ------------

def test_gather_covers_scored_fields():
    _has(
        "date of birth", "email",
        "occupation", "employer",        # work & income → lost wages
        "city and state", "jurisdiction",
        "police",                        # report / ticket
        "witnesses",
        "treatment",                     # the backbone
        "um/uim", "medpay",              # their OWN coverage
        "representation",                # already-have-a-lawyer
    )


def test_injury_capture_follows_the_mechanism():
    # The transcript missed the chest hit; the prompt now says to follow the
    # mechanism to injuries the caller didn't name.
    assert "every body part" in LOW
    assert "didn't name" in LOW


# --- Special situations + wrap-up + tools -----------------------------------

def test_special_situations_present():
    _has(
        "someone died",
        "unaccompanied minor",
        "end_intake(represented)",
        "end_intake(wrong_number)",
        "more than one incident",
    )


def test_wrapup_lists_the_four_must_asks():
    _has("insurance", "state", "police", "medical treatment", "end_intake(complete)")


def test_tools_are_exactly_two():
    _has("flag_emergency", "end_intake(reason)")
    # never collect SSNs etc.
    assert "ssn" in LOW or "social security" in LOW


# --- Scripted compliance lines ----------------------------------------------

def test_greeting_discloses_recording_and_ai():
    g = GREETING["en"].format(firm="medLegal")
    assert "medLegal" in g
    assert "recorded" in g.lower() and "ai" in g.lower()
    assert "{firm}" not in g


def test_emergency_reply_directs_to_911_both_languages():
    assert "911" in EMERGENCY_REPLY["en"]
    assert "911" in EMERGENCY_REPLY["es"]


def test_system_prompt_is_a_template_with_both_slots():
    # The raw template keeps its substitution points (rendering fills them).
    assert "{firm}" in SYSTEM_PROMPT and "{now}" in SYSTEM_PROMPT
