"""Local control UI on 127.0.0.1:8765.

Defences: loopback bind only; per-launch secret (cookie, SameSite=Strict); Host and Origin
checks (DNS-rebinding / CSRF). Approvals are deliberately NOT exposed here: they happen only
in the native dialog / console. The chat accepts a fixed command set and never executes input.
"""
import json
import secrets
import threading
import time

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from .engine import AgentEngine

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Laptop Agent</title><style>
:root{--bg:#fff;--fg:#1a1a1a;--card:#f5f5f7;--bd:#ddd;--ok:#0a7a3d;--bad:#b3261e}
@media(prefers-color-scheme:dark){:root{--bg:#111;--fg:#eee;--card:#1c1c1e;--bd:#333;--ok:#4cd38a;--bad:#ff6b63}}
body{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:14px system-ui,sans-serif}
.card{background:var(--card);border:1px solid var(--bd);border-radius:8px;padding:10px 12px;margin-bottom:10px}
button,input{font:inherit;padding:6px 10px;border:1px solid var(--bd);border-radius:6px;background:var(--card);color:var(--fg)}
pre{margin:0;max-height:220px;overflow:auto;white-space:pre-wrap;word-break:break-word;font-size:12px}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{text-align:left;padding:3px 6px;border-bottom:1px solid var(--bd);word-break:break-word}
.ok{color:var(--ok)}.bad{color:var(--bad)}
</style></head><body><h2>Laptop Agent</h2>
<div class="card" id="st"></div>
<div class="card"><button onclick="c('start')">Start</button> <button onclick="c('pause')">Pause</button> <button onclick="c('resume')">Resume</button> <button onclick="c('disconnect')">Disconnect</button>
<span id="msg"></span><br><small>Permission prompts appear only in the desktop window / console — never here.</small></div>
<div class="card"><b>Tasks</b><table id="tk"></table></div>
<div class="card"><b>Notifications</b><pre id="nt"></pre></div>
<div class="card"><b>Terminal output</b><pre id="tm"></pre></div>
<div class="card"><b>Chat with the Brain</b> (local commands start with /, try <code>/help</code>)<pre id="ch"></pre><input id="in" size="40" autocomplete="off"> <button onclick="send()">Send</button></div>
<script>
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const H={'X-Requested-With':'agent-ui'};
async function g(){const r=await fetch('/api/state',{headers:H});if(!r.ok)return;const s=await r.json();
 st.innerHTML=`<b>Connection:</b> <span class="${s.state==='connected'?'ok':'bad'}">${esc(s.state)}</span>${s.paused?' (paused)':''}<br><b>Relay:</b> ${esc(s.relay_url)}<br><b>Device:</b> ${esc(s.device_id)}<br><b>Active task:</b> ${esc(s.active||'-')}<br><b>Security level:</b> ${esc(s.security_level)}<br><b>Workspaces:</b> ${esc(s.workspaces.join(', '))}`;
 tk.innerHTML='<tr><th>Request</th><th>Type</th><th>Risk</th><th>Status</th></tr>'+s.tasks.map(t=>`<tr><td>${esc(t.request_id)}</td><td>${esc(t.task_type)}</td><td>${esc(t.risk)}</td><td>${esc(t.status)}</td></tr>`).join('');
 ch.textContent=s.chat.filter(m=>(m.channel||'app')==='app').map(m=>(m.who==='you'?'You: ':'Brain: ')+m.text).join('\\n');nt.textContent=s.notices.map(n=>n.level.toUpperCase()+': '+n.msg).join('\\n');tm.textContent=s.terminal.join('\\n')}
async function c(a){const r=await fetch('/api/control/'+a,{method:'POST',headers:H});msg.textContent=r.ok?'':' failed';g()}
async function send(){const t=document.getElementById('in').value;if(!t)return;document.getElementById('in').value='';
 const r=await fetch('/api/chat',{method:'POST',headers:{...H,'Content-Type':'application/json'},body:JSON.stringify({text:t})});
 const j=await r.json();if(j.reply&&j.reply!=='Sent to the Brain.')ch.textContent+='\\n['+j.reply+']';g()}
g();setInterval(g,2000);
</script></body></html>"""

HELP = ("Type normally to talk to the Brain. Local commands start with /: /status /level /pause /resume "
        "/disconnect /start /tasks /last /workspaces /help.\nThis chat cannot run commands or approve operations.")


def chat_reply(engine: AgentEngine, text: str, channel: str = "app") -> str:
    """Plain text goes to the Brain (as a message, never executed). '/command' runs a fixed local command."""
    text = text.strip()
    if not text:
        return ""
    if not text.startswith("/"):
        return "Sent to the Brain." if engine.send_user_message(text, channel) else "Not connected — press Start first."
    cmd = text[1:].strip().lower()
    if cmd == "help":
        return HELP
    if cmd == "status":
        s = engine.snapshot()
        return f"state={s['state']} paused={s['paused']} active={s['active']} device={s['device_id']}"
    if cmd == "pause":
        engine.pause()
        return "Paused: no new tasks will be claimed."
    if cmd == "resume":
        engine.resume()
        return "Resumed."
    if cmd == "start":
        engine.start()
        return "Started."
    if cmd == "disconnect":
        engine.disconnect()
        return "Disconnected."
    if cmd.startswith("level"):
        from .policy import LEVEL_NAMES
        lv = engine.policy.level
        return (f"Security level {lv} ({LEVEL_NAMES[lv]}). It can only be changed in the desktop window, "
                "not from chat, the web UI or the Brain.")
    if cmd == "workspaces":
        return "\n".join(engine.cfg.workspaces)
    if cmd in ("tasks", "last"):
        rows = list(engine.tasks)[: (1 if cmd == "last" else 5)]
        return "\n".join(f"{t['request_id']} {t.get('task_type','')} {t.get('status','')}" for t in rows) or "No tasks yet."
    return "Unknown command. " + HELP


def create_local_app(engine: AgentEngine, secret: str, port: int) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    ok_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    def guard(request: Request, write: bool):
        if request.headers.get("host", "") not in ok_hosts:
            raise HTTPException(400, "bad host")
        if not secrets.compare_digest(request.cookies.get("agent_ui", ""), secret):
            raise HTTPException(401, "open the UI from the desktop app")
        if write:
            origin = request.headers.get("origin")
            if origin and origin not in {f"http://{h}" for h in ok_hosts}:
                raise HTTPException(403, "bad origin")
            if request.headers.get("x-requested-with") != "agent-ui":
                raise HTTPException(403, "missing header")

    @app.middleware("http")
    async def headers(request: Request, call_next):
        r = await call_next(request)
        r.headers["Cache-Control"] = "no-store"
        r.headers["X-Frame-Options"] = "DENY"
        r.headers["Content-Security-Policy"] = ("default-src 'none'; script-src 'unsafe-inline'; "
                                                "style-src 'unsafe-inline'; connect-src 'self'")
        return r

    @app.get("/")
    def index(request: Request, t: str | None = None):
        if request.headers.get("host", "") not in ok_hosts:
            raise HTTPException(400, "bad host")
        if t is not None:
            if not secrets.compare_digest(t, secret):
                raise HTTPException(401, "bad token")
            resp = RedirectResponse("/")
            resp.set_cookie("agent_ui", secret, httponly=True, samesite="strict")
            return resp
        guard(request, False)
        return HTMLResponse(PAGE)

    @app.get("/api/state")
    def state(request: Request):
        guard(request, False)
        return engine.snapshot()

    @app.post("/api/control/{action}")
    def control(action: str, request: Request):
        guard(request, True)
        fn = {"start": engine.start, "pause": engine.pause, "resume": engine.resume,
              "disconnect": engine.disconnect}.get(action)
        if not fn:
            raise HTTPException(404)
        threading.Thread(target=fn, daemon=True).start() if action == "disconnect" else fn()
        return {"ok": True}

    @app.post("/api/chat")
    async def chat(request: Request):
        guard(request, True)
        body = await request.json()
        text = str(body.get("text", ""))[:200]
        return JSONResponse({"reply": chat_reply(engine, text)})

    # ---- OpenAI-compatible endpoint: talk to the Brain from any local tool, authenticated by an API key
    def v1_guard(request: Request):
        if request.headers.get("host", "") not in ok_hosts:
            raise HTTPException(400, "bad host")
        key = engine.cfg.local_api_key
        auth = request.headers.get("authorization", "")
        if not key or not auth.lower().startswith("bearer ") or not secrets.compare_digest(auth[7:].strip(), key):
            raise HTTPException(401, "invalid API key")

    @app.get("/v1/health")
    def v1_health(request: Request):
        """No key needed: proves THIS build of the agent is the one answering on this port."""
        if request.headers.get("host", "") not in ok_hosts:
            raise HTTPException(400, "bad host")
        return {"ok": True, "service": "laptop-agent", "api": "openai-compatible", "model": "brain",
                "state": engine.state, "needs_key_for": ["/v1/models", "/v1/chat/completions"]}

    @app.get("/v1/models")
    def v1_models(request: Request):
        v1_guard(request)
        return {"object": "list", "data": [{"id": "brain", "object": "model", "owned_by": "laptop-agent"}]}

    @app.post("/v1/chat/completions")
    async def v1_chat(request: Request):
        v1_guard(request)
        body = await request.json()
        text = ""
        for m in reversed(body.get("messages") or []):
            if m.get("role") == "user":
                c = m.get("content")
                text = c if isinstance(c, str) else " ".join(p.get("text", "") for p in c or [] if isinstance(p, dict))
                break
        if not text.strip():
            raise HTTPException(400, "no user message")
        wait = max(0, min(int(body.get("wait", 25)), 60))
        # Each API conversation is its own channel: the OpenAI `user` field or an X-Chat-Id header names it.
        # Replies go back ONLY to this caller; they never appear in the app's own chat.
        raw = str(body.get("user") or request.headers.get("x-chat-id") or "default")
        chat_id = "".join(ch for ch in raw if ch.isalnum() or ch in "_.-")[:40] or "default"
        channel = f"local:{chat_id}"
        t0 = time.time()
        if not engine.send_user_message(text, channel):
            raise HTTPException(503, "agent is not connected to the relay")
        reply = await _await_reply(engine, t0, wait, channel)
        out = reply or "(delivered to the Brain; no reply yet - it will appear in the app chat)"
        cid, now = f"chatcmpl-{secrets.token_hex(6)}", int(time.time())
        if body.get("stream"):
            def gen():
                yield "data: " + json.dumps({"id": cid, "object": "chat.completion.chunk", "created": now, "model": "brain",
                                             "choices": [{"index": 0, "delta": {"role": "assistant", "content": out}, "finish_reason": None}]}) + "\n\n"
                yield "data: " + json.dumps({"id": cid, "object": "chat.completion.chunk", "created": now, "model": "brain",
                                             "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}) + "\n\n"
                yield "data: [DONE]\n\n"
            return StreamingResponse(gen(), media_type="text/event-stream")
        return {"id": cid, "object": "chat.completion", "created": now, "model": "brain",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": out}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}

    return app


async def _await_reply(engine: AgentEngine, t0: float, wait: int, channel: str) -> str:
    import asyncio
    end = time.time() + wait
    while time.time() < end:
        new = [c for c in list(engine.chat) if c["who"] == "brain" and c["ts"] > t0 and c.get("channel") == channel]
        if new:
            await asyncio.sleep(1.5)       # let a multi-part reply finish arriving
            new = [c for c in list(engine.chat) if c["who"] == "brain" and c["ts"] > t0 and c.get("channel") == channel]
            return "\n\n".join(c["text"] for c in new)
        await asyncio.sleep(0.3)
    return ""


class LocalServer:
    def __init__(self, engine: AgentEngine, port: int):
        self.engine, self.failed = engine, False
        self.port, self.secret = port, secrets.token_urlsafe(24)
        cfg = uvicorn.Config(create_local_app(engine, self.secret, port), host="127.0.0.1", port=port,
                             log_level="warning")
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True, name="local-ui")

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.secret}"

    def start(self):
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if probe.connect_ex(("127.0.0.1", self.port)) == 0:   # something is already listening
                self.failed = True
                msg = (f"Port {self.port} is already in use, so the local API/web UI could not start. Another Laptop "
                       "Agent (an older copy?) is probably still running. Close it, then restart this app.")
                print("ERROR: " + msg)
                self.engine.notify("error", msg)
                return
        self.thread.start()

    def stop(self):
        self.server.should_exit = True
