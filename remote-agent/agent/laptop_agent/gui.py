"""PySide6 desktop application. All approvals AND security-level changes happen here only."""
import json
import threading
import time

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QDialog, QFrame, QGridLayout,
                               QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
                               QPushButton, QScrollArea, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

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


class Bridge(QObject):
    """Engine threads -> Qt main thread."""
    event = Signal(str, object)
    approval = Signal(object, object)


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


STATE_LABEL = {"connected": "Connected", "reconnecting": "Reconnecting…", "paused": "Paused", "stopped": "Stopped",
               "disconnected": "Disconnected", "auth_failed": "Access revoked"}


class MainWindow(QMainWindow):
    def __init__(self, engine: AgentEngine, bridge: Bridge, server: LocalServer):
        super().__init__()
        self.engine, self.server = engine, server
        self.setWindowTitle("Laptop Agent")
        self.resize(1040, 760)
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(14)

        # header
        head = QHBoxLayout()
        head.addWidget(_label("Laptop Agent", "title"))
        head.addStretch(1)
        self.status = QLabel()
        self.status.setObjectName("pill")
        head.addWidget(self.status)
        lay.addLayout(head)

        # info card
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

        # controls + security
        row = QHBoxLayout()
        row.setSpacing(10)
        self.b_start, self.b_pause, self.b_disc, self.b_web = (
            QPushButton("Start"), QPushButton("Pause"), QPushButton("Disconnect"), QPushButton("Open web UI"))
        self.b_start.setObjectName("primary")
        self.b_disc.setObjectName("danger")
        for b in (self.b_start, self.b_pause, self.b_disc, self.b_web):
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)

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
        self.level_group.idClicked.connect(self._level_clicked)

        # tabs
        self.tabs = QTabWidget()
        lay.addWidget(self.tabs, 1)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Time", "Request", "Task", "Ask?", "Status"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setColumnWidth(0, 90)
        self.table.setColumnWidth(1, 200)
        self.table.setColumnWidth(2, 160)
        self.table.setColumnWidth(3, 90)
        self.terminal, self.notices, self.chatlog = QPlainTextEdit(), QPlainTextEdit(), QPlainTextEdit()
        for t in (self.terminal, self.notices, self.chatlog):
            t.setReadOnly(True)
            t.setMaximumBlockCount(3000)
        chat = QWidget()
        cl = QVBoxLayout(chat)
        cl.addWidget(self.chatlog)
        self.chat_in = QLineEdit()
        self.chat_in.setPlaceholderText("Fixed commands only — type 'help'. This cannot run commands.")
        cl.addWidget(self.chat_in)
        for name, wid in (("Activity", self.table), ("Terminal", self.terminal),
                          ("Notifications", self.notices), ("Chat", chat)):
            self.tabs.addTab(wid, name)

        self.b_start.clicked.connect(self.engine.start)
        self.b_pause.clicked.connect(self._toggle_pause)
        self.b_disc.clicked.connect(lambda: threading.Thread(target=self.engine.disconnect, daemon=True).start())
        self.b_web.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(self.server.url)))
        self.chat_in.returnPressed.connect(self._chat)
        bridge.event.connect(self._on_event)
        bridge.approval.connect(self._on_approval)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(700)
        self.refresh()

    # ----------------------------------------------------------------- actions
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
        text = self.chat_in.text()
        self.chat_in.clear()
        self.chatlog.appendPlainText(f"> {text}")
        self.chatlog.appendPlainText(chat_reply(self.engine, text))

    def _on_event(self, kind, data):
        if kind == "notice":
            self.notices.appendPlainText(f"{data['level'].upper()}: {data['msg']}")
            if data["level"] == "error":
                QApplication.alert(self)

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
    server = LocalServer(engine, port)
    server.start()
    win = MainWindow(engine, bridge, server)
    win.show()
    engine.start()
    print(f"Local UI: http://127.0.0.1:{port}/  (use the 'Open web UI' button to authenticate)")
    return app.exec()
