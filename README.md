# medlegal-agent-voice — Backend

**MedLegal** is an AI voice-intake and lead-management platform for **Personal Injury (PI) law firms**. A caller dials the firm's number, talks to a natural AI intake specialist, and by the time they hang up the firm has a scored, qualified lead with a settlement estimate, a request for the right documents, and — when it qualifies — a Letter of Representation ready to e-sign. This repository is the **FastAPI backend**: the API, the realtime voice agent, the lead-intelligence engines, and the background workers.

> Frontend (Next.js admin dashboard + client portal) lives in `medlegal-agent-voice_Frontend`.

---

## What it does

```
 Inbound call ─▶ AI voice intake ─▶ post-call extraction ─▶ Lead intelligence ─▶ Document gathering ─▶ LOR e-sign
 (Twilio→SIP→     (LiveKit agent:     (DeepSeek v4-pro:      (score · qualify ·     (request → ingest →   (magic link →
  LiveKit)         STT/LLM/TTS)        structured facts +     settlement estimate)    classify → match)     typed signature →
                                       cumulative summary)                                                  signed PDF + ack)
```

End-to-end, per call:

1. **Voice intake.** Twilio routes the PSTN call over SIP into a LiveKit room; a LiveKit Agents worker runs the live conversation (Deepgram STT → LLM → Deepgram TTS) with barge-in, a two-party-consent disclosure, and red-teamed safety guardrails (no legal advice, no case value, no SOL calls). It gathers the PI story — who, what/when/where, fault, injuries, treatment, insurance, work/income, representation — confirming the must-be-exact fields (name, email) by spelling them back.
2. **Post-call processing.** On hangup the worker emits a `call.ended` event. A decoupled processor extracts structured facts (DeepSeek v4-pro), writes a two-tier summary (a short client-safe `ai_summary` + a rich internal `case_brief`), and merges cumulatively across repeat calls.
3. **Lead intelligence.** Scoring (7 factors / 100), qualification (ordered rules + hard blocks), and a settlement estimate (specials × pain-multiplier → severity floor → state-aware comparative-fault → coverage cap → completeness bands) all recompute.
4. **Document gathering.** Qualified leads get a document request by email/SMS; clients reply with photos/PDFs (or upload via the portal); an IMAP poller ingests attachments; gpt-4o vision classifies + content-matches them to the requested documents and re-estimates.
5. **Follow-ups.** A dynamic engine chases stalled doc-collection and unsigned-LOR leads over email + SMS on a per-lead cadence, within quiet hours, capped before flagging a human.
6. **LOR e-sign.** When ready, the firm sends a Letter of Representation; the client opens a magic link, types their name to sign, and receives an acknowledgment email with the fully-signed PDF.

---

## Tech stack

| Layer | Choice |
| --- | --- |
| API | **FastAPI** + **Uvicorn** (ASGI) |
| DB | **Neon Postgres 18** via **SQLAlchemy 2.0 (async)** + **asyncpg**; **Alembic** migrations |
| Multi-tenancy | Postgres **Row-Level Security** with a least-privilege `app_user` role |
| Realtime voice | **LiveKit Agents** (SIP ingress) · **Deepgram** Nova-3 (STT) + Aura-2 (TTS) |
| LLMs | **gpt-4o-mini** (realtime conversation + dashboard chat QA) · **DeepSeek v4-pro** (post-call extraction, summary, brief) · **gpt-4o** vision (document classification) · OpenAI `text-embedding-3-small` (RAG memory) |
| Telephony / messaging | **Twilio** (SIP voice, Verify OTP, SMS/WhatsApp) · **Gmail** SMTP+IMAP (document email in/out) |
| Storage | **Google Cloud Storage** (recordings + documents) |
| PDF | **fpdf2** (signed LOR) · **pypdf** (PDF text extraction) |
| Auth | JWT (HttpOnly cookies) · Argon2id · CSRF double-submit |
| Deploy | **Docker Compose** (api + worker) behind **Caddy** on a VPS |

---

## The intake call lifecycle (where the code lives)

