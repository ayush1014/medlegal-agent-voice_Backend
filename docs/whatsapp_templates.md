# WhatsApp follow-ups — approved templates (fixes error 63016)

WhatsApp only allows **freeform** messages inside a 24-hour window that the *client*
opened by messaging you first. Our follow-ups are **business-initiated** (we text the
client first), so WhatsApp rejects freeform with **error 63016** and the message shows
`undelivered`. The fix is to send an **approved Content template**.

Our code already sends via template when the SID is configured (and falls back to
freeform otherwise, which works in-window / sandbox).

## 1. Create the templates (Twilio Console → Messaging → Content Template Builder)

Create three templates, category **Utility**, with these bodies (the `{{n}}` are
positional variables — keep the order exactly):

| Env var | Template body to paste | Vars |
|---|---|---|
| `WHATSAPP_TEMPLATE_DOC_REQUEST` | `Hi, it's {{1}}. To move your case forward we need a few documents: {{2}}. You can upload them securely here: {{3}} — or reply to this message with photos.` | 1=firm, 2=checklist, 3=link |
| `WHATSAPP_TEMPLATE_RETAINER` | `Great news from {{1}} — your representation agreement is ready. Please review and sign it here: {{2}}. Reply with any questions.` | 1=firm, 2=link |
| `WHATSAPP_TEMPLATE_NUDGE` | `Hi, it's {{1}} — a quick reminder about your case. Please reply here or use your secure link when you have a moment.` | 1=firm |

Submit each for WhatsApp approval. Approval is usually minutes for Utility templates.

## 2. Wire the SIDs

Once **Approved**, copy each template's **Content SID** (`HX…`) into `.env`:

```
WHATSAPP_TEMPLATE_DOC_REQUEST=HXxxxx…
WHATSAPP_TEMPLATE_RETAINER=HXxxxx…
WHATSAPP_TEMPLATE_NUDGE=HXxxxx…
```

Restart the API (and the worker). Now document requests, retainer sends, and nudges
go out as approved templates and deliver regardless of the 24h window.

## Quick test without templates (dev)

Message your WhatsApp business number (`+16076956595`) from your phone first — that
opens a 24-hour window, and freeform follow-ups will deliver. Good for a demo; for
real proactive follow-ups you need the approved templates above.

## Notes
- Variables are sent as `content_variables` JSON (`{"1": …, "2": …}`) — order matters.
- The comms log stores the freeform fallback text as the message body for readability;
  the actual delivered content is the approved template.
