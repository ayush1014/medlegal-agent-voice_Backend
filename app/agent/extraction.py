"""Post-call structured extraction (DeepSeek, JSON mode).

Reads a finished intake transcript and returns a validated structured record.
Lenient by design: every field is optional and dates/amounts stay strings/floats
here — server-side normalization happens in extraction_service before any DB write.
"""

from __future__ import annotations

from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field

from app.config import settings
from app.models.enums import (
    CASE_TYPES,
    DAMAGE_CATEGORIES,
    INJURY_SEVERITIES,
    PARTY_ROLES,
    POLICY_KINDS,
    POLICY_PARTY_ROLES,
)


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ExtractedLead(_Base):
    full_name: str | None = None
    date_of_birth: str | None = None  # YYYY-MM-DD; verifies returning callers
    email: str | None = None
    address: str | None = None
    occupation: str | None = None
    employer: str | None = None
    employment_status: str | None = None  # e.g. employed / self-employed / unemployed / retired / student
    annual_income: float | None = None
    case_type: str | None = None
    preferred_contact_method: str | None = None
    best_time_to_contact: str | None = None
    has_attorney: bool | None = None
    summary: str | None = None


class ExtractedIncident(_Base):
    incident_date: str | None = None
    location_text: str | None = None
    description: str | None = None
    police_report_available: bool | None = None
    fault_narrative: str | None = None
    comparative_negligence_pct: int | None = None


class ExtractedInjury(_Base):
    body_part: str | None = None
    description: str | None = None
    severity: str | None = None
    is_permanent: bool | None = None
    requires_surgery: bool | None = None


class ExtractedTreatment(_Base):
    provider_name: str | None = None
    provider_type: str | None = None
    treatment_type: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    is_ongoing: bool | None = None
    billed_amount: float | None = None


class ExtractedPolicy(_Base):
    party_role: str | None = None
    carrier_name: str | None = None
    policy_kind: str | None = None
    coverage_limit: float | None = None
    claim_number: str | None = None


class ExtractedParty(_Base):
    role: str | None = None
    full_name: str | None = None
    notes: str | None = None


class ExtractedDamage(_Base):
    category: str | None = None
    description: str | None = None
    amount: float | None = None
    is_estimated: bool | None = None


class Extraction(_Base):
    lead: ExtractedLead = Field(default_factory=ExtractedLead)
    incidents: list[ExtractedIncident] = Field(default_factory=list)
    injuries: list[ExtractedInjury] = Field(default_factory=list)
    treatments: list[ExtractedTreatment] = Field(default_factory=list)
    insurance_policies: list[ExtractedPolicy] = Field(default_factory=list)
    parties: list[ExtractedParty] = Field(default_factory=list)
    damages: list[ExtractedDamage] = Field(default_factory=list)


