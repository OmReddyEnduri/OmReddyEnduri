"""Agent engine: heartbeat + poll threads, approval flow, execution, result delivery."""
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Protocol

import httpx

from .audit import Audit
from .client import RelayClient, RelayError
from .config import Config
from .executor import ExecResult, execute
from .policy import ASK, AUTO, BLOCK, Policy, Verdict
from .redact import redact

STOPPED, CONNECTED, RECONNECTING, PAUSED, DISCONNECTED, AUTH_FAILED = (
    "stopped", "connected", "reconnecting", "paused", "disconnected", "auth_failed")


@dataclass
class ApprovalRequest:
    request_id: str
    operation: str
    arguments: dict
    cwd: str
    files: list
    reason: str
    impact: str
    preview: str = ""
    warnings: list = field(default_factory=list)


class Approver(Protocol):
    def ask(self, req: ApprovalRequest, abort: threading.Event) -> str:
        """Block until the human answers: 'approve' | 'reject' | 'cancel'.
        Must return 'cancel' promptly if `abort` is set. Must NEVER time out into approval."""


class Backoff:
    def __init__(self, base=1.0, cap=60.0):
        self.base, self.cap, self.n = base, cap, 0

    def next(self) -> float:
        d = min(self.cap, self.base * (2 ** self.n))
        self.n += 1
        return d * random.uniform(0.7, 1.0)

    def reset(self):
        self.n = 0


