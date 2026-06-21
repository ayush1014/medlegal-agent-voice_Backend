"""Short links — the tiny clickable URLs (`/u/{code}`) texted to clients.

Mounted at the app root (not under /api) so the SMS/WhatsApp link is as short as
possible. Resolves the code and serves a self-contained upload or sign page.
"""

from __future__ import annotations

import json

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.services import short_links

router = APIRouter(tags=["links"])

_INVALID = (
    "<div style='font-family:-apple-system,sans-serif;color:#888;text-align:center;margin-top:64px'>"
    "This link is invalid or has expired.<br>Please ask medLegal for a new one.</div>"
)

_HEAD = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>medLegal</title><style>
:root{color-scheme:dark}*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
 font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#0b0d10;color:#e7e9ee;padding:20px}
.card{width:100%;max-width:420px;background:#15181d;border:1px solid #262b33;border-radius:20px;padding:24px}
h1{font-size:19px;margin:0 0 6px}p{color:#9aa3af;font-size:14px;margin:0 0 18px;line-height:1.5}
input[type=file]{width:100%;padding:14px;border:1px dashed #39414d;border-radius:14px;background:#0e1116;color:#cbd2dc;font-size:14px}
button{width:100%;margin-top:14px;padding:14px;border:0;border-radius:14px;background:#3b82f6;color:#fff;font-size:15px;font-weight:600;cursor:pointer}
button:disabled{opacity:.6}.msg{margin-top:14px;font-size:14px;text-align:center;min-height:20px}.ok{color:#34d399}.err{color:#f87171}
pre{white-space:pre-wrap;background:#0e1116;border:1px solid #262b33;border-radius:12px;padding:14px;font-size:12px;color:#cbd2dc;max-height:200px;overflow:auto}
</style></head><body><div class="card">"""
_FOOT = "</div></body></html>"


def _upload_page(code: str) -> str:
    return _HEAD + """
<h1>Upload your documents</h1>
<p>Your secure link from medLegal. Add photos or PDFs — you can come back and add more anytime.</p>
<input id="f" type="file" accept="image/*,application/pdf" multiple>
<button id="b" onclick="up()">Upload</button>
<div class="msg" id="m"></div>
<script>
const CODE=__CODE__;
async function up(){const f=document.getElementById('f'),b=document.getElementById('b'),m=document.getElementById('m');
 if(!f.files.length){m.className='msg err';m.textContent='Please choose a file first.';return;}
 b.disabled=true;m.className='msg';m.textContent='Uploading…';let ok=0;
 for(const file of f.files){const fd=new FormData();fd.append('code',CODE);fd.append('file',file);
  try{const r=await fetch('/api/documents/upload',{method:'POST',body:fd});if(r.ok)ok++;}catch(e){}}
 b.disabled=false;
 if(ok===f.files.length){m.className='msg ok';m.textContent='✓ Uploaded '+ok+' file'+(ok>1?'s':'')+'. Thank you!';f.value='';}
 else{m.className='msg err';m.textContent='Uploaded '+ok+' of '+f.files.length+'. Please retry the rest.';}}
</script>""".replace("__CODE__", json.dumps(code)) + _FOOT


def _sign_page(code: str) -> str:
    return _HEAD + """
<h1>Your representation agreement</h1>
<p>Please review your Letter of Representation with medLegal, then sign below.</p>
<pre>LETTER OF REPRESENTATION

This confirms that medLegal will represent you in connection with your personal
injury matter, on a contingency-fee basis as permitted by law. By signing, you
retain medLegal to pursue your claim and authorize the firm to communicate with
insurers, providers, and other parties on your behalf.</pre>
<button id="b" onclick="sign()">Agree &amp; sign</button>
<div class="msg" id="m"></div>
<script>
const CODE=__CODE__;
async function sign(){const b=document.getElementById('b'),m=document.getElementById('m');
 b.disabled=true;m.className='msg';m.textContent='Signing…';
 const fd=new FormData();fd.append('code',CODE);
 try{const r=await fetch('/api/retainers/sign',{method:'POST',body:fd});
  if(r.ok){m.className='msg ok';m.textContent='✓ Signed. Welcome aboard — your legal team is on it.';}
  else{b.disabled=false;m.className='msg err';m.textContent='Could not sign. Please try again.';}}
 catch(e){b.disabled=false;m.className='msg err';m.textContent='Network error. Please try again.';}}
</script>""".replace("__CODE__", json.dumps(code)) + _FOOT


@router.get("/u/{code}", response_class=HTMLResponse)
async def short(code: str) -> HTMLResponse:
    resolved = await short_links.resolve(code)
    if resolved is None:
        return HTMLResponse(_INVALID, status_code=404)
    if resolved["purpose"] == short_links.SIGN:
        return HTMLResponse(_sign_page(code))
    return HTMLResponse(_upload_page(code))
