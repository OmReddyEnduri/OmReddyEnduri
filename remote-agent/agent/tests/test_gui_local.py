import threading, time
import pytest
from fastapi.testclient import TestClient
from PySide6.QtWidgets import QApplication, QPushButton

from laptop_agent.engine import ApprovalRequest
from laptop_agent.gui import ApprovalDialog, Bridge, MainWindow, QtApprover
from laptop_agent.localserver import create_local_app, chat_reply

app = QApplication.instance() or QApplication([])
REQ = ApprovalRequest("r1", "write_file", {"path": "/w/a.txt", "content": "hi"}, "/w", ["/w/a.txt"],
                      "because", "Creates new file", "hi")


def test_dialog_shows_all_fields_and_three_buttons():
    d = ApprovalDialog(REQ)
    labels = [b.text() for b in d.findChildren(QPushButton)]
    assert labels == ["APPROVE ONCE", "REJECT", "CANCEL"]
    text = " ".join(w.toPlainText() for w in d.findChildren(__import__("PySide6.QtWidgets", fromlist=["QPlainTextEdit"]).QPlainTextEdit))
    for needle in ("write_file", "/w/a.txt", "because", "Creates new file"):
        assert needle in text
    assert d.b_no.isDefault() and not d.b_ok.isDefault()


@pytest.mark.parametrize("button,expected", [("b_ok", "approve"), ("b_no", "reject"), ("b_cancel", "cancel")])
def test_qt_approver_roundtrip(button, expected):
    from PySide6.QtCore import QTimer
    bridge = Bridge()
    ap = QtApprover(bridge)
    holder_dlg = {}

    def on_approval(req, holder):
        dlg = ApprovalDialog(req); holder["dlg"] = dlg
        QTimer.singleShot(50, getattr(dlg, button).click)
        dlg.exec(); holder["answer"] = dlg.answer; holder["ev"].set()
    bridge.approval.connect(on_approval)
    res = {}
    t = threading.Thread(target=lambda: res.setdefault("a", ap.ask(REQ, threading.Event())))
    t.start()
    while t.is_alive():
        app.processEvents(); time.sleep(0.01)
    assert res["a"] == expected


def test_escape_close_is_never_approve():
    d = ApprovalDialog(REQ); d.reject()
    assert d.answer in ("reject", "cancel")


def test_main_window_builds(tmp_path):
    from laptop_agent.audit import Audit
    from laptop_agent.config import Config
    from laptop_agent.engine import AgentEngine
    from laptop_agent.localserver import LocalServer
    cfg = Config(relay_url="https://x.example", device_id="d", device_token="t" * 20, workspaces=[str(tmp_path)])
    eng = AgentEngine(cfg, None, Audit(cfg.secrets, tmp_path / "a.log"))
    w = MainWindow(eng, Bridge(), LocalServer(eng, 8765)); w.refresh()
    assert "Stopped" in w.status.text() or "Reconnecting" in w.status.text()


@pytest.fixture
def local(tmp_path):
    from laptop_agent.audit import Audit
    from laptop_agent.config import Config
    from laptop_agent.engine import AgentEngine
    cfg = Config(relay_url="https://x.example", device_id="d", device_token="t" * 20, workspaces=[str(tmp_path)])
    eng = AgentEngine(cfg, None, Audit(cfg.secrets, tmp_path / "a.log"))
    return eng, TestClient(create_local_app(eng, "S3CRET", 8765), base_url="http://127.0.0.1:8765")


def test_local_ui_requires_secret_host_and_header(local):
    eng, c = local
    assert c.get("/api/state").status_code == 401
    assert c.get("/?t=wrong").status_code == 401
    assert c.get("/?t=S3CRET", follow_redirects=False).status_code in (302, 307)
    assert c.get("/api/state").status_code == 200                       # cookie now set
    assert c.post("/api/control/pause").status_code == 403              # missing header (CSRF)
    assert c.post("/api/control/pause", headers={"X-Requested-With": "agent-ui", "Origin": "http://evil.com"}).status_code == 403
    assert c.post("/api/control/pause", headers={"X-Requested-With": "agent-ui"}).status_code == 200
    assert c.get("/api/state", headers={"Host": "evil.com"}).status_code == 400   # DNS rebinding
    assert c.post("/api/approve/r1", headers={"X-Requested-With": "agent-ui"}).status_code in (404, 405)  # no approval API


def test_chat_slash_commands_and_no_execution(local):
    eng, _ = local
    assert "Unknown command" in chat_reply(eng, "/rm -rf /")
    assert "Not connected" in chat_reply(eng, "run echo hi")        # plain text goes to the Brain, never executed
    assert "desktop window" in chat_reply(eng, "/level")
    assert chat_reply(eng, "/workspaces")