def _prompt() -> str:
    return (
        "You are an expert personal-injury intake analyst. Extract EVERY fact the caller "
        "stated from the intake transcript into a single JSON object. Be thorough and "
        "exhaustive — re-read the whole transcript. Include ONLY facts the caller actually "
        "stated; use null/empty when unknown — never invent. Dates as YYYY-MM-DD. Amounts "
        "as plain numbers.\n\n"
        "EXTRACTION GUIDELINES (these are commonly missed — do NOT skip them):\n"
        "- PARTIES: list EVERY other person/entity involved — the at-fault driver, cyclist, "
        "dog owner, property owner, employer, or business, plus passengers and witnesses. "
        "Include the at-fault party EVEN IF unidentified or they fled (set full_name=null and "
        "describe them in notes, e.g. 'cyclist who hit caller from behind and fled; no "
        "witnesses'). A hit-and-run still has an at_fault party.\n"
        "- DAMAGES: capture EVERY economic impact mentioned — missed work / lost wages "
        "('lost_wages'), reduced ability to do their job or earn going forward "
        "('lost_earning_capacity'), medical bills ('medical'), future/expected medical care "
        "('future_medical'), vehicle or property damage ('property'), and other out-of-pocket "
        "costs. If they say they missed work or can't do their job, that IS a lost-wages "
        "damage even with no dollar figure (leave amount null).\n"
        "- TREATMENTS / FUTURE CARE: capture where they were treated AND any future care "
        "mentioned — follow-up visits, a cast, surgery, injections, ongoing therapy. Set "
        "is_ongoing=true when treatment continues; set injuries.requires_surgery / "
        "is_permanent when indicated.\n"
        "- INJURIES: capture each body part with severity; loss of function, severe pain, or "
        "inability to use a limb should be reflected in severity/description.\n\n"
        "JSON shape:\n"
        "{\n"
        '  "lead": {"full_name","date_of_birth"(YYYY-MM-DD if stated),"email","address",'
        '"occupation","employer","employment_status","annual_income"(number),"case_type",'
        '"preferred_contact_method","best_time_to_contact","has_attorney"(bool),'
        '"summary"(2-3 sentence neutral case summary)},\n'
        '  "incidents": [{"incident_date","location_text","description",'
        '"police_report_available"(bool),"fault_narrative","comparative_negligence_pct"(int)}],\n'
        '  "injuries": [{"body_part","description","severity","is_permanent"(bool),"requires_surgery"(bool)}],\n'
        '  "treatments": [{"provider_name","provider_type","treatment_type","start_date","end_date",'
        '"is_ongoing"(bool),"billed_amount"(number)}],\n'
        '  "insurance_policies": [{"party_role","carrier_name","policy_kind","coverage_limit"(number),"claim_number"}],\n'
        '  "parties": [{"role","full_name","notes"}],\n'
        '  "damages": [{"category","description","amount"(number),"is_estimated"(bool)}]\n'
        "}\n\n"
        f"case_type ∈ {CASE_TYPES}\n"
        f"injury severity ∈ {INJURY_SEVERITIES}\n"
        f"policy_kind ∈ {POLICY_KINDS}\n"
        f"insurance party_role ∈ {POLICY_PARTY_ROLES}\n"
        f"party role ∈ {PARTY_ROLES}\n"
        f"damage category ∈ {DAMAGE_CATEGORIES}\n"
    )


async def extract_from_transcript(transcript: str) -> Extraction:
    # DeepSeek v4-pro (thinking model) for accuracy — post-call, so latency is irrelevant.
    if not settings.deepseek_api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is not set")
    client = AsyncOpenAI(api_key=settings.deepseek_api_key, base_url=settings.deepseek_base_url)
    try:
        resp = await client.chat.completions.create(
            model=settings.deepseek_model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _prompt()},
                {"role": "user", "content": transcript},
            ],
        )
    finally:
        await client.close()
    return Extraction.model_validate_json(resp.choices[0].message.content or "{}")


async def merge_summaries(existing: str, new: str) -> str:
    """Fold a new call's summary into the running case summary (returning caller).

    Preserves every prior fact and adds/updates with the new call — so a follow-up
    call augments the summary instead of overwriting it. DeepSeek (post-call).
    """
    existing, new = (existing or "").strip(), (new or "").strip()
    if not existing:
        return new
    if not new:
        return existing
    if not settings.deepseek_api_key:
        return f"{existing} {new}".strip()  # degrade gracefully: append
    client = AsyncOpenAI(api_key=settings.deepseek_api_key, base_url=settings.deepseek_base_url)
    try:
        resp = await client.chat.completions.create(
            model=settings.deepseek_model,
            temperature=0,
            messages=[
                {"role": "system", "content": (
                    "You maintain a running case summary for a personal-injury lead across "
                    "multiple intake calls. Given the EXISTING summary and the NEW call's "
                    "summary, output ONE updated, neutral summary (2-4 sentences) that "
                    "PRESERVES all prior facts and folds in anything new or changed. Never "
                    "drop information. If the new call adds nothing, return the existing "
                    "summary. State representation status only once. Output only the summary "
                    "text — no preamble.")},
                {"role": "user", "content": f"EXISTING SUMMARY:\n{existing}\n\nNEW CALL SUMMARY:\n{new}"},
            ],
        )
    finally:
        await client.close()
    return (resp.choices[0].message.content or "").strip() or f"{existing} {new}".strip()
