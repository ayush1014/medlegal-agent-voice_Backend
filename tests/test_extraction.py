"""Phase E — post-call extraction: deterministic persistence + live DeepSeek."""

from __future__ import annotations

import uuid

import pytest_asyncio
from sqlalchemy import text

from app.agent.context import IntakeContext
from app.agent.extraction import (
    ExtractedDamage,
    ExtractedIncident,
    ExtractedInjury,
    ExtractedLead,
    ExtractedParty,
    ExtractedPolicy,
    ExtractedTreatment,
    Extraction,
    extract_from_transcript,
)
from app.database import session_scope
from app.models.enums import CASE_TYPES
from app.security.context import system_context
from app.services import extraction_service

SAMPLE = """\
Agent: Thank you for calling medLegal. This call is recorded and you're speaking with an AI assistant. For English just keep talking.
Caller: English is fine.
Agent: Can I get your full name?
Caller: John Doe.
Agent: What happened?
Caller: I was rear-ended at a red light last week in Atlanta. The other driver hit me from behind.
Agent: Were you injured?
Caller: Yeah, my neck and lower back really hurt.
Agent: Did you get medical care?
Caller: I went to urgent care and I'm starting physical therapy.
Agent: Was a police report filed?
Caller: Yes, the police came and made a report.
Agent: Do you have insurance, and does the other driver?
Caller: I have State Farm. The other driver has insurance too, I think Geico.
Agent: Do you already have an attorney for this?
Caller: No, not yet.
Agent: Did you miss work?
Caller: Yes, I missed about three days, maybe nine hundred dollars.
"""


@pytest_asyncio.fixture
async def lead(owner_engine):
    org, lead_id = uuid.uuid4(), uuid.uuid4()
    async with owner_engine.begin() as c:
        await c.execute(text("INSERT INTO organizations (id,name,slug) VALUES (:o,'F',:s)"),
                        {"o": org, "s": f"ext-{org.hex[:8]}"})
        await c.execute(
            text("INSERT INTO leads (id, organization_id, full_name, phone, case_type, source, "
                 "pipeline_status) VALUES (:id,:o,'Caller','+15550000000','Other Personal Injury',"
                 "'inbound_call','Intake Started')"),
            {"id": lead_id, "o": org},
        )
    yield {"org": org, "lead_id": lead_id}
    async with owner_engine.begin() as c:
        await c.execute(text("DELETE FROM organizations WHERE id=:o"), {"o": org})


async def test_persist_normalizes_enums_and_writes_children(lead, owner_engine):
    ctx = IntakeContext(organization_id=lead["org"], caller_phone="", lead_id=lead["lead_id"])
    ex = Extraction(
        lead=ExtractedLead(full_name="John Doe", case_type="totally not valid",
                           has_attorney=False, summary="Rear-ended at a light."),
        incidents=[ExtractedIncident(incident_date="2026-06-12", description="rear-ended")],
        injuries=[ExtractedInjury(body_part="neck", severity="bogus-severity")],
        treatments=[ExtractedTreatment(provider_name="Urgent Care", is_ongoing=True)],
        insurance_policies=[ExtractedPolicy(party_role="weird", carrier_name="State Farm", policy_kind="nope")],
        parties=[ExtractedParty(role="not-a-role", full_name="Other Driver")],
        damages=[ExtractedDamage(category="weird", amount=900.0),
                 ExtractedDamage(category="medical", amount=None)],  # None amount → skipped
    )
    async with session_scope(system_context(lead["org"])) as db:
        counts = await extraction_service.persist_extraction(db, ctx, ex)

    assert counts == {"incidents": 1, "injuries": 1, "treatments": 1, "policies": 1, "parties": 1, "damages": 1}

    async with owner_engine.begin() as c:
        ld = (await c.execute(text("SELECT full_name, case_type, pipeline_status, ai_summary "
                                   "FROM leads WHERE id=:i"), {"i": lead["lead_id"]})).first()
        sev = (await c.execute(text("SELECT severity FROM injuries WHERE lead_id=:i"),
                               {"i": lead["lead_id"]})).scalar_one()
        dmg = (await c.execute(text("SELECT category, amount FROM damages WHERE lead_id=:i"),
                               {"i": lead["lead_id"]})).first()
        pol_role = (await c.execute(text("SELECT party_role, policy_kind FROM insurance_policies WHERE lead_id=:i"),
                                    {"i": lead["lead_id"]})).first()
        party_role = (await c.execute(text("SELECT role FROM parties WHERE lead_id=:i"),
                                      {"i": lead["lead_id"]})).scalar_one()

    assert ld.full_name == "John Doe"
    assert ld.case_type == "Other Personal Injury"   # invalid → normalized
    assert ld.pipeline_status == "Intake Complete"
    assert "represented: no" in ld.ai_summary.lower()
    assert sev is None                                # invalid severity → null
    assert dmg.category == "other" and dmg.amount == 900
    assert pol_role.party_role == "other" and pol_role.policy_kind is None
    assert party_role == "other"


# --- Live DeepSeek extraction ---

async def test_live_extraction_from_transcript(lead):
    ex = await extract_from_transcript(SAMPLE)
    assert ex.lead.full_name and "doe" in ex.lead.full_name.lower()
    assert ex.lead.case_type in CASE_TYPES  # should land on "Auto Accident"
    assert ex.lead.has_attorney is False
    assert len(ex.injuries) >= 1            # neck / back
    # body parts mentioned should surface somewhere
    bodies = " ".join((i.body_part or "") + (i.description or "") for i in ex.injuries).lower()
    assert "neck" in bodies or "back" in bodies


async def test_live_run_post_call_extraction_persists(lead, owner_engine):
    counts = await extraction_service.run_post_call_extraction(
        lead["org"], lead["lead_id"], SAMPLE
    )
    assert counts["injuries"] >= 1
    async with owner_engine.begin() as c:
        n_inj = (await c.execute(text("SELECT count(*) FROM injuries WHERE lead_id=:i"),
                                 {"i": lead["lead_id"]})).scalar_one()
        pipeline = (await c.execute(text("SELECT pipeline_status FROM leads WHERE id=:i"),
                                    {"i": lead["lead_id"]})).scalar_one()
    assert n_inj >= 1
    assert pipeline == "Intake Complete"
