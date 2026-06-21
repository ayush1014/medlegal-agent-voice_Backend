"""Wave 1 — lead intelligence: scoring + qualification + settlement against the
red-teamed benchmark cases, plus the end-to-end funnel (intake.completed -> scored)."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.database import session_scope
from app.security.context import system_context
from app.services import (
    lead_intelligence,
    outbox_publisher,
    outbox_service,
    qualification_service,
    scoring_service,
    settlement_service,
)
from app.services.lead_facts import Facts

TODAY = date(2026, 6, 21)
FRESH = TODAY - timedelta(days=120)
AGED = TODAY - timedelta(days=400)
NARR = "The other driver ran a red light and struck my vehicle at full speed."


def _eval(f: Facts) -> dict:
    q = qualification_service.qualify(f, TODAY)
    sc = scoring_service.score(f, TODAY)
    st = settlement_service.estimate(f, today=TODAY, hard_block=q["hard_block"],
                                     qual_reason=q["reason"], use_llm=False)
    return {"q": q, "sc": sc, "st": st}


def _auto(**kw) -> Facts:
    base = dict(case_type="Auto Accident", police_report_available=True, fault_narrative=NARR,
                comparative_negligence_pct=0, incident_date=FRESH,
                parties=[{"role": "at_fault", "full_name": "Other Driver"}])
    base.update(kw)
    return Facts(**base)


CASES = [
    # name, facts, score(min,max), temps, quals, settle(low,high) or None=hard-block
    ("strong severe covered", _auto(
        injuries=[{"severity": "Severe"}],
        treatments=[{"provider_name": "ER", "billed_amount": 20000, "is_ongoing": True, "start_date": FRESH},
                    {"provider_name": "PT", "billed_amount": 20000, "is_ongoing": True, "start_date": FRESH}],
        policies=[{"party_role": "at_fault", "policy_kind": "Liability", "coverage_limit": 100000, "carrier_name": "SF"}],
        damages=[{"category": "medical", "amount": 40000}]),
     (82, 95), {"Hot"}, {"Qualified"}, (85000, 100000)),

    ("minor soft-tissue", _auto(
        injuries=[{"severity": "Minor"}],
        treatments=[{"provider_name": "Clinic", "billed_amount": 4000, "start_date": FRESH}],
        policies=[{"party_role": "at_fault", "policy_kind": "Liability", "coverage_limit": 25000, "carrier_name": "G"}],
        damages=[{"category": "medical", "amount": 4000}]),
     (46, 66), {"Warm", "Low"}, {"Possibly Qualified"}, (6000, 15000)),

    ("severe minimal policy", _auto(
        injuries=[{"severity": "Severe"}],
        treatments=[{"provider_name": "H", "billed_amount": 30000, "start_date": FRESH}],
        policies=[{"party_role": "at_fault", "policy_kind": "Liability", "coverage_limit": 15000, "carrier_name": "X"}],
        damages=[{"category": "medical", "amount": 30000}]),
     (56, 78), {"Warm", "Hot"}, {"Qualified"}, (12000, 15000)),

    ("severe coverage unknown", Facts(
        case_type="Pedestrian Accident", police_report_available=True,
        fault_narrative="The driver hit me in the crosswalk while I was a pedestrian crossing.",
        comparative_negligence_pct=0, incident_date=FRESH,
        parties=[{"role": "at_fault", "full_name": "D"}],
        injuries=[{"severity": "Severe"}],
        treatments=[{"provider_name": "H", "billed_amount": 50000, "start_date": FRESH}],
        policies=[], damages=[{"category": "medical", "amount": 50000}]),
     (66, 86), {"Warm", "Hot"}, {"Qualified"}, (90000, 160000)),

    ("already represented", _auto(
        has_attorney=True,
        injuries=[{"severity": "Severe"}],
        treatments=[{"provider_name": "ER", "billed_amount": 40000, "start_date": FRESH}],
        policies=[{"party_role": "at_fault", "policy_kind": "Liability", "coverage_limit": 100000, "carrier_name": "SF"}],
        damages=[{"category": "medical", "amount": 50000}]),
     (0, 15), {"Poor Fit"}, {"Needs Review"}, None),

    ("injury no treatment aged", Facts(
        case_type="Slip and Fall", incident_date=AGED,
        injuries=[{"severity": "Moderate"}], treatments=[], policies=[], parties=[], damages=[]),
     (14, 38), {"Poor Fit", "Low"}, {"Unqualified"}, None),
]


@pytest.mark.parametrize("name,facts,score_rng,temps,quals,settle", CASES,
                         ids=[c[0] for c in CASES])
def test_benchmark_cases(name, facts, score_rng, temps, quals, settle):
    r = _eval(facts)
    assert score_rng[0] <= r["sc"]["score"] <= score_rng[1], f"{name}: score {r['sc']['score']}"
    assert r["sc"]["temperature"] in temps, f"{name}: temp {r['sc']['temperature']}"
    assert r["q"]["status"] in quals, f"{name}: qual {r['q']['status']}"
    st = r["st"]
    if settle is None:
        assert st["expected"] == 0, f"{name}: hard-block should suppress settlement"
    else:
        slow, shigh = settle
        assert 0 < st["low"] <= st["expected"] <= st["high"], f"{name}: {st}"
        assert st["high"] <= shigh * 1.35, f"{name}: high {st['high']} >> {shigh}"
        assert slow * 0.6 <= st["expected"] <= shigh * 1.3, f"{name}: expected {st['expected']}"


def test_unknown_fault_does_not_beat_known_low_fault():
    """FIX #1 regression: comp=None must not score higher than recorded low fault."""
    common = dict(injuries=[{"severity": "Moderate"}],
                  treatments=[{"provider_name": "H", "billed_amount": 20000, "start_date": FRESH}],
                  policies=[{"party_role": "at_fault", "policy_kind": "Liability",
                             "coverage_limit": 100000, "carrier_name": "X"}],
                  damages=[{"category": "medical", "amount": 20000}])
    g1 = _auto(comparative_negligence_pct=None, **common)  # unknown fault
    g2 = _auto(comparative_negligence_pct=0, **common)     # recorded clean
    s1, s2 = scoring_service.score(g1, TODAY)["score"], scoring_service.score(g2, TODAY)["score"]
    assert s1 <= s2
    for g in (g1, g2):
        assert scoring_service.score(g, TODAY)["temperature"] in {"Warm", "Hot"}