| Stage | Module(s) |
| --- | --- |
| SIP ingress + live agent loop | `app/agent/worker.py`, `app/agent/prompt.py`, `app/agent/tools.py`, `app/agent/safety.py` |
| Live LLM / STT / TTS wiring | `app/agent/llm.py`, `app/agent/worker.py` (Deepgram + OpenAI plugins) |
| RAG context for returning callers | `app/agent/context.py`, `app/services/context_service.py`, `app/services/memory_service.py`, `app/agent/embeddings.py` |
| Transcript persistence + call records | `app/services/voice_service.py`, `app/services/intake_service.py` |
| Post-call extraction + summary/brief | `app/agent/extraction.py`, `app/services/extraction_service.py`, `app/services/intake_pipeline.py`, `app/jobs/post_call.py` |
| Scoring / qualification / settlement | `app/services/scoring_service.py`, `qualification_service.py`, `settlement_service.py`, `lead_facts.py`, `jurisdiction.py`, `lead_intelligence.py` |
| Calibration (estimate-vs-outcome) | `app/services/calibration_service.py` |
| Document request → ingest → classify → match | `app/services/document_service.py`, `document_ai.py`, `app/jobs/email_inbound.py`, `app/jobs/document_processing.py` |
| Follow-up automation | `app/services/followup_service.py`, `app/jobs/followups.py` |
| LOR / retainer e-sign | `app/services/retainer_service.py`, `pdf_service.py`, `short_links.py` |
| Messaging (SMS/WhatsApp/email) | `app/services/messaging_service.py`, `sms_service.py`, `email_service.py` |
| Auth / OTP / sessions | `app/services/auth_service.py`, `otp_service.py`, `session_service.py`, `app/security/*` |

---

## Event-driven workers (the outbox pattern)

Heavy/slow work never runs on the call's critical path. Producers write a row to an **outbox** in the same transaction as the state change; decoupled workers (started by the API process on an interval, or runnable as standalone crons) drain it with retry/backoff and per-org isolation:

| Event | Worker | Does |
| --- | --- | --- |
| `call.ended` | `app/jobs/post_call.py` | extraction → memory → lead intelligence re-estimate |
| `document.received` | `app/jobs/document_processing.py` | classify (vision) → content-match to requirements → re-estimate |
| (inbound email poll) | `app/jobs/email_inbound.py` | IMAP poll → ingest client attachments → emit `document.received` |
| (funnel sweep) | `app/jobs/followups.py` | advance pipeline + send email/SMS reminders, dynamically until the goal |

Toggle each via env (`POST_CALL_WORKER_ENABLED`, `DOCUMENT_WORKER_ENABLED`, `EMAIL_INBOUND_ENABLED`, `FOLLOWUPS_SCHEDULER_ENABLED`). In multi-instance deploys, run them as dedicated crons instead of in-process so each tick fires once.

---

## Lead intelligence (how a number gets to a dollar)

- **Scoring** — rules-v2, seven weighted factors (liability clarity, injury severity, treatment, insurance/coverage, representation, timeliness, completeness) → 0–100 + temperature.
- **Qualification** — ordered rules with hard blocks (e.g. already represented, clearly non-PI, no injury).
- **Settlement estimate** — `specials × pain-multiplier` → a **severity-prior floor** (so an injured case is never $0 for lack of specials) → **state-aware comparative-fault** bar (pure / modified-50 / modified-51 / contributory, per `jurisdiction.py`) → **coverage cap** (only when coverage is actually known and positive — unknown coverage is a soft ceiling, never a hard $0) → **completeness bands** that widen the range when data is thin.
- **Jurisdiction** — 50 states + DC: PI statute-of-limitations years and comparative-fault regime.
- **Calibration** — compares estimates against recorded actual outcomes (`settled`/`dropped`/`lost`/`referred_out`) to surface MAE / bias / within-band rate — the groundwork for closed-loop tuning.

---

## Security & multi-tenancy

Two Postgres roles, one database:

- **Owner** (`DATABASE_URL`) — migrations and admin only. Bypasses RLS.
- **`app_user`** (least-privilege, `NOBYPASSRLS`) — the role the app connects as **at runtime**, so **Row-Level Security is actually enforced**. The runtime URL is derived from `DATABASE_URL` with `app_user` + `APP_DB_PASSWORD`.

Every request stamps the tenant context (`organization_id`, subject, role) into transaction-local `app.*` GUCs; RLS policies read them and are **fail-closed** (no context → no rows). Pre-auth operations (login lookups, client signup, org provisioning) run under a `system` context pinned to the resolved org via `app/security/context.py::system_context`, so the app never needs the owner connection at runtime.

**Auth:** Argon2id passwords; JWT **access (15 min)** + **refresh (30 days)** in HttpOnly+Secure+SameSite cookies; refresh-token rotation with reuse detection; CSRF double-submit on mutations; Twilio Verify for client OTP (no codes stored); sliding-window rate limiting. Secrets live only in a gitignored `.env`.

---

## Project structure

