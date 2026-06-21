"""Point a Twilio number's Voice webhook at our API.

Requires PUBLIC_BASE_URL in .env (a public https URL — e.g. an ngrok tunnel to
:8000). Sets the inbound Voice webhook + the status callback.

    PUBLIC_BASE_URL=https://<id>.ngrok.app python -m scripts.configure_twilio_number
"""

from __future__ import annotations

import asyncio
import sys

import httpx

from app.config import settings

NUMBER = "+16076956595"


async def main() -> None:
    base = settings.public_base_url
    if not base:
        sys.exit("PUBLIC_BASE_URL is not set (needs a public https URL Twilio can reach).")
    base = base.rstrip("/")
    sid, tok = settings.twilio_account_sid, settings.twilio_auth_token

    async with httpx.AsyncClient(timeout=20) as c:
        # Find the number's SID.
        r = await c.get(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/IncomingPhoneNumbers.json",
            params={"PhoneNumber": NUMBER}, auth=(sid, tok),
        )
        nums = r.json().get("incoming_phone_numbers", [])
        if not nums:
            sys.exit(f"{NUMBER} not found on this account.")
        number_sid = nums[0]["sid"]

        # Set the Voice webhook + status callback.
        upd = await c.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/IncomingPhoneNumbers/{number_sid}.json",
            data={
                "VoiceUrl": f"{base}/api/voice/inbound",
                "VoiceMethod": "POST",
                "StatusCallback": f"{base}/api/voice/status",
                "StatusCallbackMethod": "POST",
            },
            auth=(sid, tok),
        )
    if upd.status_code >= 400:
        sys.exit(f"Failed to update number: {upd.status_code} {upd.text[:200]}")
    print(f"Configured {NUMBER}:")
    print(f"  Voice webhook : {base}/api/voice/inbound")
    print(f"  Status callback: {base}/api/voice/status")


if __name__ == "__main__":
    asyncio.run(main())
