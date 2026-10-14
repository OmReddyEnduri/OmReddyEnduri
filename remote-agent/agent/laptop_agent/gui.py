"""PySide6 desktop application. All approvals AND security-level changes happen here only."""
import html as _html
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QDialog, QFrame, QGridLayout,
                               QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
                               QPushButton, QScrollArea, QStackedWidget, QTableWidget, QTableWidgetItem, QTextBrowser,
                               QVBoxLayout, QWidget)

from . import theme
from .engine import AgentEngine, ApprovalRequest
from .localserver import LocalServer, chat_reply
from .policy import LEVEL_HELP, LEVEL_NAMES


def _card() -> QFrame:
    f = QFrame()
    f.setObjectName("card")
    return f


def _label(text: str, name: str = "", wrap: bool = False) -> QLabel:
    lb = QLabel(text)
    if name:
        lb.setObjectName(name)
    lb.setWordWrap(wrap)
    lb.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return lb


def _fmt_args(a: dict) -> str:
    s = json.dumps(a, indent=2, ensure_ascii=False)
    return s if len(s) < 4000 else s[:4000] + "\n…(truncated)"


class ApprovalDialog(QDialog):
    """APPROVE ONCE / REJECT / CANCEL. Default and Esc = REJECT. No timeout, ever.
    Details scroll in the middle; the three buttons are pinned at the bottom and always visible."""
    def __init__(self, req: ApprovalRequest, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Permission required")
        self.setModal(True)
        self.answer = "cancel"
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 16)
        outer.setSpacing(10)

        head = QHBoxLayout()
        icon = _label("⚠")
        icon.setStyleSheet(f"color:{theme.WARN};font-size:24px;")
        head.addWidget(icon)
        col = QVBoxLayout()
        col.setSpacing(0)
        t = _label("Allow this on your laptop?", "title")
        t.setStyleSheet("font-size:16px;")
        col.addWidget(t)
        col.addWidget(_label(f"{req.operation}  ·  {req.reason}", "muted", wrap=True))
        head.addLayout(col, 1)
        outer.addLayout(head)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        body = QVBoxLayout(inner)
        body.setContentsMargins(0, 0, 6, 0)
        body.setSpacing(6)

        def field(name, text, cap=140):
            body.addWidget(_label(name.upper(), "caption"))
            box = QPlainTextEdit(text)
            box.setReadOnly(True)
            lines = min(text.count("\n") + 1, 9)
            box.setFixedHeight(min(cap, 18 * lines + 22))
            if lines <= 2 and len(text) < 70:
                box.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            body.addWidget(box)
        field("Operation", req.operation)
        field("Arguments", _fmt_args(req.arguments), cap=170)
        field("Working directory", req.cwd or "—")
        field("Files affected", "\n".join(req.files) or "—", cap=110)
        field("Reason (from the Brain)", req.reason)
        field("Expected impact", req.impact)
        if req.preview:
            field("Content / script preview", req.preview, cap=170)
        body.addWidget(_label(f"Request ID: {req.request_id}", "muted"))
        body.addStretch(1)
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)

        bar = QHBoxLayout()
        self.b_ok, self.b_no, self.b_cancel = (QPushButton("APPROVE ONCE"), QPushButton("REJECT"), QPushButton("CANCEL"))
        self.b_ok.setObjectName("approve")
        self.b_no.setObjectName("reject")
        self.b_cancel.setObjectName("neutral")
        for b in (self.b_ok, self.b_no, self.b_cancel):
            b.setAutoDefault(False)
            b.setMinimumHeight(44)
            bar.addWidget(b)
        outer.addLayout(bar)
        self.b_no.setDefault(True)
        self.b_no.setFocus()
        self.b_ok.clicked.connect(lambda: self._done("approve"))
        self.b_no.clicked.connect(lambda: self._done("reject"))
        self.b_cancel.clicked.connect(lambda: self._done("cancel"))

        screen = (parent.screen() if parent else QApplication.primaryScreen()).availableGeometry()
        self.resize(min(660, int(screen.width() * 0.92)), min(560, int(screen.height() * 0.88)))
        self.move(screen.center().x() - self.width() // 2, screen.center().y() - self.height() // 2)

    def _done(self, a: str):
        self.answer = a
        self.accept()

    def reject(self):  # Esc / window close => never approve
        self.answer = "reject" if self.answer != "cancel" else "cancel"
        super().reject()


try:  # optional extras (pip install PySide6-Addons): full web/JS, video and PDF previews inside the app
    from PySide6.QtWebEngineWidgets import QWebEngineView
except Exception:  # noqa
    QWebEngineView = None
try:
    from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
    from PySide6.QtMultimediaWidgets import QVideoWidget
except Exception:  # noqa
    QMediaPlayer = None

IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
VID_EXT = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}


