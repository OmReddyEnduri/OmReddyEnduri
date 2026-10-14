import threading, time
import pytest
from fastapi.testclient import TestClient
from PySide6.QtWidgets import QApplication, QPushButton

from laptop_agent.engine import ApprovalRequest
from laptop_agent.store import ChatStore
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
    eng = AgentEngine(cfg, None, Audit(cfg.secrets, tmp_path / "a.log"), ChatStore(tmp_path / "c.db"))
    w = MainWindow(eng, Bridge(), LocalServer(eng, 8765)); w.refresh()
    assert "Stopped" in w.status.text() or "Reconnecting" in w.status.text()


@pytest.fixture
def local(tmp_path):
    from laptop_agent.audit import Audit
    from laptop_agent.config import Config
    from laptop_agent.engine import AgentEngine
    cfg = Config(relay_url="https://x.example", device_id="d", device_token="t" * 20, workspaces=[str(tmp_path)])
    eng = AgentEngine(cfg, None, Audit(cfg.secrets, tmp_path / "a.log"), ChatStore(tmp_path / "c.db"))
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


def test_app_chat_history_agents_page_and_read_only_api_chats(local):
    from laptop_agent.gui import Bridge, MainWindow
    from laptop_agent.localserver import LocalServer
    eng, _ = local
    eng.store.ensure_chat("app")
    eng.store.add_message("app", "you", "earlier message"); eng.store.add_message("app", "brain", "earlier reply", agent="nova")
    eng.router.agents = {"nova": {"agent_id": "nova", "name": "Nova", "online": True},
                         "atlas": {"agent_id": "atlas", "name": "Atlas", "online": False}}
    w = MainWindow(eng, Bridge(), LocalServer(eng, 8765))
    assert w.chatview.count == 2                                              # history is loaded from the LOCAL store
    # live events: an API client's chat appears as its own read-only entry
    eng.store.ensure_chat("local:scriptA"); eng.store.add_message("local:scriptA", "you", "api says hi")
    w._on_event("chat", {"who": "you", "text": "api says hi", "channel": "local:scriptA", "agent": None})
    assert w.chat_list.count() == 2 and "API" in w.chat_list.item(1).text() and "●" in w.chat_list.item(1).text()
    assert w.chatview.count == 2                                              # not shown in the General chat
    w.chat_list.setCurrentRow(1)
    assert not w.chat_in.isEnabled() and w.chatview.count == 1 and "●" not in w.chat_list.item(1).text()
    w.chat_list.setCurrentRow(0)
    assert w.chat_in.isEnabled()
    # agent selector + agents page
    w.refresh()
    labels = [w.agent_combo.itemText(i) for i in range(w.agent_combo.count())]
    assert labels[0].startswith("Auto") and "Nova  (online)" in labels and "Atlas  (offline)" in labels
    w.agent_combo.setCurrentIndex(labels.index("Atlas  (offline)")); w._on_pin(labels.index("Atlas  (offline)"))
    assert eng.store.chat("app")["pinned"] == "atlas"
    assert w.agents_box.count() == 3                                          # two cards + stretch
    # a new chat is stored on the laptop
    w._on_event("chat", {"who": "brain", "text": "hi", "channel": "app", "agent": "nova"})
    assert w.chatview.count == 3


# ------------------------------------------------------------------ preview / video / media library
def _make_video(tmp_path, secs=3):
    import subprocess
    from laptop_agent import media_tools as mt
    v = tmp_path / "clip.mp4"
    subprocess.run([mt.ffmpeg_path(), "-v", "error", "-f", "lavfi", "-i", f"testsrc=duration={secs}:size=320x240:rate=24",
                    "-pix_fmt", "yuv420p", str(v)], check=True)
    return v


def _pump(cond, secs=8):
    end = time.time() + secs
    while time.time() < end and not cond():
        app.processEvents(); time.sleep(0.02)
    app.processEvents()
    return cond()


