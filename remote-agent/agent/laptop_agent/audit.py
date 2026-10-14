"""Bounded local audit log (JSON lines, rotating) plus in-memory rings for the UI."""
import json
import logging
import threading
from collections import deque
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from .config import config_dir
from .redact import redact


class Audit:
    def __init__(self, secrets: list[str] | None = None, path=None):
        self.secrets = secrets or []
        self.recent: deque = deque(maxlen=500)
        self._lock = threading.Lock()
        p = path or (config_dir() / "audit.log")
        p.parent.mkdir(parents=True, exist_ok=True)
        self._log = logging.getLogger(f"laptop_agent.audit.{id(self)}")
        self._log.setLevel(logging.INFO)
        self._log.propagate = False
        h = RotatingFileHandler(p, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(message)s"))
        self._log.addHandler(h)
        self._h = h

    def write(self, event: str, **fields) -> None:
        rec = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
        line = redact(json.dumps(rec, default=str)[:4000], self.secrets)
        with self._lock:
            self._log.info(line)
            self.recent.append(json.loads(line) if line.endswith("}") else {"event": event})

    def close(self) -> None:
        self._log.removeHandler(self._h)
        self._h.close()