class Bridge(QObject):
    """Engine threads -> Qt main thread."""
    event = Signal(str, object)
    approval = Signal(object, object)
    preview = Signal(object, object)


class QtApprover:
    def __init__(self, bridge: Bridge):
        self.bridge = bridge

    def ask(self, req, abort) -> str:
        holder = {"ev": threading.Event(), "answer": "cancel"}
        self.bridge.approval.emit(req, holder)
        while not holder["ev"].wait(0.1):
            if abort.is_set():
                holder["abort"] = True
                dlg = holder.get("dlg")
                if dlg is not None:
                    QTimer.singleShot(0, dlg.reject)
                holder["ev"].wait(2)
                return "cancel"
        return holder["answer"]


class PreviewPane(QWidget):
    """Shows what the Brain wants you to see, inside this app (images, text, markdown, html, video poster)."""
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        self.title = _label("Nothing to preview yet", "muted")
        top.addWidget(self.title, 1)
        self.b_ext = QPushButton("Open in browser")
        self.b_ext.setVisible(False)
        top.addWidget(self.b_ext)
        lay.addLayout(top)
        self.stack = QStackedWidget()
        lay.addWidget(self.stack, 1)
        self.msg = _label("The Brain will show images, pages and videos here.", "muted", wrap=True)
        self.msg.setAlignment(Qt.AlignCenter)
        self.img = QLabel()
        self.img.setAlignment(Qt.AlignCenter)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(self.img)
        self.scroll.viewport().setStyleSheet(f"background:{theme.BG};")
        self.img.setStyleSheet(f"background:{theme.BG};")
        self.text = QTextBrowser()
        self.text.setOpenExternalLinks(False)
        for w in (self.msg, self.scroll, self.text):
            self.stack.addWidget(w)
        self.web = None
        if QWebEngineView:
            self.web = QWebEngineView()
            self.stack.addWidget(self.web)
        self._pix = None
        self._target = None
        self.b_ext.clicked.connect(lambda: self._target and QDesktopServices.openUrl(QUrl.fromUserInput(self._target)))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit()

    def _fit(self):
        if self._pix is not None and not self._pix.isNull():
            w = max(self.scroll.viewport().width() - 8, 100)
            self.img.setPixmap(self._pix if self._pix.width() <= w else self._pix.scaledToWidth(w, Qt.SmoothTransformation))

    def _show_image(self, pix: QPixmap) -> bool:
        if pix.isNull():
            return False
        self._pix = pix
        self.stack.setCurrentWidget(self.scroll)
        self._fit()
        return True

    def show_plan(self, plan: dict) -> tuple[bool, str]:
        """Called on the GUI thread. Returns (ok, message for the Brain)."""
        try:
            return self._show(plan)
        except Exception as e:  # never let a bad file crash the app
            return False, f"preview failed: {type(e).__name__}: {e}"

    def _show(self, plan):
        url, path = plan.get("url"), plan.get("path")
        self._target = url or (QUrl.fromLocalFile(path).toString() if path else None)
        self.b_ext.setVisible(True)
        if url:
            self.title.setText(url)
            if self.web:
                self.web.load(QUrl(url))
                self.stack.setCurrentWidget(self.web)
                return True, f"showing {url} in the Laptop Agent app (live page)"
            import httpx
            r = httpx.get(url, timeout=6, follow_redirects=True)
            self.text.setHtml(r.text)
            self.stack.setCurrentWidget(self.text)
            return True, (f"showing a static snapshot of {url} in the app (no JavaScript). "
                          "For full live pages the user can run: pip install PySide6-Addons")
        ext = os.path.splitext(path)[1].lower()
        self.title.setText(path)
        if ext in IMG_EXT:
            if not self._show_image(QPixmap(path)):
                return False, "could not decode this image"
            return True, f"showing image {path} in the Laptop Agent app"
        if ext in (".html", ".htm"):
            if self.web:
                self.web.load(QUrl.fromLocalFile(path))
                self.stack.setCurrentWidget(self.web)
                return True, f"showing {path} in the app (full web view)"
            self.text.setSource(QUrl.fromLocalFile(path))
            self.stack.setCurrentWidget(self.text)
            return True, (f"showing {path} in the app with basic HTML/CSS only (no JavaScript, so React/JSX apps "
                          "will not run). For full previews the user can run: pip install PySide6-Addons")
        if ext == ".md":
            self.text.setMarkdown(open(path, encoding="utf-8", errors="replace").read(400_000))
            self.stack.setCurrentWidget(self.text)
            return True, f"showing {path} in the app"
        if ext == ".txt":
            self.text.setPlainText(open(path, encoding="utf-8", errors="replace").read(400_000))
            self.stack.setCurrentWidget(self.text)
            return True, f"showing {path} in the app"
        if ext in VID_EXT:
            return self._show_video(path)
        if ext == ".pdf":
            if self.web:
                self.web.load(QUrl.fromLocalFile(path))
                self.stack.setCurrentWidget(self.web)
                return True, f"showing {path} in the app"
            self.msg.setText("PDF preview needs the optional add-on:  pip install PySide6-Addons")
            self.stack.setCurrentWidget(self.msg)
            return False, "PDF preview needs PySide6-Addons (pip install PySide6-Addons)"
        return False, f"cannot preview {ext} files in the app"

    def _show_video(self, path):
        if QMediaPlayer:
            if not hasattr(self, "video"):
                self.video, self.player, self.audio = QVideoWidget(), QMediaPlayer(self), QAudioOutput(self)
                self.player.setAudioOutput(self.audio)
                self.player.setVideoOutput(self.video)
                self.stack.addWidget(self.video)
            self.player.setSource(QUrl.fromLocalFile(path))
            self.stack.setCurrentWidget(self.video)
            self.player.play()
            return True, f"playing {path} in the app"
        ff = shutil.which("ffmpeg")
        if ff:
            with tempfile.TemporaryDirectory() as td:
                out = os.path.join(td, "poster.jpg")
                subprocess.run([ff, "-v", "error", "-ss", "1", "-i", path, "-frames:v", "1", out], capture_output=True, timeout=30)
                if os.path.exists(out) and self._show_image(QPixmap(out)):
                    return True, (f"showing a still frame of {path} (the app cannot play video without "
                                  "PySide6-Addons: pip install PySide6-Addons)")
        self.msg.setText("Video playback needs the optional add-on:  pip install PySide6-Addons")
        self.stack.setCurrentWidget(self.msg)
        return False, "video preview needs PySide6-Addons or ffmpeg"


