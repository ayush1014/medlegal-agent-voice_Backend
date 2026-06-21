"""Cron job — run_all_orgs: outbox sweep + per-org follow-up advancement."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest_asyncio
from sqlalchemy import text

from app.config import settings
from app.database import session_scope
from app.jobs import followups as job
from app.security.context import system_context
from app.services import document_service, messaging_service, outbox_service

FRESH = date(2026, 6, 21) - timedelta(days=120)


@pytest_asyncio.fixture(autouse=True)
def mocks(monkeypatch):
    monkeypatch.setattr(settings, "twilio_whatsapp_number", "+14155238886")
    monkeypatch.setattr(settings, "funnel_channel", "whatsapp")  # deterministic, ignore .env
    monkeypatch.setattr(document_service, "_store_object", lambda p, c, m: f"gs://t/{p}")
    monkeypatch.setattr(messaging_service, "_twilio_send",
                        lambda **kw: ("WA_" + uuid.uuid4().hex[:12], "queued"))


async def _org(owner_engine, slug) -> uuid.UUID:
    oid = uuid.uuid4()
    async with owner_engine.begin() as c:
        await c.execute(text("INSERT INTO organizations (id,name,slug) VALUES (:o,'F',:s)"),
                        {"o": oid, "s": f"{slug}-{oid.hex[:8]}"})
    return oid


async def test_run_all_orgs_sweeps_and_advances(owner_engine):
    org_a = await _org(owner_engine, "joba")
    org_b = await _org(owner_engine, "jobb")
    try:
        # org A: a Qualified lead with no docs -> tick should request docs.
        la = uuid.uuid4()
        async with owner_engine.begin() as c:
            await c.execute(text("INSERT INTO leads (id,organization_id,full_name,phone,case_type,source,"
                                 "qualification_status,pipeline_status) VALUES (:i,:o,'A','+15551110000',"
                                 "'Auto Accident','inbound_call','Qualified','Qualified')"),
                            {"i": la, "o": org_a})
        # org B: a lead with a pending intake.completed event -> sweep should score it.
        lb = uuid.uuid4()
        async with owner_engine.begin() as c:
            await c.execute(text("INSERT INTO leads (id,organization_id,full_name,phone,case_type,source,"
                                 "pipeline_status) VALUES (:i,:o,'B','+15552220000','Auto Accident',"
                                 "'inbound_call','Intake Complete')"), {"i": lb, "o": org_b})
            await c.execute(text("INSERT INTO incidents (organization_id,lead_id,incident_date,"
                                 "police_report_available,fault_narrative,comparative_negligence_pct) "
                                 "VALUES (:o,:l,:d,true,'the other driver rear ended me at the light hard',0)"),
                            {"o": org_b, "l": lb, "d": FRESH})
            await c.execute(text("INSERT INTO injuries (organization_id,lead_id,body_part,severity) "
                                 "VALUES (:o,:l,'neck','Severe')"), {"o": org_b, "l": lb})
            await c.execute(text("INSERT INTO medical_treatments (organization_id,lead_id,provider_name,"
                                 "billed_amount,is_ongoing) VALUES (:o,:l,'ER',40000,true)"), {"o": org_b, "l": lb})
            await c.execute(text("INSERT INTO insurance_policies (organization_id,lead_id,party_role,"
                                 "policy_kind,coverage_limit,carrier_name) VALUES (:o,:l,'at_fault','Liability',100000,'SF')"),
                            {"o": org_b, "l": lb})
            await c.execute(text("INSERT INTO damages (organization_id,lead_id,category,amount) "
                                 "VALUES (:o,:l,'medical',40000)"), {"o": org_b, "l": lb})
        async with session_scope(system_context(org_b)) as db:
            await outbox_service.emit_event(db, org_b, aggregate_type="lead", aggregate_id=lb,
                                            event_type="intake.completed", payload={"lead_id": str(lb)})

        totals = await job.run_all_orgs()
        assert totals["orgs"] >= 2
        assert totals["events_published"] >= 1
        assert totals["docs_requested"] >= 1

        async with owner_engine.begin() as c:
            a_pipeline = (await c.execute(text("SELECT pipeline_status FROM leads WHERE id=:l"), {"l": la})).scalar_one()
            b_score = (await c.execute(text("SELECT lead_score, qualification_status FROM leads WHERE id=:l"), {"l": lb})).first()
        assert a_pipeline == "Docs Requested"           # org A advanced
        assert b_score.lead_score >= 75 and b_score.qualification_status == "Qualified"  # org B swept+scored
    finally:
        async with owner_engine.begin() as c:
            await c.execute(text("DELETE FROM organizations WHERE id IN (:a,:b)"), {"a": org_a, "b": org_b})