# --- End-to-end funnel: intake.completed -> dispatch -> lead scored ---

@pytest_asyncio.fixture
async def org(owner_engine):
    oid = uuid.uuid4()
    async with owner_engine.begin() as c:
        await c.execute(text("INSERT INTO organizations (id,name,slug) VALUES (:o,'F',:s)"),
                        {"o": oid, "s": f"li-{oid.hex[:8]}"})
    yield oid
    async with owner_engine.begin() as c:
        await c.execute(text("DELETE FROM organizations WHERE id=:o"), {"o": oid})


async def test_funnel_dispatch_scores_lead(org, owner_engine):
    lead_id = uuid.uuid4()
    async with owner_engine.begin() as c:
        await c.execute(text("INSERT INTO leads (id,organization_id,full_name,phone,case_type,source,"
                             "pipeline_status) VALUES (:i,:o,'John Doe','+15550000000','Auto Accident',"
                             "'inbound_call','Intake Complete')"), {"i": lead_id, "o": org})
        await c.execute(text("INSERT INTO incidents (organization_id,lead_id,incident_date,"
                             "police_report_available,fault_narrative,comparative_negligence_pct) "
                             "VALUES (:o,:l,:d,true,:n,0)"), {"o": org, "l": lead_id, "d": FRESH, "n": NARR})
        await c.execute(text("INSERT INTO injuries (organization_id,lead_id,body_part,severity) "
                             "VALUES (:o,:l,'neck','Severe')"), {"o": org, "l": lead_id})
        await c.execute(text("INSERT INTO medical_treatments (organization_id,lead_id,provider_name,"
                             "billed_amount,is_ongoing) VALUES (:o,:l,'ER',40000,true)"), {"o": org, "l": lead_id})
        await c.execute(text("INSERT INTO insurance_policies (organization_id,lead_id,party_role,"
                             "policy_kind,coverage_limit,carrier_name) VALUES (:o,:l,'at_fault','Liability',100000,'SF')"),
                        {"o": org, "l": lead_id})
        await c.execute(text("INSERT INTO damages (organization_id,lead_id,category,amount) "
                             "VALUES (:o,:l,'medical',40000)"), {"o": org, "l": lead_id})

    async with session_scope(system_context(org)) as db:
        await outbox_service.emit_event(db, org, aggregate_type="lead", aggregate_id=lead_id,
                                        event_type="intake.completed", payload={"lead_id": str(lead_id)})

    res = await outbox_publisher.dispatch_pending_for_org(org)
    assert res["published"] == 1

    async with owner_engine.begin() as c:
        lead = (await c.execute(text("SELECT lead_score, lead_temperature, qualification_status, "
                                     "settlement_expected, pipeline_status FROM leads WHERE id=:i"),
                                {"i": lead_id})).first()
        n_scores = (await c.execute(text("SELECT count(*) FROM lead_scores WHERE lead_id=:i"),
                                    {"i": lead_id})).scalar_one()
        n_settle = (await c.execute(text("SELECT count(*) FROM settlement_estimates WHERE lead_id=:i"),
                                    {"i": lead_id})).scalar_one()
    assert lead.lead_score >= 75 and lead.lead_temperature == "Hot"
    assert lead.qualification_status == "Qualified" and lead.pipeline_status == "Qualified"
    assert lead.settlement_expected and lead.settlement_expected > 0
    assert n_scores == 1 and n_settle == 1
