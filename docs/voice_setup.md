# Voice intake — live setup (PRD-2)

The agent **brain**, telephony **webhooks**, extraction, memory, and handoff are
all tested. The one thing only a real phone call can validate is the **audio
path** (Twilio → LiveKit SIP → Deepgram ↔ DeepSeek ↔ Aura 2). These are the steps
to run that live smoke.

## 0. Prerequres
- `.env` has: `DATABASE_URL`, `APP_DB_PASSWORD`, `DEEPSEEK_API_KEY`,
  `DEEPGRAM_API_KEY`, `LIVEKIT_URL/API_KEY/API_SECRET`, `TWILIO_*`.
- A public tunnel for Twilio → our API, e.g. `ngrok http 8000`. Put the https URL
  in `.env` as `PUBLIC_BASE_URL=https://<id>.ngrok.app`.
- The firm's Twilio number is in `phone_numbers` for the org (the demo seed sets one;
  for a real test, insert your actual Twilio number for the firm).

## 1. LiveKit SIP inbound trunk + dispatch rule
Using the LiveKit CLI (`lk`) or the LiveKit Cloud dashboard:

- **Inbound trunk** accepting your Twilio number(s):
  ```bash
  lk sip inbound create \
    --numbers "+16076956595" \
    --name "medlegal-inbound"
  ```
- **Dispatch rule** → one room per call, dispatching our agent:
  ```bash
  lk sip dispatch create \
    --rule individual --room-prefix "call-" \
    --agent-name "medlegal-intake"
  ```
- Copy the trunk's **SIP URI host** (e.g. `xxxx.sip.livekit.cloud`) into `.env`:
  ```
  LIVEKIT_SIP_URI=xxxx.sip.livekit.cloud
  ```

## 2. Point Twilio at our webhook
On the Twilio number (Console → Phone Numbers → your number):
- **Voice → A call comes in → Webhook**: `POST {PUBLIC_BASE_URL}/api/voice/inbound`
- **Call status changes → Webhook**: `POST {PUBLIC_BASE_URL}/api/voice/status`

Our `/api/voice/inbound` resolves the firm from the dialed number, records the
call, and returns TwiML `<Dial><Sip>sip:<CallSid>@{LIVEKIT_SIP_URI}</Sip></Dial>`
to bridge the caller into LiveKit (when `LIVEKIT_SIP_URI` is set; otherwise it
falls back to voicemail capture so no lead is lost).

## 3. Run the pieces
```bash
# API (webhooks)
uvicorn app.main:app --reload

# Voice agent worker (separate process)
python -m app.agent.worker dev
```

## 4. Place the call
Dial the Twilio number. You should hear the recording/AI disclosure + language
prompt, then a natural PI intake. Afterwards verify in the DB:
- `voice_calls` row finalized (status, duration),
- `intake_transcripts` + `transcript_segments` populated,
- a `leads` row (partial) under the firm,
- `agent_events` logging tool calls.

## Notes
- **Signature validation:** keep `TWILIO_VALIDATE_WEBHOOKS=true` in prod;
  `PUBLIC_BASE_URL` must match exactly what Twilio calls.
- **Spanish:** STT runs `nova-3` multilingual; the default Aura 2 voice is English
  (`aura-2-thalia-en`). Per-language TTS voice switching is a v1.1 refinement
  (PRD §13.1) — `aura-2-celeste-es` is wired in `_AURA_VOICE`.
- **A2P 10DLC** must be approved for the follow-up/welcome SMS to deliver to US
  numbers (OTP via Verify is separate and already works).