def test_preview_pane_in_app(tmp_path):
    from PIL import Image
    from laptop_agent.gui import PreviewPane
    pp = PreviewPane()
    Image.new("RGB", (400, 300), (10, 100, 200)).save(tmp_path / "a.png")
    (tmp_path / "p.html").write_text("<h1>Hello</h1><p>basic</p>")
    (tmp_path / "n.md").write_text("# Title\n\ntext")
    (tmp_path / "t.txt").write_text("plain")
    ok, msg = pp.show_plan({"path": str(tmp_path / "a.png")}); assert ok and "image" in msg and pp._pix is not None
    ok, msg = pp.show_plan({"path": str(tmp_path / "p.html")}); assert ok and "app" in msg and "Hello" in pp.text.toPlainText()
    ok, msg = pp.show_plan({"path": str(tmp_path / "n.md")}); assert ok and "Title" in pp.text.toPlainText()
    ok, msg = pp.show_plan({"path": str(tmp_path / "t.txt")}); assert ok and pp.text.toPlainText() == "plain"
    ok, msg = pp.show_plan({"path": str(tmp_path / "x.xyz")}); assert not ok
    (tmp_path / "bad.png").write_bytes(b"not an image")
    ok, msg = pp.show_plan({"path": str(tmp_path / "bad.png")}); assert not ok


def test_local_openai_endpoint(local):
    eng, c = local
    eng.cfg.local_api_key = "lak_testkey"
    H = {"Authorization": "Bearer lak_testkey"}
    assert c.get("/v1/models").status_code == 401
    assert c.get("/v1/models", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get("/v1/models", headers=H).json()["data"][0]["id"] == "brain"
    assert c.get("/v1/models", headers={**H, "Host": "evil.com"}).status_code == 400
    # not connected -> 503, nothing sent
    r = c.post("/v1/chat/completions", headers=H, json={"messages": [{"role": "user", "content": "hi"}], "wait": 1})
    assert r.status_code == 503
    # connected: message goes out, Brain reply is returned in OpenAI format
    import time
    sent = []
    eng._client = object(); eng._stop.clear()
    eng.send_user_message = lambda t, ch="app": (sent.append((t, ch)), eng._add_chat("you", t, ch),
                                                  threading.Timer(0.3, lambda: eng._add_chat("brain", "pong", ch)).start(),
                                                  threading.Timer(0.1, lambda: eng._add_chat("brain", "wrong chat!", "app")).start()) and True
    r = c.post("/v1/chat/completions", headers=H, json={"messages": [{"role": "system", "content": "x"}, {"role": "user", "content": "ping"}], "wait": 5})
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "pong"       # NOT "wrong chat!"
    assert sent == [("ping", "local:default")]
    r = c.post("/v1/chat/completions", headers={**H, "X-Chat-Id": "tool-b"}, json={"messages": [{"role": "user", "content": "yo"}], "wait": 5})
    assert sent[-1] == ("yo", "local:tool-b") and r.json()["choices"][0]["message"]["content"] == "pong"
    r = c.post("/v1/chat/completions", headers=H, json={"user": "alice smith!", "messages": [{"role": "user", "content": "k"}], "wait": 5, "stream": True})
    assert sent[-1] == ("k", "local:alicesmith") and "data: [DONE]" in r.text
    assert c.post("/v1/chat/completions", headers=H, json={"messages": []}).status_code == 400


def test_v1_health_without_key_and_port_conflict(local):
    eng, c = local
    r = c.get("/v1/health")
    assert r.status_code == 200 and r.json()["service"] == "laptop-agent"
    assert c.get("/v1/health", headers={"Host": "evil.com"}).status_code == 400
    import socket
    from laptop_agent.localserver import LocalServer
    s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen(); port = s.getsockname()[1]
    try:
        srv = LocalServer(eng, port); srv.start()
        assert srv.failed and any("already in use" in n["msg"] for n in eng.notices)
    finally:
        s.close()


def test_app_has_separate_chats_and_api_chats_are_read_only(local, tmp_path):
    from laptop_agent.gui import Bridge, MainWindow
    from laptop_agent.localserver import LocalServer
    eng, _ = local
    w = MainWindow(eng, Bridge(), LocalServer(eng, 8765))
    w._on_event("chat", {"who": "you", "text": "hello", "channel": "app"})
    w._on_event("chat", {"who": "you", "text": "api says hi", "channel": "local:scriptA"})
    w._on_event("chat", {"who": "brain", "text": "answer to api", "channel": "local:scriptA"})
    assert w.chat_list.count() == 2 and "API" in w.chat_list.item(1).text() and "●" in w.chat_list.item(1).text()
    assert [t for _, t in w._chats["app"]] == ["hello"]                      # API traffic is not in the app chat
    w.chat_list.setCurrentRow(1)
    assert not w.chat_in.isEnabled() and "answer to api" in w.chatview.toPlainText() and "●" not in w.chat_list.item(1).text()
    w.chat_list.setCurrentRow(0)
    assert w.chat_in.isEnabled() and "api says hi" not in w.chatview.toPlainText()
    w._titles["app:xyz"] = "Project X"; w._ensure_chat("app:xyz"); w._on_event("chat", {"who": "brain", "text": "in project x", "channel": "app:xyz"})
    assert w.chat_list.count() == 3 and "Project X" in w.chat_list.item(2).text()
