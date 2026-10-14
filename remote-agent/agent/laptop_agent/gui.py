"""PySide6 desktop application. All approvals are answered here, on the native event loop."""
import threading
import time

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
                               QPlainTextEdit, QPushButton, QTableWidget, QTableWidgetItem, QTabWidget,
                               QVBoxLayout, QWidget)

from .engine import AgentEngine, ApprovalRequest
from .localserver import LocalServer, chat_reply


class ApprovalDialog(QDialog):
    """Buttons: APPROVE ONCE / REJECT / CANCEL. Default and Escape = REJECT. No timeout."""
    def __init__(self, req: ApprovalRequest, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Permission required")
        self.setModal(True)
        self.setMinimumWidth(640)
        self.answer = "cancel"
        lay = QVBoxLayout(self)
        rows = [("Operation", req.operation), ("Arguments", _fmt_args(req.arguments)),
                ("Working directory", req.cwd or "-"), ("Files affected", "\n".join(req.files) or "-"),
                ("Reason (from Brain)", req.reason), ("Expected impact", req.impact),
                ("Request ID", req.request_id)]
        for k, v in rows:
            lbl = QLabel(f"<b>{k}</b>")
            body = QPlainTextEdit(v)
            body.setReadOnly(True)
            body.setMaximumHeight(110 if k == "Arguments" else 60)
            lay.addWidget(lbl)
            lay.addWidget(body)
        if req.preview:
            lay.addWidget(QLabel("<b>Content / script preview</b>"))
            pv = QPlainTextEdit(req.preview)
            pv.setReadOnly(True)
            pv.setMaximumHeight(160)
            lay.addWidget(pv)
        bar = QHBoxLayout()
        self.b_ok, self.b_no, self.b_cancel = (QPushButton("APPROVE ONCE"), QPushButton("REJECT"),
                                               QPushButton("CANCEL"))
        for b in (self.b_ok, self.b_no, self.b_cancel):
            b.setAutoDefault(False)
            bar.addWidget(b)
        lay.addLayout(bar)
        self.b_no.setDefault(True)
        self.b_no.setFocus()
        self.b_ok.clicked.connect(lambda: self._done("approve"))
        self.b_no.clicked.connect(lambda: self._done("reject"))
        self.b_cancel.clicked.connect(lambda: self._done("cancel"))

    def _done(self, a: str):
        self.answer = a
        self.accept()

    def reject(self):  # Esc / window close => REJECT-equivalent (never approve)
        self.answer = "reject" if self.answer != "cancel" else "cancel"
        super().reject()


def _fmt_args(a: dict) -> str:
    import json
    s = json.dumps(a, indent=2, ensure_ascii=False)
    return s if len(s) < 4000 else s[:4000] + "\n…(truncated)"


class Bridge(QObject):
    """Engine threads -> Qt main thread."""
    event = Signal(str, object)
    approval = Signal(object, object)   # (req, holder)


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


class MainWindow(QMainWindow):
    def __init__(self, engine: AgentEngine, bridge: Bridge, server: LocalServer):
        super().__init__()
        self.engine, self.server = engine, server
        self.setWindowTitle("Laptop Agent")
        self.resize(960, 700)
        w = QWidget()
        self.setCentralWidget(w)
        lay = QVBoxLayout(w)
        self.status = QLabel()
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.status)
        bar = QHBoxLayout()
        self.b_start, self.b_pause, self.b_disc, self.b_web = (QPushButton("Start"), QPushButton("Pause"),
                                                                QPushButton("Disconnect"), QPushButton("Open web UI"))
        for b in (self.b_start, self.b_pause, self.b_disc, self.b_web):
            bar.addWidget(b)
        lay.addLayout(bar)
        self.tabs = QTabWidget()
        lay.addWidget(self.tabs)
        self.incoming, self.history = self._table(), self._table()
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
        for name, wid in (("Incoming / active", self.incoming), ("History", self.history),
                          ("Terminal output", self.terminal), ("Notifications", self.notices), ("Chat", chat)):
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

    @staticmethod
    def _table():
        t = QTableWidget(0, 5)
        t.setHorizontalHeaderLabels(["Request", "Type", "Risk", "Status", "Reason"])
        t.horizontalHeader().setStretchLastSection(True)
        t.setEditTriggers(QTableWidget.NoEditTriggers)
        return t

    def _toggle_pause(self):
        (self.engine.resume if self.engine.paused else self.engine.pause)()

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

    def refresh(self):
        s = self.engine.snapshot()
        self.status.setText(f"<b>Connection:</b> {s['state']}{' (paused)' if s['paused'] else ''} &nbsp; "
                            f"<b>Relay:</b> {s['relay_url']} &nbsp; <b>Device:</b> {s['device_id']} &nbsp; "
                            f"<b>Active:</b> {s['active'] or '-'}")
        self.b_pause.setText("Resume" if s["paused"] else "Pause")
        done = {"completed", "failed", "timeout", "cancelled", "rejected", "blocked", "aborted"}
        self._fill(self.incoming, [t for t in s["tasks"] if t.get("status") not in done])
        self._fill(self.history, [t for t in s["tasks"] if t.get("status") in done])
        self.terminal.setPlainText("\n".join(s["terminal"]))

    @staticmethod
    def _fill(table, rows):
        table.setRowCount(len(rows))
        for i, t in enumerate(rows):
            for j, k in enumerate(("request_id", "task_type", "risk", "status", "reason")):
                table.setItem(i, j, QTableWidgetItem(str(t.get(k, "") or "")))

    def closeEvent(self, e):
        self.engine.shutdown()
        self.server.stop()
        e.accept()


def run_gui(engine_factory, port: int) -> int:
    app = QApplication.instance() or QApplication([])
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
