"""Fast, deterministic safety-flag detection on each caller turn.

Runs alongside the LLM so the agent reacts immediately to emergencies and
already-represented callers without waiting on a model round-trip. Bilingual.
"""

from __future__ import annotations

# Phrases that indicate a medical/safety emergency (EN + ES).
_EMERGENCY = [
    "911", "can't breathe", "cant breathe", "not breathing", "chest pain",
    "bleeding badly", "heavy bleeding", "unconscious", "passed out", "heart attack",
    "stroke", "suicidal", "kill myself", "dying", "emergency",
    "no puedo respirar", "no respira", "sangrando mucho", "inconsciente",
    "ataque al corazón", "ataque al corazon", "emergencia", "me estoy muriendo",
]

# Phrases indicating the caller already has legal representation (EN + ES).
_REPRESENTED = [
    "i have a lawyer", "i have an attorney", "already have a lawyer",
    "already have an attorney", "already represented", "my lawyer", "my attorney",
    "tengo un abogado", "ya tengo abogado", "ya tengo un abogado", "mi abogado",
]


def _contains_any(text: str, phrases: list[str]) -> bool:
    low = text.lower()
    return any(p in low for p in phrases)


def detect_emergency(text: str) -> bool:
    return _contains_any(text, _EMERGENCY)


def detect_already_represented(text: str) -> bool:
    return _contains_any(text, _REPRESENTED)


def detect_language_choice(text: str) -> str:
    """Map a language-prompt reply to 'es' or 'en' (default)."""
    low = text.lower()
    if "español" in low or "espanol" in low or "spanish" in low:
        return "es"
    return "en"