STATE_LABEL = {"connected": "Connected", "reconnecting": "Reconnecting…", "paused": "Paused", "stopped": "Stopped",
               "disconnected": "Disconnected", "auth_failed": "Access revoked"}
NAV = [("activity", "Activity"), ("preview", "Preview"), ("chat", "Chat"), ("terminal", "Terminal"),
       ("notices", "Notifications"), ("connect", "Connect")]


class MainWindow(QMainWindow):
    def __init__(self, engine: AgentEngine, bridge: Bridge, server: LocalServer):
        super().__init__()
        self.engine, self.server = engine, server
        self.setWindowTitle("Laptop Agent")
        self.resize(1120, 780)
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ---------------- sidebar
        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(210)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(14, 18, 14, 16)
        sl.setSpacing(4)
        sl.addWidget(_label("Laptop Agent", "title"))
        self.status = QLabel()
        self.status.setObjectName("pill")
        self.status.setAlignment(Qt.AlignCenter)
        sl.addWidget(self.status)
        sl.addSpacing(14)
        self.pages = QStackedWidget()
        self.nav_group = QButtonGroup(self)
        self.nav, self._page_idx = {}, {}
        for i, (key, name) in enumerate(NAV):
            b = QPushButton(name)
            b.setObjectName("nav")
            b.setCheckable(True)
            self.nav_group.addButton(b, i)
            self.nav[key] = b
            self._page_idx[key] = i
            sl.addWidget(b)
        sl.addStretch(1)
        self.b_start, self.b_pause, self.b_disc, self.b_web = (
            QPushButton("Start"), QPushButton("Pause"), QPushButton("Disconnect"), QPushButton("Open web UI"))
        self.b_start.setObjectName("primary")
        self.b_disc.setObjectName("danger")
        for b in (self.b_start, self.b_pause, self.b_disc, self.b_web):
            sl.addWidget(b)
        outer.addWidget(side)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(24, 20, 24, 20)
        rl.addWidget(self.pages)
        outer.addWidget(right, 1)

        self.pages.addWidget(self._build_activity())
        self.preview = PreviewPane()
        self.pages.addWidget(self._wrap("Preview", self.preview))
        self.pages.addWidget(self._build_chat())
        self.terminal = QPlainTextEdit()
        self.terminal.setReadOnly(True)
        self.terminal.setMaximumBlockCount(3000)
        self.pages.addWidget(self._wrap("Terminal", self.terminal))
        self.notices = QPlainTextEdit()
        self.notices.setReadOnly(True)
        self.notices.setMaximumBlockCount(1000)
        self.pages.addWidget(self._wrap("Notifications", self.notices))
        self.pages.addWidget(self._build_connect())

        self.nav_group.idClicked.connect(self._nav)
        self.nav["activity"].setChecked(True)
        self.level_group.idClicked.connect(self._level_clicked)
        self.b_start.clicked.connect(self.engine.start)
        self.b_pause.clicked.connect(self._toggle_pause)
        self.b_disc.clicked.connect(lambda: threading.Thread(target=self.engine.disconnect, daemon=True).start())
        self.b_web.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(self.server.url)))
        self.chat_in.returnPressed.connect(self._chat)
        self.b_send.clicked.connect(self._chat)
        self.b_stop.clicked.connect(self._stop_brain)
        bridge.event.connect(self._on_event)
        bridge.approval.connect(self._on_approval)
        bridge.preview.connect(self._on_preview)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(700)
        self._chat_items: list = []
        self._render_chat()
        self.refresh()

    # ----------------------------------------------------------------- pages
    @staticmethod
    def _wrap(title: str, w: QWidget) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(_label(title, "title"))
        lay.addWidget(w, 1)
        return page

    def _build_activity(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)
        lay.addWidget(_label("Activity", "title"))
        info = _card()
        g = QGridLayout(info)
        g.setContentsMargins(18, 14, 18, 14)
        g.setHorizontalSpacing(28)
        self.v_relay, self.v_dev, self.v_ws, self.v_active = (_label("", "value") for _ in range(4))
        for i, (cap, w) in enumerate((("RELAY", self.v_relay), ("DEVICE", self.v_dev),
                                      ("WORKSPACE", self.v_ws), ("ACTIVE TASK", self.v_active))):
            g.addWidget(_label(cap, "caption"), 0, i)
            g.addWidget(w, 1, i)
        lay.addWidget(info)
        sec = _card()
        sl = QHBoxLayout(sec)
        sl.setContentsMargins(18, 14, 18, 14)
        left = QVBoxLayout()
        left.addWidget(_label("ASK LEVEL  (how often I ask before acting)", "caption"))
        self.level_help = _label("", "muted", wrap=True)
        left.addWidget(self.level_help)
        sl.addLayout(left, 1)
        self.level_group = QButtonGroup(self)
        self.level_btns = {}
        seg = QHBoxLayout()
        seg.setSpacing(0)
        for n, name, oname in ((1, "1  Low", "segL"), (2, "2  Medium", "seg"), (3, "3  High", "segR")):
            b = QPushButton(name)
            b.setObjectName(oname)
            b.setCheckable(True)
            self.level_group.addButton(b, n)
            self.level_btns[n] = b
            seg.addWidget(b)
        sl.addLayout(seg)
        lay.addWidget(sec)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Time", "Request", "Task", "Ask?", "Status"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        for c, w in enumerate((90, 200, 160, 90)):
            self.table.setColumnWidth(c, w)
        card = _card()
        cl = QVBoxLayout(card)
        cl.setContentsMargins(8, 8, 8, 8)
        cl.addWidget(self.table)
        lay.addWidget(card, 1)
        return page

    def _build_chat(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        head.addWidget(_label("Chat with the Brain", "title"), 1)
        self.b_stop = QPushButton("STOP the Brain")
        self.b_stop.setObjectName("stop")
        self.b_stop.setToolTip("Sends the word \"stop\" to the Brain. It halts all work until you talk to it again.")
        head.addWidget(self.b_stop)
        lay.addLayout(head)
        lay.addWidget(_label("The Brain is always listening. Type normally. Local commands start with / (try /help). "
                             "Chat never runs commands on this laptop.", "muted", wrap=True))
        self.chatview = QTextBrowser()
        lay.addWidget(self.chatview, 1)
        row = QHBoxLayout()
        self.chat_in = QLineEdit()
        self.chat_in.setPlaceholderText("Talk to the Brain…")
        self.b_send = QPushButton("Send")
        self.b_send.setObjectName("primary")
        row.addWidget(self.chat_in, 1)
        row.addWidget(self.b_send)
        lay.addLayout(row)
        return page

    def _build_connect(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        lay.addWidget(_label("Connect", "title"))
        lay.addWidget(_label("Talk to the Brain from any tool on this laptop. The endpoint is OpenAI-compatible; "
                             "it only listens on 127.0.0.1 and needs this key.", "muted", wrap=True))
        card = _card()
        g = QGridLayout(card)
        g.setContentsMargins(18, 14, 18, 14)
        self.v_endpoint, self.v_key, self.v_model = _label("", "value"), _label("", "value"), _label("brain", "value")
        for r, (cap, w) in enumerate((("ENDPOINT (base URL)", self.v_endpoint), ("MODEL", self.v_model),
                                      ("API KEY", self.v_key))):
            g.addWidget(_label(cap, "caption"), r * 2, 0)
            g.addWidget(w, r * 2 + 1, 0)
        self.b_showkey, self.b_copykey, self.b_copyurl = QPushButton("Show"), QPushButton("Copy key"), QPushButton("Copy URL")
        g.addWidget(self.b_copyurl, 1, 1)
        g.addWidget(self.b_showkey, 5, 1)
        g.addWidget(self.b_copykey, 5, 2)
        lay.addWidget(card)
        self.example = QPlainTextEdit()
        self.example.setReadOnly(True)
        self.example.setMaximumHeight(150)
        lay.addWidget(_label("EXAMPLE", "caption"))
        lay.addWidget(self.example)
        lay.addStretch(1)
        self._show_key = False
        self.b_showkey.clicked.connect(self._toggle_key)
        self.b_copykey.clicked.connect(lambda: QApplication.clipboard().setText(self.engine.cfg.local_api_key))
        self.b_copyurl.clicked.connect(lambda: QApplication.clipboard().setText(self.v_endpoint.text()))
        return page

    def _toggle_key(self):
        self._show_key = not self._show_key
        self.b_showkey.setText("Hide" if self._show_key else "Show")
        self._refresh_connect()

    def _refresh_connect(self):
        cfg = self.engine.cfg
        url = f"http://127.0.0.1:{cfg.ui_port}/v1"
        key = cfg.local_api_key or "(generated on first run)"
        self.v_endpoint.setText(url)
        self.v_key.setText(key if self._show_key or not cfg.local_api_key else key[:8] + "•" * 24)
        self.example.setPlainText(
            f"curl {url}/chat/completions \\\n  -H \"Authorization: Bearer <your key>\" -H \"Content-Type: application/json\" \\\n"
            f"  -d '{{\"model\":\"brain\",\"messages\":[{{\"role\":\"user\",\"content\":\"hello\"}}]}}'\n\n"
            f"# Python (openai package): OpenAI(base_url=\"{url}\", api_key=\"<your key>\")")

    # ----------------------------------------------------------------- actions
    def _nav(self, i: int):
        self.pages.setCurrentIndex(i)
        if i == self._page_idx["chat"]:
            self.nav["chat"].setText("Chat")

    def _go(self, key: str):
        self.nav[key].setChecked(True)
        self._nav(self._page_idx[key])

    def _toggle_pause(self):
        (self.engine.resume if self.engine.paused else self.engine.pause)()

    def _level_clicked(self, n: int):
        cur = self.engine.policy.level
        if n == cur:
            return
        if n < cur:  # relaxing needs an explicit confirmation
            ok = QMessageBox.question(
                self, "Lower the ask level?",
                f"Level {n} ({LEVEL_NAMES[n]}): {LEVEL_HELP[n]}\n\nThe Brain will be able to do more without asking. "
                "Blocked actions stay blocked. Continue?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if ok != QMessageBox.Yes:
                self.level_btns[cur].setChecked(True)
                return
        self.engine.set_security_level(n)
        self.refresh()

    def _chat(self):
        text = self.chat_in.text().strip()
        self.chat_in.clear()
        if not text:
            return
        reply = chat_reply(self.engine, text)      # engine echoes your message itself when it goes to the Brain
        if text.startswith("/") or reply != "Sent to the Brain.":
            self._chat_items.append(("sys", reply))
            self._render_chat()

    def _stop_brain(self):
        if self.engine.send_user_message("stop"):
            self._chat_items.append(("sys", "Sent \"stop\" to the Brain."))
        else:
            self._chat_items.append(("sys", "Not connected - press Start first."))
        self._render_chat()

    def _render_chat(self):
        parts = []
        for who, text in self._chat_items:
            t = _html.escape(text).replace("\n", "<br>")
            if who == "you":
                parts.append(f'<table align="right" width="75%" cellpadding="9" style="margin:6px 0"><tr><td bgcolor="{theme.ACCENT}" style="color:white">{t}</td></tr></table><br clear="all">')
            elif who == "brain":
                parts.append(f'<table align="left" width="75%" cellpadding="9" style="margin:6px 0"><tr><td bgcolor="{theme.CARD2}"><b style="color:{theme.MUTED}">Brain</b><br>{t}</td></tr></table><br clear="all">')
            else:
                parts.append(f'<p align="center" style="color:{theme.MUTED}">{t}</p>')
        self.chatview.setHtml("".join(parts) or f'<p align="center" style="color:{theme.MUTED}">No messages yet - say hi.</p>')
        self.chatview.verticalScrollBar().setValue(self.chatview.verticalScrollBar().maximum())

    def _on_event(self, kind, data):
        if kind == "chat":
            self._chat_items.append((data["who"], data["text"]))
            self._render_chat()
            if data["who"] == "brain":
                if self.pages.currentIndex() != self._page_idx["chat"]:
                    self.nav["chat"].setText("Chat  ●")
                QApplication.alert(self)
        if kind == "notice":
            self.notices.appendPlainText(f"{data['level'].upper()}: {data['msg']}")
            if data["level"] == "error":
                QApplication.alert(self)

    def _on_preview(self, plan, holder):
        holder["result"] = self.preview.show_plan(plan)
        if holder["result"][0]:
            self._go("preview")
            self.show()
            self.raise_()
            QApplication.alert(self)
        holder["ev"].set()

    def _on_approval(self, req, holder):
        self.show()
        self.raise_()
        self.activateWindow()
        QApplication.alert(self)
        dlg = ApprovalDialog(req, self)
        holder["dlg"] = dlg
        dlg.exec()
        holder["answer"] = "cancel" if holder.get("abort") else dlg.answer
        holder["ev"].set()

    # ----------------------------------------------------------------- refresh
    def refresh(self):
        s = self.engine.snapshot()
        color = theme.STATUS_COLORS.get(s["state"], theme.MUTED)
        label = "Paused" if s["paused"] and s["state"] in ("connected", "paused") else STATE_LABEL.get(s["state"], s["state"])
        self.status.setText(f"●  {label}")
        self.status.setStyleSheet(f"color:{color};")
        self.v_relay.setText(s["relay_url"])
        self.v_dev.setText(s["device_id"])
        self.v_ws.setText(", ".join(s["workspaces"]))
        self.v_active.setText(s["active"] or "—")
        self.b_pause.setText("Resume" if s["paused"] else "Pause")
        lv = s["security_level"]
        self.level_btns[lv].setChecked(True)
        self.level_help.setText(f"<b>Level {lv} — {LEVEL_NAMES[lv]}.</b> {LEVEL_HELP[lv]}")
        self._fill(s["tasks"])
        self.terminal.setPlainText("\n".join(s["terminal"]))
        self._refresh_connect()

    def _fill(self, rows):
        t = self.table
        t.setRowCount(len(rows))
        for i, r in enumerate(rows):
            vals = (time.strftime("%H:%M:%S", time.localtime(r.get("ts", 0))), r.get("request_id", ""),
                    r.get("task_type", ""), {"auto": "no", "ask": "yes", "block": "blocked"}.get(r.get("risk"), ""),
                    str(r.get("status", "")).replace("_", " "))
            for j, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if j == 4:
                    it.setForeground(QColor(theme.STATUS_COLORS.get(r.get("status"), theme.TEXT)))
                t.setItem(i, j, it)

    def closeEvent(self, e):
        self.engine.shutdown()
        self.server.stop()
        e.accept()


def run_gui(engine_factory, port: int) -> int:
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(theme.QSS)
    bridge = Bridge()
    engine: AgentEngine = engine_factory(QtApprover(bridge))
    engine.add_listener(lambda k, d: bridge.event.emit(k, d))

    def preview(plan):          # engine thread -> GUI thread -> result back
        holder = {"ev": threading.Event()}
        bridge.preview.emit(plan, holder)
        holder["ev"].wait(20)
        return holder.get("result", (False, "the app did not respond"))
    engine.preview_handler = preview
    server = LocalServer(engine, port)
    server.start()
    win = MainWindow(engine, bridge, server)
    win.show()
    engine.start()
    print(f"Local UI: http://127.0.0.1:{port}/  (use the 'Open web UI' button to authenticate)")
    return app.exec()
