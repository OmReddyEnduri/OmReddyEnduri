"""PySide6 desktop application. All approvals AND security-level changes happen here only."""
import html as _html
import json
import os
import shutil
import subprocess
import secrets
import tempfile
import threading
import time

from PySide6.QtCore import QObject, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QDialog, QFrame, QGridLayout,
                               QComboBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit,
                               QPushButton, QScrollArea, QSlider, QStackedWidget, QTableWidget, QTableWidgetItem, QTextBrowser,
                               QVBoxLayout, QWidget)

from . import media_tools as mt
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


VW, VH = 960, 540          # video player frame size (letterboxed)


def _placeholder(w: int, h: int, glyph: str, sub: str = "") -> QPixmap:
    pm = QPixmap(w, h)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("#10161d"))
    p.setPen(QColor(theme.BORDER))
    p.drawRoundedRect(0, 0, w - 1, h - 1, 10, 10)
    p.setPen(QColor(theme.MUTED))
    f = p.font()
    f.setPointSize(max(10, h // 5))
    f.setBold(True)
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignCenter, glyph)
    if sub:
        f.setPointSize(8)
        p.setFont(f)
        p.drawText(pm.rect().adjusted(0, 0, 0, -6), Qt.AlignHCenter | Qt.AlignBottom, sub)
    p.end()
    return pm


def _play_badge(pm: QPixmap) -> QPixmap:
    out = QPixmap(pm)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    r = max(18, min(out.width(), out.height()) // 6)
    cx, cy = out.width() // 2, out.height() // 2
    p.setBrush(QColor(0, 0, 0, 150))
    p.setPen(Qt.NoPen)
    p.drawEllipse(cx - r, cy - r, 2 * r, 2 * r)
    p.setBrush(QColor("white"))
    from PySide6.QtGui import QPolygon
    from PySide6.QtCore import QPoint
    p.drawPolygon(QPolygon([QPoint(cx - r // 3, cy - r // 2), QPoint(cx - r // 3, cy + r // 2), QPoint(cx + r // 2, cy)]))
    p.end()
    return out


class Thumbs(QObject):
    """Thumbnails for the library and the chat. Images load instantly; video posters come from ffmpeg in a thread."""
    ready = Signal(str)

    def __init__(self):
        super().__init__()
        self._video: dict[str, QPixmap] = {}
        self._asked: set[str] = set()

    def pixmap(self, path: str, w: int, h: int, crop: bool = False) -> QPixmap:
        kind = mt.kind_of(path)
        if kind == "image":
            pm = QPixmap(path)
            if not pm.isNull():
                mode = Qt.KeepAspectRatioByExpanding if crop else Qt.KeepAspectRatio
                pm = pm.scaled(w, h, mode, Qt.SmoothTransformation)
                return pm.copy((pm.width() - w) // 2, (pm.height() - h) // 2, w, h) if crop else pm
            return _placeholder(w, h, "IMG", "unreadable")
        if kind == "video":
            pm = self._video.get(path)
            if pm is None:
                self._request(path)
                return _play_badge(_placeholder(w, h, "", "loading…"))
            pm = pm.scaled(w, h, Qt.KeepAspectRatioByExpanding if crop else Qt.KeepAspectRatio, Qt.SmoothTransformation)
            if crop:
                pm = pm.copy((pm.width() - w) // 2, (pm.height() - h) // 2, w, h)
            return _play_badge(pm)
        return _placeholder(w, h, os.path.splitext(path)[1].upper().lstrip(".") or "FILE")

    def _request(self, path: str):
        if path in self._asked:
            return
        self._asked.add(path)

        def work():
            data = mt.poster(path, 1.0, 480)
            if data:
                img = QImage.fromData(data)
                if not img.isNull():
                    self._video[path] = QPixmap.fromImage(img)
            else:
                self._video[path] = _placeholder(240, 135, "VIDEO", "no poster (install imageio-ffmpeg)")
            self.ready.emit(path)
        threading.Thread(target=work, daemon=True).start()


class _FramePump(QObject):
    frame = Signal(int, int, bytes)
    done = Signal(int)


class VideoPlayer(QWidget):
    """In-app video: ffmpeg decodes to raw frames (needs only ffmpeg, which pip can install). Play/pause/seek.
    Silent by design: it is a preview, not a media centre. Use 'Open externally' for sound."""
    FPS = 24

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self.screen = QLabel()
        self.screen.setAlignment(Qt.AlignCenter)
        self.screen.setMinimumSize(320, 180)
        self.screen.setObjectName("videoScreen")
        self.screen.setStyleSheet("#videoScreen { background: #000; border-radius: 10px; }")
        lay.addWidget(self.screen, 1)
        bar = QHBoxLayout()
        self.b_play = QPushButton("▶  Play")
        self.b_play.setObjectName("primary")
        self.b_play.setMinimumWidth(110)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 1000)
        self.tlabel = QLabel("0:00 / 0:00")
        self.tlabel.setMinimumWidth(96)
        bar.addWidget(self.b_play)
        bar.addWidget(self.slider, 1)
        bar.addWidget(self.tlabel)
        lay.addLayout(bar)
        self._pump = _FramePump()
        self._pump.frame.connect(self._on_frame)
        self._pump.done.connect(self._on_done)
        self._path = None
        self._dur = 0.0
        self._pos = 0.0
        self._gen = 0
        self._start = 0.0
        self._proc = None
        self._playing = False
        self._pix = None
        self.frames_shown = 0
        self.b_play.clicked.connect(self.toggle)
        self.slider.sliderReleased.connect(lambda: self.seek(self.slider.value() / 1000))
        self.slider.sliderMoved.connect(lambda v: self._time(v / 1000 * self._dur))

    # ---- control
    def load(self, path: str, autoplay: bool = True) -> dict:
        self.pause()
        self._path, self._pos, self.frames_shown = path, 0.0, 0
        info = mt.probe(path)
        self._dur = info["duration"]
        self._poster(0.5 if self._dur > 1 else 0)
        self._update_ui()
        if autoplay and mt.ffmpeg_path():
            self.play()
        return info

    def toggle(self):
        self.pause() if self._playing else self.play()

    def play(self):
        if self._playing or not self._path:
            return
        ff = mt.ffmpeg_path()
        if not ff:
            self.screen.setText("Video needs ffmpeg.  Run:  pip install imageio-ffmpeg")
            return
        if self._dur and self._pos >= self._dur - 0.15:
            self._pos = 0.0
        self._playing = True
        self.b_play.setText("⏸  Pause")
        self._gen += 1
        self._start = self._pos
        threading.Thread(target=self._decode, args=(self._gen, ff, self._path, self._pos), daemon=True).start()

    def pause(self):
        self._gen += 1                       # invalidates the decoder thread
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                proc.kill()
            except OSError:
                pass
        self._playing = False
        self.b_play.setText("▶  Play")

    def seek(self, frac: float):
        was = self._playing
        self.pause()
        self._pos = max(0.0, min(frac, 1.0)) * self._dur
        self._poster(self._pos)
        self._update_ui()
        if was:
            self.play()

    # ---- decoding (worker thread)
    def _decode(self, gen: int, ff: str, path: str, start: float):
        vf = (f"fps={self.FPS},scale={VW}:{VH}:force_original_aspect_ratio=decrease,"
              f"pad={VW}:{VH}:(ow-iw)/2:(oh-ih)/2:color=black")
        cmd = [ff, "-v", "error", "-re", "-ss", f"{start:.2f}", "-i", path, "-an", "-vf", vf,
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **mt.NOWINDOW)
        except OSError:
            self._pump.done.emit(gen)
            return
        self._proc = proc
        size, n = VW * VH * 3, 0
        try:
            while gen == self._gen:
                buf = proc.stdout.read(size)
                if len(buf) < size:
                    break
                self._pump.frame.emit(gen, n, bytes(buf))
                n += 1
        finally:
            try:
                proc.kill()
            except OSError:
                pass
        if gen == self._gen:
            self._pump.done.emit(gen)

    # ---- UI thread
    def _on_frame(self, gen: int, n: int, data: bytes):
        if gen != self._gen:
            return
        img = QImage(data, VW, VH, VW * 3, QImage.Format_RGB888).copy()
        self._pix = QPixmap.fromImage(img)
        self.frames_shown += 1
        self._fit()
        self._pos = self._start + n / self.FPS
        self._update_ui()

    def _on_done(self, gen: int):
        if gen != self._gen:
            return
        self._playing = False
        self._proc = None
        self.b_play.setText("↻  Replay")
        if self._dur:
            self._pos = self._dur
            self._update_ui()

    def _poster(self, t: float):
        data = mt.poster(self._path, t, VW)
        if data:
            img = QImage.fromData(data)
            if not img.isNull():
                self._pix = QPixmap.fromImage(img)
                self._fit()

    def _fit(self):
        if self._pix is not None:
            self.screen.setPixmap(self._pix.scaled(self.screen.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _time(self, t: float):
        self.tlabel.setText(f"{mt.human_time(t)} / {mt.human_time(self._dur)}")

    def _update_ui(self):
        self._time(self._pos)
        if self._dur and not self.slider.isSliderDown():
            self.slider.blockSignals(True)
            self.slider.setValue(int(1000 * min(self._pos, self._dur) / self._dur))
            self.slider.blockSignals(False)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit()

    def hideEvent(self, e):                  # leaving the Preview page stops decoding
        self.pause()
        super().hideEvent(e)


class PreviewPane(QWidget):
    """The viewer: file header (name, details, actions) over the content (image / page / video)."""
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        head = _card()
        hl = QHBoxLayout(head)
        hl.setContentsMargins(16, 12, 16, 12)
        col = QVBoxLayout()
        col.setSpacing(2)
        self.title = QLabel("Nothing to preview yet")
        self.title.setStyleSheet("font-size:15px; font-weight:700;")
        self.title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.meta = _label("Photos, videos and pages the agents show you appear here.", "muted", wrap=True)
        col.addWidget(self.title)
        col.addWidget(self.meta)
        hl.addLayout(col, 1)
        self.b_fit = QPushButton("100%")
        self.b_folder, self.b_ext, self.b_copy = QPushButton("Show in folder"), QPushButton("Open externally"), QPushButton("Copy path")
        for b in (self.b_fit, self.b_folder, self.b_ext, self.b_copy):
            hl.addWidget(b)
        lay.addWidget(head)
        self.stack = QStackedWidget()
        lay.addWidget(self.stack, 1)
        self.msg = _label("", "muted", wrap=True)
        self.msg.setAlignment(Qt.AlignCenter)
        self.img = QLabel()
        self.img.setAlignment(Qt.AlignCenter)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(self.img)
        self.scroll.viewport().setObjectName("imgVp")
        self.scroll.viewport().setStyleSheet(f"#imgVp {{ background: {theme.BG}; }}")
        self.scroll.setStyleSheet(f"QScrollArea {{ border: 1px solid {theme.BORDER}; border-radius: 12px; }}")
        self.text = QTextBrowser()
        self.text.setOpenExternalLinks(False)
        self.video = VideoPlayer()
        for w in (self.msg, self.scroll, self.text, self.video):
            self.stack.addWidget(w)
        self.web = None
        if QWebEngineView:
            self.web = QWebEngineView()
            self.stack.addWidget(self.web)
        self._pix = None
        self._target = None          # file path or url
        self._fit_mode = True
        self._set_empty()
        self.b_fit.clicked.connect(self._toggle_fit)
        self.b_ext.clicked.connect(lambda: self._target and QDesktopServices.openUrl(
            QUrl(self._target) if "://" in self._target else QUrl.fromLocalFile(self._target)))
        self.b_folder.clicked.connect(lambda: self._target and os.path.exists(self._target) and QDesktopServices.openUrl(
            QUrl.fromLocalFile(os.path.dirname(self._target))))
        self.b_copy.clicked.connect(lambda: self._target and QApplication.clipboard().setText(self._target))

    # ---- header
    def _set_empty(self):
        self.title.setText("Nothing to preview yet")
        self.meta.setText("Photos, videos and pages the agents show you appear here, and in the library on the left.")
        for b in (self.b_fit, self.b_folder, self.b_ext, self.b_copy):
            b.setVisible(False)
        self.msg.setText("Ask an agent to build or show you something.")
        self.stack.setCurrentWidget(self.msg)

    def _header(self, name: str, details: list[str], *, path: bool, fit: bool):
        self.title.setText(name)
        self.meta.setText("  ·  ".join(d for d in details if d))
        self.b_fit.setVisible(fit)
        self.b_folder.setVisible(path)
        self.b_ext.setVisible(True)
        self.b_copy.setVisible(True)

    def _toggle_fit(self):
        self._fit_mode = not self._fit_mode
        self.b_fit.setText("Fit" if not self._fit_mode else "100%")
        self._fit()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit()

    def _fit(self):
        if self._pix is not None and not self._pix.isNull():
            if self._fit_mode:
                box = self.scroll.viewport().size() - QSize(12, 12)
                self.img.setPixmap(self._pix.scaled(box, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                                   if (self._pix.width() > box.width() or self._pix.height() > box.height()) else self._pix)
            else:
                self.img.setPixmap(self._pix)

    def _show_image(self, pix: QPixmap) -> bool:
        if pix.isNull():
            return False
        self._pix = pix
        self.stack.setCurrentWidget(self.scroll)
        self._fit()
        return True

    # ---- public
    def show_plan(self, plan: dict, meta: dict | None = None) -> tuple[bool, str]:
        """Called on the GUI thread. Returns (ok, message for the agent)."""
        try:
            return self._show(plan, meta or {})
        except Exception as e:  # never let a bad file crash the app
            return False, f"preview failed: {type(e).__name__}: {e}"

    def _from(self, meta: dict) -> str:
        bits = []
        if meta.get("agent"):
            bits.append("from " + str(meta["agent"]))
        if meta.get("ts"):
            bits.append(time.strftime("%H:%M", time.localtime(meta["ts"])))
        return " ".join(bits)

    def _show(self, plan, meta):
        self.video.pause()
        url, path = plan.get("url"), plan.get("path")
        self._target = url or path
        if url:
            if self.web:
                self.web.load(QUrl(url))
                self.stack.setCurrentWidget(self.web)
                self._header(url, ["Live page"], path=False, fit=False)
                return True, f"showing {url} in the Laptop Agent app (live page)"
            import httpx
            r = httpx.get(url, timeout=6, follow_redirects=True)
            self.text.setHtml(r.text)
            self.stack.setCurrentWidget(self.text)
            self._header(url, ["Static snapshot (no JavaScript)"], path=False, fit=False)
            return True, (f"showing a static snapshot of {url} in the app (no JavaScript). "
                          "For full live pages the user can run: pip install PySide6-Addons")
        ext = os.path.splitext(path)[1].lower()
        name = os.path.basename(path)
        size = mt.human_size(os.path.getsize(path)) if os.path.exists(path) else ""
        kind = mt.kind_of(path)
        if kind == "image" or ext == ".svg":
            pm = QPixmap(path)
            if not self._show_image(pm):
                return False, "could not decode this image"
            self._fit_mode = True
            self.b_fit.setText("100%")
            self._header(name, ["Photo", f"{pm.width()}×{pm.height()}", size, self._from(meta)], path=True, fit=True)
            return True, f"showing image {path} in the Laptop Agent app"
        if kind == "video":
            if not mt.ffmpeg_path():
                self.msg.setText("Video preview needs ffmpeg.\n\nRun:   pip install imageio-ffmpeg\nthen restart the agent.")
                self.stack.setCurrentWidget(self.msg)
                self._header(name, ["Video", size], path=True, fit=False)
                return False, "video preview needs ffmpeg (pip install imageio-ffmpeg)"
            info = self.video.load(path)
            self.stack.setCurrentWidget(self.video)
            self._header(name, ["Video", f"{info['width']}×{info['height']}" if info["width"] else "",
                                mt.human_time(info["duration"]), size, "silent preview", self._from(meta)], path=True, fit=False)
            return True, f"playing video {path} in the Laptop Agent app"
        if ext in (".html", ".htm"):
            if self.web:
                self.web.load(QUrl.fromLocalFile(path))
                self.stack.setCurrentWidget(self.web)
                self._header(name, ["Web page", size, self._from(meta)], path=True, fit=False)
                return True, f"showing {path} in the app (full web view)"
            self.text.setSource(QUrl.fromLocalFile(path))
            self.stack.setCurrentWidget(self.text)
            self._header(name, ["Web page (basic HTML/CSS only)", size, self._from(meta)], path=True, fit=False)
            return True, (f"showing {path} in the app with basic HTML/CSS only (no JavaScript, so React/JSX apps "
                          "will not run). For full previews the user can run: pip install PySide6-Addons")
        if ext == ".md":
            self.text.setMarkdown(open(path, encoding="utf-8", errors="replace").read(400_000))
            self.stack.setCurrentWidget(self.text)
            self._header(name, ["Markdown", size, self._from(meta)], path=True, fit=False)
            return True, f"showing {path} in the app"
        if ext == ".txt":
            self.text.setPlainText(open(path, encoding="utf-8", errors="replace").read(400_000))
            self.stack.setCurrentWidget(self.text)
            self._header(name, ["Text", size, self._from(meta)], path=True, fit=False)
            return True, f"showing {path} in the app"
        if ext == ".pdf":
            if self.web:
                self.web.load(QUrl.fromLocalFile(path))
                self.stack.setCurrentWidget(self.web)
                self._header(name, ["PDF", size, self._from(meta)], path=True, fit=False)
                return True, f"showing {path} in the app"
            self.msg.setText("PDF preview needs the optional add-on:\n\npip install PySide6-Addons")
            self.stack.setCurrentWidget(self.msg)
            self._header(name, ["PDF", size], path=True, fit=False)
            return False, "PDF preview needs PySide6-Addons (pip install PySide6-Addons)"
        return False, f"cannot preview {ext} files in the app"


class PreviewPage(QWidget):
    """Library of every photo/video/page the agents sent or showed (left) + the viewer (right)."""
    def __init__(self, engine, thumbs: Thumbs):
        super().__init__()
        self.engine, self.thumbs = engine, thumbs
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(14)
        left = QVBoxLayout()
        left.addWidget(_label("Preview", "title"))
        self.filter = QComboBox()
        self.filter.addItems(["Everything", "Photos", "Videos"])
        left.addWidget(self.filter)
        self.lib = QListWidget()
        self.lib.setViewMode(QListWidget.IconMode)
        self.lib.setIconSize(QSize(120, 76))
        self.lib.setGridSize(QSize(132, 112))
        self.lib.setResizeMode(QListWidget.Adjust)
        self.lib.setMovement(QListWidget.Static)
        self.lib.setWordWrap(True)
        self.lib.setUniformItemSizes(True)
        self.lib.setFixedWidth(292)
        left.addWidget(self.lib, 1)
        self.empty = _label("Photos and videos from the agents will collect here.", "muted", wrap=True)
        left.addWidget(self.empty)
        h.addLayout(left)
        self.viewer = PreviewPane()
        h.addWidget(self.viewer, 1)
        self._rows: dict[str, dict] = {}
        self.lib.itemClicked.connect(lambda it: self.open_path(it.data(Qt.UserRole)))
        self.filter.currentIndexChanged.connect(self._apply_filter)
        thumbs.ready.connect(self._thumb_ready)

    def load_from_store(self):
        for row in reversed(self.engine.store.media(200)):
            self.add_media(row)

    def add_media(self, row: dict, select: bool = False):
        path = row["path"]
        if path in self._rows:                       # re-sent: move to the top
            for i in range(self.lib.count()):
                if self.lib.item(i).data(Qt.UserRole) == path:
                    self.lib.takeItem(i)
                    break
        self._rows[path] = row
        it = QListWidgetItem(QIcon(self.thumbs.pixmap(path, 120, 76, crop=True)), os.path.basename(path))
        it.setData(Qt.UserRole, path)
        it.setToolTip(f"{path}\n{self._who(row)}")
        it.setSizeHint(QSize(132, 112))
        self.lib.insertItem(0, it)
        self.empty.setVisible(False)
        self._apply_filter()
        if select:
            self.lib.setCurrentItem(it)

    def _who(self, row) -> str:
        name = self.engine.router.name_of(row.get("agent")) if row.get("agent") else ""
        return ("from " + name + " · " if name else "") + time.strftime("%H:%M", time.localtime(row.get("ts", 0)))

    def _apply_filter(self):
        want = {0: None, 1: "image", 2: "video"}[self.filter.currentIndex()]
        for i in range(self.lib.count()):
            it = self.lib.item(i)
            it.setHidden(bool(want) and mt.kind_of(it.data(Qt.UserRole)) != want)

    def _thumb_ready(self, path: str):
        for i in range(self.lib.count()):
            it = self.lib.item(i)
            if it.data(Qt.UserRole) == path:
                it.setIcon(QIcon(self.thumbs.pixmap(path, 120, 76, crop=True)))

    def open_path(self, path: str):
        row = self._rows.get(path, {})
        meta = {"agent": self.engine.router.name_of(row.get("agent")) if row.get("agent") else None, "ts": row.get("ts")}
        return self.viewer.show_plan({"path": path}, meta)


AGENT_COLORS = ["#6ea8ff", "#2fcf8a", "#f5a524", "#c08bff", "#ff7aa8", "#3fd0e0"]


def agent_color(name: str) -> str:
    return AGENT_COLORS[sum(map(ord, name or "")) % len(AGENT_COLORS)]


def _clear_layout_keep_stretch(lay):
    while lay.count() > 1:
        it = lay.takeAt(0)
        w = it.widget()
        if w is not None:
            _drop(w)


def _drop(w):
    w.hide()
    w.setParent(None)      # vanish NOW (deleteLater alone leaves ghosts until the event loop runs)
    w.deleteLater()


def _clear_layout(lay):
    while lay.count():
        it = lay.takeAt(0)
        w, sub = it.widget(), it.layout()
        if w is not None:
            _drop(w)
        elif sub is not None:
            _clear_layout(sub)


class _ClickLabel(QLabel):
    clicked = Signal()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(e)


class ChatView(QScrollArea):
    """Chat bubbles: you on the right, each agent on the left with its name, system lines centred."""
    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet(f"QScrollArea {{ background: #0a0f14; border: 1px solid {theme.BORDER}; border-radius: 12px; }}")
        self.viewport().setObjectName("chatViewport")
        self.viewport().setStyleSheet("#chatViewport { background: transparent; }")   # scoped: widget stylesheets cascade to children
        inner = QWidget()
        inner.setObjectName("chatInner")
        inner.setStyleSheet("#chatInner { background: transparent; }")      # scoped: must not override the bubble colours
        self._lay = QVBoxLayout(inner)
        self._lay.setContentsMargins(10, 10, 10, 10)
        self._lay.setSpacing(8)
        self._lay.addStretch(1)
        self.setWidget(inner)
        self.count = 0
        self.thumbs = None            # set by the main window (shared Thumbs helper)
        self.on_media = None          # callback(path) when a photo/video bubble is clicked
        self._media_labels: dict[str, list] = {}

    def thumb_ready(self, path: str):
        for lab in self._media_labels.get(path, []):
            try:
                lab.setPixmap(self.thumbs.pixmap(path, 360, 220))
            except RuntimeError:      # label already deleted by clear()
                pass

    def clear(self):
        self._media_labels = {}
        while self._lay.count() > 1:
            it = self._lay.takeAt(0)
            w, sub = it.widget(), it.layout()
            if w is not None:
                _drop(w)
            elif sub is not None:
                _clear_layout(sub)
                sub.deleteLater()
        self.count = 0

    def add(self, who: str, text: str, name: str | None = None):
        self.count += 1
        if who == "sys":
            lb = QLabel(text)
            lb.setObjectName("sysline")
            lb.setAlignment(Qt.AlignCenter)
            lb.setWordWrap(True)
            self._lay.insertWidget(self._lay.count() - 1, lb)
        elif who == "media":
            bubble = QFrame()
            bubble.setObjectName("bubbleBrain")
            bl = QVBoxLayout(bubble)
            bl.setContentsMargins(10, 8, 10, 8)
            bl.setSpacing(4)
            if name:
                tag = QLabel(name)
                tag.setStyleSheet(f"color:{agent_color(name)}; font-weight:700; font-size:11px;")
                bl.addWidget(tag)
            lab = _ClickLabel()
            lab.setPixmap(self.thumbs.pixmap(text, 360, 220) if self.thumbs else _placeholder(360, 220, "MEDIA"))
            lab.setCursor(Qt.PointingHandCursor)
            lab.setToolTip("Click to open in Preview")
            lab.clicked.connect(lambda p=text: self.on_media and self.on_media(p))
            self._media_labels.setdefault(text, []).append(lab)
            bl.addWidget(lab)
            kind = {"image": "Photo", "video": "Video"}.get(mt.kind_of(text), "File")
            bl.addWidget(_label(f"{kind}  ·  {os.path.basename(text)}", "muted"))
            row = QHBoxLayout()
            row.addWidget(bubble)
            row.addStretch(1)
            self._lay.insertLayout(self._lay.count() - 1, row)
        else:
            bubble = QFrame()
            bubble.setObjectName("bubbleYou" if who == "you" else "bubbleBrain")
            bl = QVBoxLayout(bubble)
            bl.setContentsMargins(14, 9, 14, 10)
            bl.setSpacing(2)
            if who != "you" and name:
                tag = QLabel(name)
                tag.setStyleSheet(f"color:{agent_color(name)}; font-weight:700; font-size:11px;")
                bl.addWidget(tag)
            body = QLabel(text)
            body.setObjectName("bubbleText")
            body.setWordWrap(True)
            body.setTextInteractionFlags(Qt.TextSelectableByMouse)
            fm = body.fontMetrics()
            widest = max((fm.horizontalAdvance(line) for line in text.split("\n")), default=60)
            body.setFixedWidth(max(60, min(520, widest + 8)))
            if who == "you":
                body.setStyleSheet("color:white;")
            bl.addWidget(body)
            row = QHBoxLayout()
            if who == "you":
                row.addStretch(1)
                row.addWidget(bubble)
            else:
                row.addWidget(bubble)
                row.addStretch(1)
            self._lay.insertLayout(self._lay.count() - 1, row)
        QTimer.singleShot(30, lambda: self.verticalScrollBar().setValue(self.verticalScrollBar().maximum()))


STATE_LABEL = {"connected": "Connected", "reconnecting": "Reconnecting…", "paused": "Paused", "stopped": "Stopped",
               "disconnected": "Disconnected", "auth_failed": "Access revoked"}
NAV = [("activity", "Activity"), ("preview", "Preview"), ("chat", "Chat"), ("agents", "Agents"), ("terminal", "Terminal"),
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
        self.thumbs = Thumbs()
        self.preview_page = PreviewPage(self.engine, self.thumbs)
        self.preview = self.preview_page.viewer
        self.pages.addWidget(self.preview_page)
        self.pages.addWidget(self._build_chat())
        self.pages.addWidget(self._build_agents())
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
        self.agent_combo.activated.connect(self._on_pin)
        self.b_reassign.clicked.connect(lambda: self.engine.reassign_chat(self._cur))
        self.b_send.clicked.connect(self._chat)
        self.b_stop.clicked.connect(self._stop_brain)
        bridge.event.connect(self._on_event)
        bridge.approval.connect(self._on_approval)
        bridge.preview.connect(self._on_preview)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(700)
        self._load_chats()
        self.preview_page.load_from_store()
        self.chatview.thumbs, self.chatview.on_media = self.thumbs, self._open_media
        self.thumbs.ready.connect(self.chatview.thumb_ready)
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
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Time", "Request", "Agent", "Task", "Ask?", "Status"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        for c, w in enumerate((80, 170, 110, 150, 80)):
            self.table.setColumnWidth(c, w)
        card = _card()
        cl = QVBoxLayout(card)
        cl.setContentsMargins(8, 8, 8, 8)
        cl.addWidget(self.table)
        lay.addWidget(card, 1)
        return page

    def _build_chat(self) -> QWidget:
        page = QWidget()
        outer = QHBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(14)
        left = QVBoxLayout()
        left.addWidget(_label("Chats", "title"))
        self.chat_list = QListWidget()
        self.chat_list.setFixedWidth(230)
        left.addWidget(self.chat_list, 1)
        self.b_newchat = QPushButton("+  New chat")
        self.b_newchat.setObjectName("primary")
        left.addWidget(self.b_newchat)
        outer.addLayout(left)
        lay = QVBoxLayout()
        head = QHBoxLayout()
        self.chat_title = _label("General", "title")
        head.addWidget(self.chat_title, 1)
        self.agent_combo = QComboBox()
        self.agent_combo.setToolTip("Who handles this chat. Auto = the router picks and hands over if an agent drops.")
        head.addWidget(self.agent_combo)
        self.b_reassign = QPushButton("Reassign")
        self.b_reassign.setToolTip("Hand this chat's open work to another agent right now (same context).")
        head.addWidget(self.b_reassign)
        self.b_stop = QPushButton("STOP")
        self.b_stop.setObjectName("stop")
        self.b_stop.setToolTip("Sends the word \"stop\" to every agent. They halt all work until you talk to them again.")
        head.addWidget(self.b_stop)
        lay.addLayout(head)
        self.chat_hint = _label("", "muted", wrap=True)
        lay.addWidget(self.chat_hint)
        self.chatview = ChatView()
        lay.addWidget(self.chatview, 1)
        row = QHBoxLayout()
        self.chat_in = QLineEdit()
        self.b_send = QPushButton("Send")
        self.b_send.setObjectName("primary")
        row.addWidget(self.chat_in, 1)
        row.addWidget(self.b_send)
        lay.addLayout(row)
        outer.addLayout(lay, 1)
        self._cur = "app"
        self._items: dict[str, QListWidgetItem] = {}
        self._unread: set[str] = set()
        self._agents_sig = None
        self._combo_sig = None
        self.chat_list.currentItemChanged.connect(self._select_chat)
        self.b_newchat.clicked.connect(self._new_chat)
        return page

    def _build_agents(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        lay.addWidget(_label("Agents", "title"))
        lay.addWidget(_label("Each LLM Arena window that runs the Brain prompt becomes an agent with a name. Call one by "
                             "typing @name in a chat. If an agent drops, its chats move to another agent with the same "
                             "context (kept on this laptop).", "muted", wrap=True))
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.NoFrame)
        sc.viewport().setObjectName("agentsVp")
        sc.viewport().setStyleSheet("#agentsVp { background: transparent; }")
        inner = QWidget()
        inner.setObjectName("agentsInner")
        inner.setStyleSheet("#agentsInner { background: transparent; }")
        self.agents_box = QVBoxLayout(inner)
        self.agents_box.setContentsMargins(0, 0, 6, 0)
        self.agents_box.setSpacing(10)
        self.agents_box.addStretch(1)
        sc.setWidget(inner)
        lay.addWidget(sc, 1)
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
        if i == self._page_idx["preview"]:
            self.nav["preview"].setText("Preview")

    def _open_media(self, path: str):
        self._go("preview")
        self.preview_page.open_path(path)

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

    # ----------------------------------------------------------------- chats
    def _agent_name(self, agent_id) -> str | None:
        return self.engine.router.name_of(agent_id) if agent_id else None

    def _item_text(self, c: dict) -> str:
        ch = c["channel"]
        if ch.startswith(("local:", "relay:")):
            sub = "API client"
        else:
            sub = ("pinned · " if c.get("pinned") else "") + (self._agent_name(c.get("assigned")) or "unassigned")
        return c["title"] + ("   ●" if ch in self._unread else "") + "\n" + sub

    def _ensure_item(self, c: dict):
        ch = c["channel"]
        if ch not in self._items:
            it = QListWidgetItem(self._item_text(c))
            it.setData(Qt.UserRole, ch)
            self._items[ch] = it
            self.chat_list.addItem(it)

    def _load_chats(self):
        self.engine.store.ensure_chat("app")
        for c in sorted(self.engine.store.chats(), key=lambda c: (c["channel"] != "app", c["created"])):
            self._ensure_item(c)
        self.chat_list.setCurrentRow(0)

    def _select_chat(self, item, _prev=None):
        if item is None:
            return
        self._cur = item.data(Qt.UserRole)
        self._unread.discard(self._cur)
        c0 = self.engine.store.chat(self._cur)
        if c0 and self._cur in self._items:
            self._items[self._cur].setText(self._item_text(c0))
        api = self._cur.startswith(("local:", "relay:"))
        chat = self.engine.store.chat(self._cur) or {}
        self.chat_title.setText(chat.get("title", self._cur))
        self.chat_in.setEnabled(not api)
        self.b_send.setEnabled(not api)
        self.agent_combo.setEnabled(True)
        self.chat_in.setPlaceholderText("This chat belongs to an API client; replies go back to that client, not here."
                                        if api else "Message the agents…  (@name calls a specific one)")
        self.chat_hint.setText(
            "Read-only: a program using an API key started this conversation. The router chose the agent and keeps the "
            "context on this laptop." if api else
            "The router picks an agent for this chat and keeps its context on this laptop. Type @name to call a specific "
            "agent. Local commands start with / (try /help). Chat never runs commands on this laptop.")
        self._combo_sig = None
        self._render_chat()

    def _render_chat(self):
        self.chatview.clear()
        msgs = self.engine.store.messages(self._cur, 200)
        for m in msgs:
            self.chatview.add(m["who"], m["text"], self._agent_name(m["agent"]))
        if not msgs:
            self.chatview.add("sys", "No messages yet. Say hi, or type @name to call an agent.")

    def _new_chat(self):
        name, ok = QInputDialog.getText(self, "New chat", "Name this chat:")
        if not ok:
            return
        ch = "app:" + secrets.token_hex(3)
        c = self.engine.store.ensure_chat(ch, name.strip()[:40] or "New chat")
        self._ensure_item(c)
        self.chat_list.setCurrentItem(self._items[ch])

    def _on_pin(self, idx: int):
        self.engine.pin_chat(self._cur, self.agent_combo.itemData(idx))
        self._combo_sig = None

    def _chat(self):
        text = self.chat_in.text().strip()
        self.chat_in.clear()
        if not text:
            return
        reply = chat_reply(self.engine, text, self._cur)     # the router echoes your message into the chat itself
        if text.startswith("/") or reply != "Sent to the Brain.":
            self.chatview.add("sys", reply)

    def _stop_brain(self):
        if not self.engine.send_user_message("stop", self._cur):
            self.chatview.add("sys", "Not connected - press Start first.")

    def _on_event(self, kind, data):
        if kind == "chat":
            ch = data.get("channel", "app")
            c = self.engine.store.chat(ch)
            if c:
                self._ensure_item(c)
            if ch == self._cur:
                self.chatview.add(data["who"], data["text"], self._agent_name(data.get("agent")))
            elif data["who"] != "sys":
                self._unread.add(ch)
                if c:
                    self._items[ch].setText(self._item_text(c))
            if data["who"] in ("brain", "media"):
                if self.pages.currentIndex() != self._page_idx["chat"]:
                    self.nav["chat"].setText("Chat  ●")
                QApplication.alert(self)
        if kind == "media":
            shown = data.get("source") == "open_preview"
            self.preview_page.add_media(data, select=shown)
            if not shown and self.pages.currentIndex() != self._page_idx["preview"]:
                self.nav["preview"].setText("Preview  ●")
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
        self._refresh_chats(s)
        self._refresh_agents(s)

    def _refresh_chats(self, s):
        for c in s["chats"]:
            self._ensure_item(c)
            it = self._items[c["channel"]]
            txt = self._item_text(c)
            if it.text() != txt:
                it.setText(txt)
        cur = next((c for c in s["chats"] if c["channel"] == self._cur), None)
        agents = s["agents"]
        sig = (self._cur, cur.get("pinned") if cur else None, tuple((a["agent_id"], a["name"], a["online"]) for a in agents))
        if sig != self._combo_sig:
            self._combo_sig = sig
            self.agent_combo.blockSignals(True)
            self.agent_combo.clear()
            self.agent_combo.addItem("Auto · router decides", None)
            for a in agents:
                self.agent_combo.addItem(f"{a['name']}  ({'online' if a['online'] else 'offline'})", a["agent_id"])
            pinned = cur.get("pinned") if cur else None
            idx = next((i for i in range(self.agent_combo.count()) if self.agent_combo.itemData(i) == pinned), 0)
            self.agent_combo.setCurrentIndex(idx)
            self.agent_combo.blockSignals(False)
        if cur:
            who = self._agent_name(cur.get("assigned"))
            self.b_reassign.setEnabled(bool(cur.get("assigned")))
            self.chat_title.setText(cur["title"] + (f"   ·   {who}" if who else ""))

    def _refresh_agents(self, s):
        agents, chats = s["agents"], s["chats"]
        sig = tuple((a["agent_id"], a["name"], a["online"], tuple(c["title"] for c in chats if c.get("assigned") == a["agent_id"]))
                    for a in agents)
        if sig == self._agents_sig:
            return
        self._agents_sig = sig
        _clear_layout_keep_stretch(self.agents_box)
        if not agents:
            card = _card()
            cl = QVBoxLayout(card)
            cl.setContentsMargins(18, 16, 18, 16)
            cl.addWidget(_label("No agents yet", "title"))
            cl.addWidget(_label("Open LLM Arena, paste the Brain prompt and start it. It registers with the relay, gets a "
                                "name (like Nova) and appears here. Open more windows with the same prompt for more agents.",
                                "muted", wrap=True))
            self.agents_box.insertWidget(0, card)
            return
        for i, a in enumerate(agents):
            card = _card()
            row = QHBoxLayout(card)
            row.setContentsMargins(18, 14, 18, 14)
            dot = QLabel("●")
            dot.setStyleSheet(f"color:{theme.OK if a['online'] else theme.MUTED}; font-size:18px;")
            row.addWidget(dot)
            col = QVBoxLayout()
            nm = QLabel(a["name"])
            nm.setStyleSheet(f"color:{agent_color(a['name'])}; font-size:16px; font-weight:700;")
            col.addWidget(nm)
            mine = [c["title"] for c in chats if c.get("assigned") == a["agent_id"]]
            col.addWidget(_label(("online" if a["online"] else "offline") + f"  ·  id {a['agent_id']}  ·  "
                                 + (f"{len(mine)} chat(s): " + ", ".join(mine[:4]) if mine else "no chats yet"), "muted", wrap=True))
            row.addLayout(col, 1)
            b_call, b_ren = QPushButton("Call"), QPushButton("Rename")
            b_call.clicked.connect(lambda _=False, n=a["name"]: self._call(n))
            b_ren.clicked.connect(lambda _=False, aid=a["agent_id"], n=a["name"]: self._rename(aid, n))
            row.addWidget(b_call)
            row.addWidget(b_ren)
            self.agents_box.insertWidget(i, card)

    def _call(self, name: str):
        self._go("chat")
        self.chat_in.setText(f"@{name.lower()} ")
        self.chat_in.setFocus()

    def _rename(self, agent_id: str, cur: str):
        name, ok = QInputDialog.getText(self, "Rename agent", "New name:", text=cur)
        if ok and name.strip():
            self.engine.rename_agent(agent_id, name.strip()[:40])

    def _fill(self, rows):
        t = self.table
        t.setRowCount(len(rows))
        for i, r in enumerate(rows):
            vals = (time.strftime("%H:%M:%S", time.localtime(r.get("ts", 0))), r.get("request_id", ""),
                    self._agent_name(r.get("agent")) or "", r.get("task_type", ""), {"auto": "no", "ask": "yes", "block": "blocked"}.get(r.get("risk"), ""),
                    str(r.get("status", "")).replace("_", " "))
            for j, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if j == 5:
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
