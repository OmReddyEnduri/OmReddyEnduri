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