```
app/
  main.py              # app factory, CORS, lifespan (starts in-process workers)
  config.py            # env-driven settings (owner + app-role connections, providers, toggles)
  database.py          # async engines/sessions; NEON_CONNECT_ARGS (statement cache off — see Gotchas)
  agent/               # LiveKit voice agent: worker, prompt, tools, safety, extraction, RAG context
  api/
    router.py          # aggregates feature routers
    routes/            # auth, leads, voice, documents, messaging, retainers, sign,
                       #   portal, followups, analytics, org, admin, links
  services/            # business logic (intake, scoring, qualification, settlement,
                       #   jurisdiction, calibration, documents, follow-ups, retainer, email, ...)
  jobs/                # outbox workers: post_call, document_processing, email_inbound, followups
  models/              # SQLAlchemy ORM + enums (controlled vocabularies)
  schemas/             # Pydantic request/response models
  security/            # tenant context, JWT tokens, cookies, CSRF, passwords
migrations/versions/   # Alembic migrations (async)
scripts/               # provision_app_role, seed_demo, setup_livekit_sip, configure_twilio_number, ...
docs/                  # voice_setup.md, whatsapp_templates.md, ...
tests/                 # RLS isolation, prompt contract, LLM behavioral evals, intelligence, voice
docker-compose.yml     # api + worker services
Caddyfile              # TLS reverse proxy
```

---

## Configuration (`.env`)

Required at minimum:

```bash
# --- App ---
ENVIRONMENT=development            # development | staging | production
# --- Database ---
DATABASE_URL=postgresql://OWNER:PWD@HOST/neondb?sslmode=require   # owner (migrations/admin)
APP_DB_PASSWORD=strong-password    # app_user role; WITHOUT it RLS is NOT enforced
# --- Auth ---
JWT_SECRET=long-random-secret
```

Provider keys (enable the corresponding feature when set):

```bash
# Voice
LIVEKIT_URL=wss://xxxx.livekit.cloud
LIVEKIT_API_KEY=...
LIVEKIT_API_SECRET=...
LIVEKIT_SIP_URI=xxxx.sip.livekit.cloud
DEEPGRAM_API_KEY=...
OPENAI_API_KEY=...                 # realtime LLM + vision + embeddings
DEEPSEEK_API_KEY=...               # post-call extraction / summary / brief

# Telephony & messaging
TWILIO_ACCOUNT_SID=...
TWILIO_AUTH_TOKEN=...
TWILIO_VERIFY_SERVICE_SID=...      # client OTP
FUNNEL_CHANNEL=sms                 # sms | whatsapp (follow-ups / docs / retainer)

# Email document intake (Gmail App Password)
GMAIL_USER=intake@yourfirm.com
GMAIL_APP_PASSWORD=...

# Storage
STORAGE_BACKEND=gcs
GCS_BUCKET_NAME=...
GOOGLE_APPLICATION_CREDENTIALS_JSON={...}    # service-account JSON

# Links / firm
FRONTEND_BASE_URL=https://your-frontend.vercel.app   # client portal + /sign magic links
FIRM_TIMEZONE=America/New_York

# Background jobs (defaults shown)
POST_CALL_WORKER_ENABLED=true
DOCUMENT_WORKER_ENABLED=true
EMAIL_INBOUND_ENABLED=true
FOLLOWUPS_SCHEDULER_ENABLED=false  # enable on a SINGLE instance, or run the cron instead
FOLLOWUP_NUDGE_INTERVAL_HOURS=1
FOLLOWUP_MAX_ATTEMPTS=10
FOLLOWUP_QUIET_START_HOUR=8
FOLLOWUP_QUIET_END_HOUR=20
```

See `app/config.py` for the full list and defaults.

---

## Setup

```bash
cd medlegal-agent-voice_Backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 1) Create / rotate the least-privilege role (reads APP_DB_PASSWORD)
python -m scripts.provision_app_role

# 2) Apply migrations (runs as the owner)
alembic upgrade head

# 3) (optional) seed a demo firm — slug `demo`, admin demo@example.com / demodemo123
python -m scripts.seed_demo
```

## Run (local)

```bash
# API (in-process workers start per the *_ENABLED toggles)
uvicorn app.main:app --reload          # http://localhost:8000  · /docs · /api/health

# Voice agent worker (separate process; needs LiveKit + Deepgram + an LLM key)
python -m app.agent.worker
```

Voice provisioning (one-time): `python -m scripts.setup_livekit_sip` and `python -m scripts.configure_twilio_number` — see `docs/voice_setup.md`.

## Tests

```bash
python -m pytest -m "not llm"     # fast: RLS isolation, prompt contract, intelligence, services
python -m pytest -m llm           # live behavioral evals (needs an LLM key; makes real calls)
```