class AgentEngine:
    def __init__(self, cfg: Config, approver: Approver, audit: Audit | None = None):
        cfg.validate()
        self.cfg, self.approver = cfg, approver
        self.audit = audit or Audit(cfg.secrets)
        self.policy = Policy(cfg.workspaces, cfg.security_level)
        self.state = STOPPED
        self.paused = False
        self.active: str | None = None
        self.tasks: deque = deque(maxlen=200)       # newest first; dicts for UI
        self.terminal: deque = deque(maxlen=2000)
        self.notices: deque = deque(maxlen=200)
        self.chat: deque = deque(maxlen=300)         # {who: you|brain, text, ts}
        self._chat_last = 0
        self.last_error = ""
        self._listeners: list[Callable] = []
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._cancel: dict[str, threading.Event] = {}
        self._lock = threading.RLock()
        self._client: RelayClient | None = None
        self.preview_handler = None   # GUI sets this: (plan) -> (ok, message); previews render inside the app

    # ------------------------------------------------------------- UI plumbing
    def add_listener(self, fn: Callable[[str, object], None]):
        self._listeners.append(fn)

    def _emit(self, kind: str, data=None):
        for fn in list(self._listeners):
            try:
                fn(kind, data)
            except Exception:
                pass

    def _set_state(self, s: str):
        if s != self.state:
            self.state = s
            self.audit.write("state", state=s)
            self._emit("state", s)

    def notify(self, level: str, msg: str):
        msg = redact(msg, self.cfg.secrets)
        self.notices.appendleft({"level": level, "msg": msg, "ts": time.time()})
        self.audit.write("notice", level=level, msg=msg)
        self._emit("notice", {"level": level, "msg": msg})

    def _term(self, tag: str, text: str):
        text = redact(text, self.cfg.secrets)
        for line in text.splitlines() or [""]:
            self.terminal.append(f"[{tag}] {line}")
        self._emit("terminal", text)

    def _track(self, rid: str, **kw):
        with self._lock:
            row = next((t for t in self.tasks if t["request_id"] == rid), None)
            if row is None:
                row = {"request_id": rid, "ts": time.time()}
                self.tasks.appendleft(row)
            row.update(kw)
        self._emit("task", dict(row))

    def set_security_level(self, level: int):
        """Only callable from the native GUI. Persists and audits the change."""
        if level not in (1, 2, 3):
            raise ValueError("level must be 1, 2 or 3")
        old, self.policy.level, self.cfg.security_level = self.policy.level, level, level
        try:
            from . import config as C
            C.save(self.cfg)
        except Exception as e:
            self.notify("warning", f"Could not save security level: {e}")
        self.audit.write("security_level_changed", old=old, new=level)
        self._emit("level", level)

    # ----------------------------------------------------------------- control
    def start(self):
        with self._lock:
            if self._threads and any(t.is_alive() for t in self._threads):
                self.paused = False
                self._set_state(CONNECTED if self.state == PAUSED else self.state)
                return
            self._stop = threading.Event()
            self.paused = False
            self._client = RelayClient(self.cfg.relay_url, self.cfg.device_token)
            self._set_state(RECONNECTING)
            self._threads = [threading.Thread(target=self._heartbeat_loop, name="hb", daemon=True),
                             threading.Thread(target=self._poll_loop, name="poll", daemon=True),
                             threading.Thread(target=self._chat_loop, name="chat", daemon=True)]
            for t in self._threads:
                t.start()
        self.audit.write("started", relay=self.cfg.relay_url, device=self.cfg.device_id)

    def pause(self):
        self.paused = True
        self._set_state(PAUSED)
        self._beat_now()

    def resume(self):
        self.paused = False
        if self.state == PAUSED:
            self._set_state(CONNECTED)
        self._beat_now()

    def disconnect(self):
        """Immediate: abort approval wait, kill running process, stop threads."""
        self._stop.set()
        for ev in list(self._cancel.values()):
            ev.set()
        for t in self._threads:
            if t is not threading.current_thread():
                t.join(timeout=8)
        if self._client:
            self._client.close()
        self._set_state(DISCONNECTED)
        self.audit.write("disconnected")

    def shutdown(self):
        self.disconnect()
        self.audit.close()

    def _beat_now(self):
        try:
            if self._client and not self._stop.is_set():
                self._client.heartbeat("paused" if self.paused else "running", self.active)
        except Exception:
            pass

    # ------------------------------------------------------------------- loops
    def _heartbeat_loop(self):
        bo = Backoff(2, 30)
        while not self._stop.is_set():
            try:
                r = self._client.heartbeat("paused" if self.paused else "running", self.active)
                for rid in r.get("cancel_requests", []):
                    ev = self._cancel.get(rid)
                    if ev and not ev.is_set():
                        ev.set()
                        self.notify("info", f"Brain requested cancel of {rid}")
                bo.reset()
                if self.state in (RECONNECTING,):
                    self._set_state(PAUSED if self.paused else CONNECTED)
                wait = self.cfg.heartbeat_interval_s
            except RelayError as e:
                if e.permanent:
                    self._auth_failed(e)
                    return
                self._conn_problem(e)
                wait = bo.next()
            except httpx.HTTPError as e:
                self._conn_problem(e)
                wait = bo.next()
            self._stop.wait(wait)

    def _poll_loop(self):
        bo = Backoff(1, 60)
        while not self._stop.is_set():
            if self.paused or self.active:
                self._stop.wait(0.5)
                continue
            try:
                task = self._client.next_task()
                bo.reset()
            except RelayError as e:
                if e.permanent:
                    self._auth_failed(e)
                    return
                self._conn_problem(e)
                self._stop.wait(bo.next())
                continue
            except httpx.HTTPError as e:
                self._conn_problem(e)
                self._stop.wait(bo.next())
                continue
            if task:
                try:
                    self._handle(task)
                except Exception as e:  # never let one task kill the loop
                    self.notify("error", f"Internal error on {task.get('request_id')}: {type(e).__name__}: {e}")
                    self.audit.write("internal_error", request_id=task.get("request_id"), error=str(e))
                finally:
                    self.active = None
                    self._cancel.pop(task.get("request_id"), None)
                continue
            self._stop.wait(self.cfg.poll_interval_s * random.uniform(0.8, 1.2))

    # -------------------------------------------------------------------- chat
    def send_user_message(self, text: str) -> bool:
        """User typed something in the app -> relay -> Brain (text only; never executes anything)."""
        text = (text or "").strip()[:4000]
        if not text or not self._client or self._stop.is_set():
            return False
        self._add_chat("you", text)

        def post():
            try:
                self._client.send_message(text)
            except Exception as e:
                self.notify("warning", f"Message not delivered to the Brain: {e}")
        threading.Thread(target=post, daemon=True).start()
        return True

    def _add_chat(self, who: str, text: str):
        row = {"who": who, "text": text, "ts": time.time()}
        self.chat.append(row)
        self._emit("chat", row)

    def _chat_loop(self):
        while not self._stop.is_set():
            try:
                r = self._client.brain_messages(self._chat_last)
                for m in r.get("messages", []):
                    self._add_chat("brain", m["text"])
                    self.audit.write("brain_message", text=m["text"][:200])
                self._chat_last = r.get("last_id", self._chat_last)
                self._stop.wait(2.5)
            except RelayError as e:
                if e.permanent:
                    return
                self._stop.wait(5)
            except httpx.HTTPError:
                self._stop.wait(5)

    def _auth_failed(self, e):
        self.notify("error", f"Relay rejected this device ({e}). Token revoked or wrong. Disconnecting.")
        self._set_state(AUTH_FAILED)
        self._stop.set()

    def _conn_problem(self, e):
        msg = f"{type(e).__name__}: {e}"[:200]
        if self.state != RECONNECTING:
            self.notify("warning", f"Relay unreachable, retrying with backoff ({msg})")
        self.last_error = msg
        if not self._stop.is_set():
            self._set_state(RECONNECTING)

    # ---------------------------------------------------------------- delivery
    def _deliver(self, what: str, fn, *a) -> bool:
        """Retry transient failures with backoff until delivered or stopped."""
        bo = Backoff(1, 30)
        while True:
            try:
                fn(*a)
                return True
            except RelayError as e:
                if e.permanent:
                    self.notify("warning", f"Relay refused {what}: {e}")
                    return False
            except httpx.HTTPError:
                pass
            if self._stop.is_set():
                return False
            self._stop.wait(bo.next())

    # ----------------------------------------------------------------- handler
    def _handle(self, task: dict):
        rid = task["request_id"]
        self.active = rid
        cancel = self._cancel.setdefault(rid, threading.Event())
        ttype, args = task.get("task_type", ""), task.get("arguments") or {}
        self.audit.write("task_received", request_id=rid, task_type=ttype, reason=task.get("reason"))
        self._track(rid, task_type=ttype, reason=task.get("reason", ""), status="received",
                    arguments=args)
        v = self.policy.classify(ttype, args)
        self._track(rid, risk=v.level, status="classified")

        if v.level == BLOCK:
            note = "; ".join(v.reasons)
            self._deliver("decision", self._client.decision, rid, "blocked", "block", note)
            self._track(rid, status="blocked", note=note)
            self.notify("warning", f"BLOCKED {ttype} ({rid}): {note}")
            self.audit.write("blocked", request_id=rid, reasons=v.reasons)
            return

        if v.level == ASK:
            req = ApprovalRequest(rid, ttype, args, v.cwd, v.files, task.get("reason", ""),
                                  v.impact, v.preview)
            self._track(rid, status="awaiting_approval")
            self._emit("approval_needed", req)
            abort = _AnyEvent(self._stop, cancel)
            answer = self.approver.ask(req, abort)
            self.audit.write("approval", request_id=rid, answer=answer)
            if answer != "approve":
                dec = "rejected" if answer == "reject" else "cancelled"
                self._deliver("decision", self._client.decision, rid, dec, "ask", f"user:{answer}")
                self._track(rid, status=dec)
                return
            v2 = self.policy.classify(ttype, args)  # re-verify: state may have changed while waiting
            if v2.level == BLOCK or v2.plan != v.plan:
                note = "state changed during approval; refusing: " + "; ".join(v2.reasons or ["plan differs"])
                self._deliver("decision", self._client.decision, rid, "blocked", "block", note)
                self._track(rid, status="blocked", note=note)
                self.notify("warning", note)
                return
            v = v2

        if cancel.is_set() or self._stop.is_set():
            self._deliver("decision", self._client.decision, rid, "cancelled", v.level, "cancelled before start")
            self._track(rid, status="cancelled")
            return
        if not self._deliver("decision", self._client.decision, rid, "approved", v.level, None):
            self._track(rid, status="aborted", note="decision not accepted by relay")
            return
        self._track(rid, status="running")
        self._term("run", f"{ttype} {rid}")
        if ttype == "open_preview" and self.preview_handler:
            ok, msg = self.preview_handler(v.plan)
            res = ExecResult(stdout=msg) if ok else ExecResult("failed", 1, error=msg)
        else:
            res = execute(v, int(task.get("timeout_seconds", 60)), self.cfg.max_output_bytes, cancel,
                          lambda tag, s: self._term(tag, s), self.cfg.workspaces)
        payload = self._payload(res)
        self.audit.write("result", request_id=rid, status=res.status, exit_code=res.exit_code)
        self._track(rid, status=res.status, exit_code=res.exit_code, error=res.error)
        if not self._deliver("result", self._client.result, rid, payload) and not self._stop.is_set():
            self.notify("error", f"Could not deliver result for {rid}")

    def _payload(self, r: ExecResult) -> dict:
        sec = self.cfg.secrets
        cap = self.cfg.max_output_bytes
        out, err = redact(r.stdout, sec), redact(r.stderr, sec)
        trunc = r.truncated or len(out) > cap or len(err) > cap
        return {"status": r.status, "exit_code": r.exit_code, "stdout": out[:cap], "stderr": err[:cap],
                "output_truncated": trunc, "error": redact(r.error or "", sec) or None,
                "started_at": r.started, "completed_at": r.completed, "data": r.data}

    # -------------------------------------------------------------- UI snapshot
    def snapshot(self) -> dict:
        return {"state": self.state, "paused": self.paused, "active": self.active,
                "relay_url": self.cfg.relay_url, "security_level": self.policy.level, "device_id": self.cfg.device_id,
                "workspaces": self.cfg.workspaces, "last_error": self.last_error,
                "tasks": [{k: v for k, v in t.items() if k != "arguments"} for t in list(self.tasks)[:100]],
                "terminal": list(self.terminal)[-300:], "chat": list(self.chat)[-60:], "notices": list(self.notices)[:50]}


class _AnyEvent:
    """Event-like view that is set when any member is set."""
    def __init__(self, *evs):
        self.evs = evs

    def is_set(self):
        return any(e.is_set() for e in self.evs)

    def wait(self, timeout=None):
        end = None if timeout is None else time.monotonic() + timeout
        while not self.is_set():
            if end is not None and time.monotonic() >= end:
                return False
            time.sleep(0.05)
        return True
