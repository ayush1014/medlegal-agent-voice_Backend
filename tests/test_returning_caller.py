"""Returning caller: one profile per phone, dossier recall, extraction dedup."""

from __future__ import annotations

import uuid
from datetime import date

import pytest_asyncio
from sqlalchemy import text

from app.agent.context import IntakeContext
from app.agent.extraction import (
    Extraction, ExtractedIncident, ExtractedInjury, ExtractedLead, ExtractedTreatment,
)
from app.database import session_scope
from app.security.context import system_context
from app.services import extraction_service, intake_service


@pytest_asyncio.fixture
async def org(owner_engine):
    oid = uuid.uuid4()
    async with owner_engine.begin() as c:
        await c.execute(text("INSERT INTO organizations (id,name,slug) VALUES (:o,'F',:s)"),
                        {"o": oid, "s": f"rc-{oid.hex[:8]}"})
    yield oid
    async with owner_engine.begin() as c:
        await c.execute(text("DELETE FROM organizations WHERE id=:o"), {"o": oid})


async def test_same_number_reuses_one_lead(org, owner_engine):
    phone = "+1" + f"{uuid.uuid4().int % 10**10:010d}"

    ctx1 = IntakeContext(organization_id=org, caller_phone=phone)
    async with session_scope(system_context(org)) as db:
        await intake_service.create_session_records(db, ctx1)
    assert ctx1.returning is False
    first_lead = ctx1.lead_id

    # Second call from the same number → same lead, flagged returning.
    ctx2 = IntakeContext(organization_id=org, caller_phone=phone)
    async with session_scope(system_context(org)) as db:
        await intake_service.create_session_records(db, ctx2)
    assert ctx2.lead_id == first_lead
    assert ctx2.returning is True

    async with owner_engine.begin() as c:
        n_leads = (await c.execute(text("SELECT count(*) FROM leads WHERE organization_id=:o AND phone=:p"),
                                   {"o": org, "p": phone})).scalar_one()
        n_transcripts = (await c.execute(text("SELECT count(*) FROM intake_transcripts WHERE lead_id=:l"),
                                         {"l": first_lead})).scalar_one()
    assert n_leads == 1            # one profile per phone
    assert n_transcripts == 2      # but a transcript per call


async def test_dossier_recall(org, owner_engine):
    lead_id = uuid.uuid4()
    async with owner_engine.begin() as c:
        await c.execute(text("INSERT INTO leads (id,organization_id,full_name,phone,case_type,source,"
                             "ai_summary) VALUES (:i,:o,'Jane Roe','+15550000000','Auto Accident',"
                             "'inbound_call','Rear-ended at a light.')"), {"i": lead_id, "o": org})
        await c.execute(text("INSERT INTO injuries (organization_id,lead_id,body_part,severity) "
                             "VALUES (:o,:l,'neck','Moderate')"), {"o": org, "l": lead_id})
    async with session_scope(system_context(org)) as db:
        dossier = await intake_service.build_dossier(db, lead_id)
    assert dossier and "Jane Roe" in dossier and "neck" in dossier and "Auto Accident" in dossier


async def test_extraction_dedup_on_repeat(org, owner_engine):
    lead_id = uuid.uuid4()
    async with owner_engine.begin() as c:
        await c.execute(text("INSERT INTO leads (id,organization_id,full_name,phone,case_type,source) "
                             "VALUES (:i,:o,'Caller','+15550000000','Other Personal Injury','inbound_call')"),
                        {"i": lead_id, "o": org})
    ctx = IntakeContext(organization_id=org, caller_phone="+15550000000", lead_id=lead_id)
    ex = Extraction(
        lead=ExtractedLead(full_name="John Doe", case_type="Auto Accident"),
        incidents=[ExtractedIncident(incident_date="2026-06-10", description="rear-ended")],
        injuries=[ExtractedInjury(body_part="neck", severity="Moderate")],
        treatments=[ExtractedTreatment(provider_name="Urgent Care")],
    )
    # Persist twice (as if two calls extracted the same facts).
    for _ in range(2):
        async with session_scope(system_context(org)) as db:
            await extraction_service.persist_extraction(db, ctx, ex)

    async with owner_engine.begin() as c:
        n_inj = (await c.execute(text("SELECT count(*) FROM injuries WHERE lead_id=:l"), {"l": lead_id})).scalar_one()
        n_inc = (await c.execute(text("SELECT count(*) FROM incidents WHERE lead_id=:l"), {"l": lead_id})).scalar_one()
        n_tx = (await c.execute(text("SELECT count(*) FROM medical_treatments WHERE lead_id=:l"), {"l": lead_id})).scalar_one()
    assert n_inj == 1 and n_inc == 1 and n_tx == 1  # not duplicated on the repeat call
