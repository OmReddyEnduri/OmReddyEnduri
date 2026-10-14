"""Real agent engine <-> real relay (served over HTTP by uvicorn)."""
import os, socket, threading, time
import httpx, pytest, uvicorn

from app.config import Settings
from app.main import create_app
from laptop_agent.audit import Audit
from laptop_agent.client import register
from laptop_agent.config import Config
from laptop_agent.engine import AgentEngine

BRAIN, ADMIN, ENROLL = "b" * 32, "a" * 32, "e" * 32


class ScriptedApprover:
    def __init__(self):
        self.answer, self.seen = "approve", []

    def ask(self, req, abort):
        self.seen.append(req)
        return self.answer


@pytest.fixture
def stack(tmp_path):
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    st = Settings(f"sqlite:///{tmp_path}/r.db", BRAIN, ADMIN, ENROLL, "", lost_window_s=90)
    srv = uvicorn.Server(uvicorn.Config(create_app(st), host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=srv.run, daemon=True); th.start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            httpx.get(url + "/healthz"); break
        except Exception:
            time.sleep(0.05)
    ws = tmp_path / "ws"; ws.mkdir()
    r = register(url, ENROLL, "test-laptop", {"os": "test"})
    cfg = Config(relay_url=url, device_id=r["device_id"], device_token=r["device_token"], workspaces=[str(ws)], security_level=3)
    appr = ScriptedApprover()
    eng = AgentEngine(cfg, appr, Audit(cfg.secrets, tmp_path / "audit.log"))
    cfg.poll_interval_s, cfg.heartbeat_interval_s = 0.15, 0.3
    brain = httpx.Client(base_url=url, headers={"Authorization": f"Bearer {BRAIN}"})
    eng.start()
    yield eng, brain, appr, cfg, ws, tmp_path
    eng.shutdown(); srv.should_exit = True; th.join(3)


def submit(brain, cfg, rid, ttype, args, timeout=20):
    r = brain.post("/api/v1/tasks", json={"request_id": rid, "device_id": cfg.device_id, "task_type": ttype,
                                          "arguments": args, "reason": "integration test", "timeout_seconds": timeout})
    assert r.status_code in (200, 201), r.text


def wait(brain, rid, want=None, t=15):
    end = time.time() + t
    while time.time() < end:
        j = brain.get(f"/api/v1/tasks/{rid}").json()
        if j["status"] in ("completed", "failed", "rejected", "blocked", "cancelled", "timeout") and (not want or j["status"] == want):
            return j
        time.sleep(0.1)
    raise AssertionError(f"timeout waiting for {rid}: {j}")


def test_online_and_auto_task(stack):
    eng, brain, appr, cfg, ws, _ = stack
    time.sleep(0.6)
    d = brain.get("/api/v1/devices/status").json()["devices"][0]
    assert d["online"] and d["agent_state"] == "running"
    submit(brain, cfg, "t-status", "device_status", {})
    j = wait(brain, "t-status")
    assert j["status"] == "completed" and "hostname" in j["stdout"] and j["exit_code"] == 0
    assert appr.seen == []          # auto-allowed: no prompt


def test_write_requires_approval_then_runs(stack):
    eng, brain, appr, cfg, ws, _ = stack
    submit(brain, cfg, "t-w", "write_file", {"path": str(ws / "hello.txt"), "content": "hello"})
    j = wait(brain, "t-w")
    assert j["status"] == "completed" and (ws / "hello.txt").read_text() == "hello"
    assert len(appr.seen) == 1 and appr.seen[0].operation == "write_file"


def test_reject_never_executes(stack):
    eng, brain, appr, cfg, ws, _ = stack
    appr.answer = "reject"
    submit(brain, cfg, "t-rej", "write_file", {"path": str(ws / "no.txt"), "content": "x"})
    j = wait(brain, "t-rej")
    assert j["status"] == "rejected" and not (ws / "no.txt").exists()


def test_cancel_button(stack):
    eng, brain, appr, cfg, ws, _ = stack
    appr.answer = "cancel"
    submit(brain, cfg, "t-can", "make_dir", {"path": str(ws / "d")})
    assert wait(brain, "t-can")["status"] == "cancelled" and not (ws / "d").exists()


def test_blocked_never_prompts(stack):
    eng, brain, appr, cfg, ws, _ = stack
    submit(brain, cfg, "t-blk", "run_command", {"argv": ["rm", "-rf", "/"]})
    j = wait(brain, "t-blk")
    assert j["status"] == "blocked" and appr.seen == []
    submit(brain, cfg, "t-out", "read_file", {"path": "/etc/passwd"})
    assert wait(brain, "t-out")["status"] == "blocked"


def test_run_command_output_and_exit_code(stack):
    eng, brain, appr, cfg, ws, _ = stack
    submit(brain, cfg, "t-cmd", "run_command", {"argv": ["python3", "-c", "import sys;print('out');print('err',file=sys.stderr);sys.exit(3)"]})
    j = wait(brain, "t-cmd")
    assert j["status"] == "failed" and j["exit_code"] == 3 and "out" in j["stdout"] and "err" in j["stderr"]
    assert j["started_at"] and j["completed_at"]


def test_secret_redaction_and_output_cap(stack):
    eng, brain, appr, cfg, ws, _ = stack
    cfg.max_output_bytes = 2000
    code = "print('password=supersecretvalue1 ' + 'x'*50000)"
    submit(brain, cfg, "t-sec", "run_command", {"argv": ["python3", "-c", code]})
    j = wait(brain, "t-sec")
    assert "supersecretvalue1" not in j["stdout"] and j["output_truncated"] and len(j["stdout"]) <= 2000


def test_timeout_kills_process(stack):
    eng, brain, appr, cfg, ws, _ = stack
    t0 = time.time()
    submit(brain, cfg, "t-to", "run_command", {"argv": ["python3", "-c", "import time;time.sleep(60)"]}, timeout=1)
    j = wait(brain, "t-to")
    assert j["status"] == "timeout" and time.time() - t0 < 10


def test_pause_stops_claiming_and_resume(stack):
    eng, brain, appr, cfg, ws, _ = stack
    eng.pause(); time.sleep(0.5)
    submit(brain, cfg, "t-p", "device_status", {})
    time.sleep(1.0)
    assert brain.get("/api/v1/tasks/t-p").json()["status"] == "queued"
    assert brain.get("/api/v1/devices/status").json()["devices"][0]["agent_state"] == "paused"
    eng.resume()
    assert wait(brain, "t-p")["status"] == "completed"


def test_duplicate_request_runs_once(stack):
    eng, brain, appr, cfg, ws, _ = stack
    for _ in range(3):
        submit(brain, cfg, "t-dup", "write_file", {"path": str(ws / "once.txt"), "content": "1"})
    wait(brain, "t-dup")
    assert len(appr.seen) == 1


def test_brain_cancel_during_command(stack):
    eng, brain, appr, cfg, ws, _ = stack
    submit(brain, cfg, "t-bc", "run_command", {"argv": ["python3", "-c", "import time;time.sleep(60)"]})
    for _ in range(60):
        if brain.get("/api/v1/tasks/t-bc").json()["status"] == "running": break
        time.sleep(0.1)
    brain.post("/api/v1/tasks/t-bc/cancel")
    assert wait(brain, "t-bc", "cancelled", 10)["status"] == "cancelled"


def test_disconnect_is_immediate_and_aborts_approval(stack):
    eng, brain, appr, cfg, ws, _ = stack
    ev = threading.Event()
    class Blocker:
        def ask(self, req, abort):
            ev.set()
            abort.wait(30)
            return "cancel"
    eng.approver = Blocker()
    submit(brain, cfg, "t-dis", "make_dir", {"path": str(ws / "q")})
    assert ev.wait(10)
    t0 = time.time(); eng.disconnect()
    assert time.time() - t0 < 5 and eng.state == "disconnected" and not (ws / "q").exists()


def test_revoked_token_stops_agent(stack):
    eng, brain, appr, cfg, ws, _ = stack
    httpx.post(cfg.relay_url + f"/api/v1/admin/devices/{cfg.device_id}/revoke", headers={"Authorization": f"Bearer {ADMIN}"})
    for _ in range(50):
        if eng.state == "auth_failed": break
        time.sleep(0.1)
    assert eng.state == "auth_failed"


def test_relay_outage_reconnects(stack):
    """Agent survives an unreachable relay and recovers (backoff path)."""
    eng, brain, appr, cfg, ws, _ = stack
    eng._client._c.base_url = "http://127.0.0.1:1"      # simulate outage
    for _ in range(50):
        if eng.state == "reconnecting": break
        time.sleep(0.1)
    assert eng.state == "reconnecting"
    eng._client._c.base_url = cfg.relay_url
    for _ in range(300):
        if eng.state == "connected": break
        time.sleep(0.1)
    assert eng.state == "connected"


def test_screenshot_end_to_end(stack, monkeypatch):
    import base64
    from PIL import Image, ImageGrab
    monkeypatch.setattr(ImageGrab, "grab", lambda **k: Image.new("RGB", (2000, 1000), (10, 120, 200)))
    eng, brain, appr, cfg, ws, _ = stack
    submit(brain, cfg, "t-shot", "screenshot", {"path": str(ws / "screenshots" / "a.png")})
    j = wait(brain, "t-shot")
    assert j["status"] == "completed" and (ws / "screenshots" / "a.png").exists()
    assert appr.seen and appr.seen[0].operation == "screenshot"
    assert j["data"]["width"] == 2000 and j["data"]["image_width"] == 1280
    assert base64.b64decode(j["data"]["image_base64"])[:2] == b"\xff\xd8"   # JPEG


def test_write_files_then_read_files_roundtrip(stack):
    eng, brain, appr, cfg, ws, _ = stack
    files = [{"path": str(ws / "app" / "src" / "App.jsx"), "content": "export default () => <h1>hi</h1>;\nconst token = getToken();\n"},
             {"path": str(ws / "app" / "package.json"), "content": '{"name":"app"}'},
             {"path": str(ws / "app" / "node_modules" / "x" / "i.js"), "content": "skipme"},
             {"path": str(ws / "app" / ".env"), "content": "SECRET=1"}]
    submit(brain, cfg, "t-wf", "write_files", {"files": files[:3]})
    assert wait(brain, "t-wf")["status"] == "completed" and len(appr.seen) == 1
    submit(brain, cfg, "t-wf2", "write_files", {"files": [files[3]]})      # .env write is blocked
    assert wait(brain, "t-wf2")["status"] == "blocked"
    submit(brain, cfg, "t-rf", "read_files", {"dir": str(ws / "app"), "globs": ["**/*.jsx", "**/*.json", "**/*.js"]})
    j = wait(brain, "t-rf")
    got = {os.path.basename(f["path"]): f for f in j["data"]["files"]}
    assert j["status"] == "completed" and set(got) == {"App.jsx", "package.json"}      # node_modules skipped
    assert "const token = getToken();" in got["App.jsx"]["content"]                     # code not mangled
    assert len(appr.seen) == 1                                                          # reads need no prompt


def test_level2_auto_creates_new_files_but_asks_to_overwrite(stack):
    eng, brain, appr, cfg, ws, _ = stack
    eng.set_security_level(2)
    submit(brain, cfg, "t-l2a", "write_file", {"path": str(ws / "new.jsx"), "content": "a"})
    assert wait(brain, "t-l2a")["status"] == "completed" and appr.seen == []      # auto, no prompt
    submit(brain, cfg, "t-l2b", "write_file", {"path": str(ws / "new.jsx"), "content": "b", "overwrite": True})
    assert wait(brain, "t-l2b")["status"] == "completed" and len(appr.seen) == 1  # overwrite asks
    eng.set_security_level(1)
    submit(brain, cfg, "t-l1", "write_file", {"path": str(ws / "new.jsx"), "content": "c", "overwrite": True})
    assert wait(brain, "t-l1")["status"] == "completed" and len(appr.seen) == 1   # level 1: no new prompt
    submit(brain, cfg, "t-l1c", "run_command", {"argv": ["python3", "-c", "print(1)"]})
    wait(brain, "t-l1c"); assert len(appr.seen) == 2                              # commands always ask
    assert (ws / "new.jsx").read_text() == "c"


def test_chat_both_ways(stack):
    eng, brain, appr, cfg, ws, _ = stack
    assert eng.send_user_message("hello brain, can you hear me?")
    deadline = time.time() + 10
    while time.time() < deadline:
        r = brain.get(f"/api/v1/messages?device_id={cfg.device_id}").json()
        if r["messages"]: break
        time.sleep(0.2)
    assert [m["text"] for m in r["messages"]] == ["hello brain, can you hear me?"]
    brain.post("/api/v1/messages", json={"device_id": cfg.device_id, "text": "loud and clear"})
    deadline = time.time() + 10
    while time.time() < deadline and not any(c["who"] == "brain" for c in eng.chat): time.sleep(0.2)
    assert [c["text"] for c in eng.chat if c["who"] == "brain"] == ["loud and clear"]


def test_binary_write_read_image_video_preview_download(stack, monkeypatch):
    import base64, io, subprocess, webbrowser, http.server, functools
    from PIL import Image
    eng, brain, appr, cfg, ws, _ = stack
    eng.set_security_level(1)
    buf = io.BytesIO(); Image.new("RGB", (1600, 900), (200, 30, 30)).save(buf, "PNG")
    submit(brain, cfg, "t-img", "write_file", {"path": str(ws / "m" / "red.png"), "encoding": "base64",
                                               "content": base64.b64encode(buf.getvalue()).decode()})
    assert wait(brain, "t-img")["status"] == "completed" and (ws / "m" / "red.png").read_bytes() == buf.getvalue()
    submit(brain, cfg, "t-ri", "read_image", {"path": str(ws / "m" / "red.png"), "max_width": 640})
    j = wait(brain, "t-ri")
    assert j["data"]["width"] == 1600 and j["data"]["image_width"] == 640
    assert base64.b64decode(j["data"]["image_base64"])[:2] == b"\xff\xd8" and appr.seen == []
    # real video via ffmpeg
    vid = ws / "m" / "clip.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=10",
                    "-pix_fmt", "yuv420p", str(vid)], check=True)
    submit(brain, cfg, "t-vf", "video_frames", {"path": str(vid), "count": 3})
    j = wait(brain, "t-vf", t=40)
    assert j["status"] == "completed" and len(j["data"]["frames"]) == 3
    assert base64.b64decode(j["data"]["frames"][0]["image_base64"])[:2] == b"\xff\xd8"
    # preview opens (browser stubbed)
    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda u: opened.append(u) or True)
    (ws / "index.html").write_text("<h1>hi</h1>")
    submit(brain, cfg, "t-pv", "open_preview", {"path": str(ws / "index.html")})
    assert wait(brain, "t-pv")["status"] == "completed" and opened and opened[0].startswith("file:")
    # download (loopback allowed only via the test flag)
    import laptop_agent.executor as ex
    monkeypatch.setattr(ex, "ALLOW_PRIVATE_DOWNLOADS", True)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(ws / "m")))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    # policy blocks http/localhost, so exercise the executor path directly with a verdict
    from laptop_agent.policy import Verdict
    v = Verdict("ask", "download_file", {}, plan={"url": f"http://127.0.0.1:{srv.server_port}/clip.mp4", "path": str(ws / "dl" / "clip.mp4"), "max_bytes": 5_000_000})
    r = ex.execute(v, 20, 1000, threading.Event())
    srv.shutdown()
    assert r.status == "completed" and (ws / "dl" / "clip.mp4").read_bytes() == vid.read_bytes()
    v.plan["max_bytes"] = 100
    srv2 = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(ws / "m")))
    threading.Thread(target=srv2.serve_forever, daemon=True).start()
    v.plan["url"] = f"http://127.0.0.1:{srv2.server_port}/clip.mp4"; v.plan["path"] = str(ws / "dl" / "big.mp4")
    r = ex.execute(v, 20, 1000, threading.Event()); srv2.shutdown()
    assert r.status == "failed" and "size limit" in r.error and not (ws / "dl" / "big.mp4").exists()
