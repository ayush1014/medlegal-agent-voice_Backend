# Follow-up automation — running the tick

The funnel auto-runs intelligence right after each call (inline dispatch). The
**follow-up tick** is what advances leads afterward (request docs → send retainer →
nudge stalls) and catches any outbox event the inline path missed.

One tick = `python -m app.jobs.followups`:
1. sweeps the outbox (`dispatch_pending`) so unscored `intake.completed` events get processed;
2. runs `run_followups` for every firm (idempotent, per-org isolated).

## Option A — system cron (recommended, multi-instance safe)

Run every 10 minutes:

```cron
*/10 * * * * cd /path/to/medlegal-agent-voice_Backend && /path/to/.venv/bin/python -m app.jobs.followups >> /var/log/medlegal-followups.log 2>&1
```

(or a systemd timer / Cloud Scheduler hitting a small runner). This is the canonical
path — exactly one process ticks, so no double-sends.

## Option B — in-app scheduler (single instance only)

Set in `.env`:

```
FOLLOWUPS_SCHEDULER_ENABLED=true
FOLLOWUPS_INTERVAL_SECONDS=900
```

The API process then ticks on the interval (lifespan background task). Do **not**
enable this on more than one instance, or every instance will tick and double-send.

## Notes
- Idempotent + rate-limited (`leads.last_follow_up_at`), so an extra tick is harmless.
- WhatsApp sends require `TWILIO_WHATSAPP_NUMBER`; outside the 24h window you need
  approved templates (pass `content_sid` in `messaging_service.send_message`).