## Migrations (Alembic)

```bash
alembic upgrade head          # apply
alembic downgrade -1          # roll back one
alembic check                 # detect model/DB drift
alembic revision -m "msg"     # new migration
```

## Deployment

Single VPS, Docker Compose (`api` + `worker`) behind Caddy (TLS), pointing at Neon + GCS:

```bash
docker compose up -d --build
```

The frontend deploys separately on Vercel and proxies `/api/*` to this backend.

---

## Operational gotchas

- **Neon (pooled endpoint) + asyncpg prepared statements.** Neon's pooled endpoint is PgBouncer in transaction mode; asyncpg's named prepared-statement cache goes stale after `ALTER TABLE`, causing `InvalidCachedStatementError` app-wide. **Fix (already applied):** all engines use `connect_args=NEON_CONNECT_ARGS` (`statement_cache_size=0`, `prepared_statement_cache_size=0`) in `app/database.py`.
- **Inbound email attachments.** Clients' photos arrive **inline**, not as `attachment` disposition — the IMAP ingester accepts any named image/PDF part and de-dups by Message-ID across a lookback window.
- **Follow-up scheduler.** `FOLLOWUPS_SCHEDULER_ENABLED=true` is for a single API instance; multi-instance deploys should disable it and run `python -m app.jobs.followups` as a cron so each tick fires once.
- **Prompt size budget.** The system prompt is re-sent every turn; `tests/test_intake_prompt_contract.py` guards it (size + every guardrail + the confirmation discipline) so refinements can't silently regress safety or balloon latency.

---

## Roadmap — next iterations

Where this platform goes next, ordered roughly by leverage. The goal of each is the same: make the PI intake **more efficient, more accurate, and more precise**.

### 1. Multi-agent telephony with warm transfers
A **triage/router agent** answers, identifies the caller's need, and warm-transfers to a **domain specialist agent** — each with its own prompt, voice, tools, and knowledge:
- **Medical** specialist — injuries, providers, treatment timeline, records requests.
- **Insurance / subrogation** specialist — coverage, UM/UIM/MedPay, adjusters, liens.
- **Legal intake** specialist — liability, jurisdiction, representation, SOL-sensitive facts.

Transfers carry full context (the in-progress lead + transcript) so the caller never repeats themselves, and a human attorney can be looped in (listen / whisper / take over) for high-value or sensitive calls.

### 2. Agentic follow-ups that ask for exactly what's missing
Evolve the current rule-based reminder engine into an **LLM follow-up agent** that reads each lead's *specific* gaps and composes personalized, non-templated **email + SMS** ("we still need your medical bills and the other driver's insurance"), parses the client's replies, extracts the answers, and updates the lead — escalating to an **outbound AI phone call** (LiveKit outbound SIP) when text goes unanswered. Closed-loop, until the case file is complete.

### 3. Real-time web research for settlement estimates
A research agent that, at estimate time, runs **live web/database research** — comparable jury verdicts and settlements for the injury type and **jurisdiction**, current medical-cost benchmarks, and venue tendencies — then grounds the estimate in cited, retrievable evidence with a confidence band, instead of relying solely on static multipliers. Feeds directly into `settlement_service` and the calibration loop.

### 4. Document & medical-records intelligence
OCR + structured extraction from medical bills and records (ICD/CPT codes, providers, dates, **charges → auto-computed specials**), police-report parsing, and an auto-assembled **treatment timeline** — turning uploaded paper into structured damages the moment it arrives.

### 5. Closed-loop calibration → auto-tuning
Feed recorded actual outcomes back through `calibration_service` to **auto-tune** the pain-multipliers, severity floors, and comparative-fault adjustments **per jurisdiction and case type** — the estimate gets more accurate with every settled case.

### 6. Legally-robust e-signature + case-management integrations
Swap the internal LOR mock for a certified e-sign provider (DocuSign / Dropbox Sign) with a full audit trail, and push qualified leads + signed retainers into firm CRMs (**Clio, Filevine, Litify, Salesforce**).

### 7. Compliance & trust
A2P 10DLC registered messaging, **TCPA consent logging + DNC scrubbing**, configurable call-recording retention, automated **conflict-of-interest checks** against existing clients/adversaries, and an automated QA/red-team monitor that flags any turn where the agent drifted toward legal advice, a case value, or an SOL call.

### 8. Reach & experience
Spanish + multilingual intake (the prompt already scaffolds it), **speed-to-lead** instant callback for hot leads, voicemail/missed-call text-back recovery, white-label per-firm branding (voice, prompt, intake rules), and forecasting/source-attribution analytics for the firm's pipeline.
