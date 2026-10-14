"""Local control UI on 127.0.0.1:8765.

Defences: loopback bind only; per-launch secret (cookie, SameSite=Strict); Host and Origin
checks (DNS-rebinding / CSRF). Approvals are deliberately NOT exposed here: they happen only
in the native dialog / console. The chat accepts a fixed command set and never executes input.
"""
import secrets
import threading

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

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
<div class="card"><b>Chat (fixed commands only — type <code>help</code>)</b><pre id="ch"></pre><input id="in" size="40" autocomplete="off"> <button onclick="send()">Send</button></div>
<script>
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const H={'X-Requested-With':'agent-ui'};
async function g(){const r=await fetch('/api/state',{headers:H});if(!r.ok)return;const s=await r.json();
 st.innerHTML=`<b>Connection:</b> <span class="${s.state==='connected'?'ok':'bad'}">${esc(s.state)}</span>${s.paused?' (paused)':''}<br><b>Relay:</b> ${esc(s.relay_url)}<br><b>Device:</b> ${esc(s.device_id)}<br><b>Active task:</b> ${esc(s.active||'-')}<br><b>Security level:</b> ${esc(s.security_level)}<br><b>Workspaces:</b> ${esc(s.workspaces.join(', '))}`;
 tk.innerHTML='<tr><th>Request</th><th>Type</th><th>Risk</th><th>Status</th></tr>'+s.tasks.map(t=>`<tr><td>${esc(t.request_id)}</td><td>${esc(t.task_type)}</td><td>${esc(t.risk)}</td><td>${esc(t.status)}</td></tr>`).join('');
 nt.textContent=s.notices.map(n=>n.level.toUpperCase()+': '+n.msg).join('\\n');tm.textContent=s.terminal.join('\\n')}
async function c(a){const r=await fetch('/api/control/'+a,{method:'POST',headers:H});msg.textContent=r.ok?'':' failed';g()}
async function send(){const t=document.getElementById('in').value;if(!t)return;document.getElementById('in').value='';
 const r=await fetch('/api/chat',{method:'POST',headers:{...H,'Content-Type':'application/json'},body:JSON.stringify({text:t})});
 const j=await r.json();ch.textContent+='> '+t+'\\n'+j.reply+'\\n';ch.scrollTop=ch.scrollHeight}
g();setInterval(g,2000);
</script></body></html>"""

HELP = ("Commands: status | level | pause | resume | disconnect | start | tasks | last | workspaces | help.\n"
        "This chat cannot run commands or approve operations.")


def chat_reply(engine: AgentEngine, text: str) -> str:
    cmd = text.strip().lower()
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
    return "I only understand fixed commands. " + HELP


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

    return app


class LocalServer:
    def __init__(self, engine: AgentEngine, port: int):
        self.port, self.secret = port, secrets.token_urlsafe(24)
        cfg = uvicorn.Config(create_local_app(engine, self.secret, port), host="127.0.0.1", port=port,
                             log_level="warning")
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True, name="local-ui")

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.secret}"

    def start(self):
        self.thread.start()

    def stop(self):
        self.server.should_exit = True
