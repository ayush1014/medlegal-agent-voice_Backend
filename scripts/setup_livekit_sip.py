"""Provision LiveKit SIP for inbound calls (PRD-2 Phase D, live setup).

Creates (idempotently) a SIP inbound trunk for the firm's number and a dispatch
rule that drops each call into its own room and auto-dispatches our named agent.
Prints the value to set as LIVEKIT_SIP_URI.

    python -m scripts.setup_livekit_sip
"""

from __future__ import annotations

import asyncio
from urllib.parse import urlparse

from livekit import api

from app.config import settings

NUMBER = "+16076956595"
AGENT_NAME = "medlegal-intake"
RULE_NAME = "medlegal-intake-rule"


def sip_host() -> str:
    host = urlparse(settings.livekit_url or "").hostname or ""
    if host.endswith(".livekit.cloud"):
        return host.replace(".livekit.cloud", ".sip.livekit.cloud")
    return host  # self-hosted: set the SIP host manually


async def main() -> None:
    lk = api.LiveKitAPI(
        url=settings.livekit_url,
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret,
    )
    try:
        trunks = await lk.sip.list_sip_inbound_trunk(api.ListSIPInboundTrunkRequest())
        trunk = next((t for t in trunks.items if NUMBER in t.numbers), None)
        if trunk:
            print(f"reusing inbound trunk {trunk.sip_trunk_id}")
        else:
            trunk = await lk.sip.create_sip_inbound_trunk(
                api.CreateSIPInboundTrunkRequest(
                    trunk=api.SIPInboundTrunkInfo(name="medlegal-inbound", numbers=[NUMBER])
                )
            )
            print(f"created inbound trunk {trunk.sip_trunk_id}")

        rules = await lk.sip.list_sip_dispatch_rule(api.ListSIPDispatchRuleRequest())
        existing = next((r for r in rules.items if r.name == RULE_NAME), None)
        if existing:
            print(f"reusing dispatch rule {existing.sip_dispatch_rule_id}")
        else:
            rule = await lk.sip.create_sip_dispatch_rule(
                api.CreateSIPDispatchRuleRequest(
                    name=RULE_NAME,
                    trunk_ids=[trunk.sip_trunk_id],
                    rule=api.SIPDispatchRule(
                        dispatch_rule_individual=api.SIPDispatchRuleIndividual(room_prefix="call-")
                    ),
                    room_config=api.RoomConfiguration(
                        agents=[api.RoomAgentDispatch(agent_name=AGENT_NAME)]
                    ),
                )
            )
            print(f"created dispatch rule {rule.sip_dispatch_rule_id}")
    finally:
        await lk.aclose()

    print()
    print(f"Set in .env →  LIVEKIT_SIP_URI={sip_host()}")


if __name__ == "__main__":
    asyncio.run(main())