def test_video_player_plays_pauses_and_seeks_inside_the_app(tmp_path):
    from laptop_agent.gui import VideoPlayer
    vp = VideoPlayer(); vp.resize(700, 460); vp.show()
    info = vp.load(str(_make_video(tmp_path)))
    assert round(info["duration"]) == 3 and info["width"] == 320 and info["height"] == 240
    assert _pump(lambda: vp.frames_shown >= 8) and vp._playing and not vp.screen.pixmap().isNull()
    assert vp.b_play.text().endswith("Pause") and vp._pos > 0
    vp.pause()
    n = vp.frames_shown; _pump(lambda: False, 0.4)
    assert not vp._playing and vp.frames_shown == n and vp.b_play.text().endswith("Play")     # paused: decoding really stopped
    vp.seek(0.5)
    assert abs(vp._pos - 1.5) < 0.1 and "0:01" in vp.tlabel.text()
    vp.play()
    assert _pump(lambda: vp.frames_shown > n + 4) and vp._pos >= 1.5
    vp.seek(0.95)                                                                               # near the end: plays out
    assert _pump(lambda: not vp._playing, 6) and vp.b_play.text().endswith("Replay")
    vp.toggle()                                                                                 # replay from the start
    assert _pump(lambda: vp._playing and vp._pos < 1.0, 3)
    vp.hide(); assert not vp._playing                                                           # leaving the page stops decoding


def test_preview_pane_shows_photo_video_and_page_with_details(tmp_path):
    from PIL import Image
    from laptop_agent.gui import PreviewPane
    pp = PreviewPane(); pp.resize(800, 600); pp.show()
    Image.new("RGB", (640, 360), (200, 60, 60)).save(tmp_path / "photo.png")
    (tmp_path / "p.html").write_text("<h1>Hi</h1>")
    ok, msg = pp.show_plan({"path": str(tmp_path / "photo.png")}, {"agent": "Nova", "ts": time.time()})
    assert ok and "Photo" in pp.meta.text() and "640×360" in pp.meta.text() and "from Nova" in pp.meta.text()
    assert pp.title.text() == "photo.png" and pp.b_fit.isVisible() and pp.b_folder.isVisible()
    pp._toggle_fit(); assert pp.b_fit.text() == "Fit" and pp.img.pixmap().width() == 640                 # 100% view
    ok, msg = pp.show_plan({"path": str(_make_video(tmp_path))})
    assert ok and "playing video" in msg and "Video" in pp.meta.text() and "0:03" in pp.meta.text() and "silent" in pp.meta.text()
    assert pp.stack.currentWidget() is pp.video and _pump(lambda: pp.video.frames_shown >= 3)
    ok, msg = pp.show_plan({"path": str(tmp_path / "p.html")})                                         # switching away stops the video
    assert ok and not pp.video._playing and "Web page" in pp.meta.text()


def test_media_library_and_chat_thumbnails(local, tmp_path):
    from PIL import Image
    from laptop_agent.gui import Bridge, MainWindow
    from laptop_agent.localserver import LocalServer
    eng, _ = local
    Image.new("RGB", (300, 200), (10, 90, 200)).save(tmp_path / "a.png")
    v = _make_video(tmp_path, 2)
    eng.router.agents = {"nova": {"agent_id": "nova", "name": "Nova", "online": True}}
    eng.store.add_media(str(tmp_path / "a.png"), "image", "nova", "app", "write_file")
    eng.store.add_media(str(v), "video", "nova", "app", "download_file")
    w = MainWindow(eng, Bridge(), LocalServer(eng, 8765)); w.resize(1100, 760); w.show()
    lib = w.preview_page.lib
    assert lib.count() == 2 and {lib.item(i).text() for i in range(2)} == {"a.png", "clip.mp4"}
    w.preview_page.filter.setCurrentIndex(2)                                                            # Videos only
    assert [lib.item(i).isHidden() for i in range(lib.count())].count(True) == 1
    w.preview_page.filter.setCurrentIndex(0)
    w.preview_page.open_path(str(tmp_path / "a.png")); assert w.preview.title.text() == "a.png"
    # a photo arriving from an agent: library + inline chat thumbnail + attention dots
    Image.new("RGB", (200, 100), (200, 200, 10)).save(tmp_path / "new.png")
    row = eng.store.add_media(str(tmp_path / "new.png"), "image", "nova", "app", "write_file")
    eng.store.add_message("app", "media", str(tmp_path / "new.png"), agent="nova")
    w._on_event("media", row); w._on_event("chat", {"who": "media", "text": str(tmp_path / "new.png"), "channel": "app", "agent": "nova"})
    assert lib.count() == 3 and lib.item(0).text() == "new.png" and w.nav["preview"].text().endswith("●")
    assert str(tmp_path / "new.png") in w.chatview._media_labels and w.nav["chat"].text().endswith("●")
    w._open_media(str(tmp_path / "new.png"))                                                           # click the chat thumbnail
    assert w.pages.currentIndex() == w._page_idx["preview"] and w.preview.title.text() == "new.png" and w.nav["preview"].text() == "Preview"
    # video poster thumbnails arrive asynchronously
    assert _pump(lambda: str(v) in w.thumbs._video)
