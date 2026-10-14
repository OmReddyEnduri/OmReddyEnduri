"""Executes already-approved operations. Never decides policy."""
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone

from .policy import Verdict
from .redact import redact

SCRUB_ENV = ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "APIKEY", "CREDENTIAL", "ENROLL")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Capture:
    """Bounded stream collector: keeps the first `cap` bytes, counts the rest."""
    def __init__(self, cap: int):
        self.cap, self.buf, self.dropped = cap, bytearray(), 0

    def add(self, b: bytes):
        room = self.cap - len(self.buf)
        if room > 0:
            self.buf += b[:room]
        self.dropped += max(0, len(b) - max(room, 0))

    def text(self) -> str:
        return self.buf.decode("utf-8", "replace")


class ExecResult:
    def __init__(self, status="completed", exit_code=0, stdout="", stderr="", truncated=False,
                 error=None, data=None, started=None):
        self.status, self.exit_code, self.stdout, self.stderr = status, exit_code, stdout, stderr
        self.truncated, self.error, self.data = truncated, error, data
        self.started, self.completed = started or now(), now()


def _kill_tree(p: subprocess.Popen):
    try:
        if os.name == "posix":
            os.killpg(p.pid, signal.SIGKILL)
        else:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
    except (ProcessLookupError, PermissionError, OSError):
        p.kill()


def run_process(argv, cwd, timeout, cap, cancel: threading.Event, on_output=None) -> ExecResult:
    started = now()
    env = {k: v for k, v in os.environ.items() if not any(s in k.upper() for s in SCRUB_ENV)}
    kw = {"start_new_session": True} if os.name == "posix" else {"creationflags": 0x00000200}
    try:
        p = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, shell=False, **kw)
    except (OSError, ValueError) as e:
        return ExecResult("failed", None, error=f"could not start: {e}", started=started)
    out, err = Capture(cap), Capture(cap)

    def pump(stream, cap_obj, tag):
        for chunk in iter(lambda: stream.read1(4096) if hasattr(stream, "read1") else stream.read(4096), b""):
            cap_obj.add(chunk)
            if on_output:
                on_output(tag, chunk.decode("utf-8", "replace"))
    ts = [threading.Thread(target=pump, args=(p.stdout, out, "out"), daemon=True),
          threading.Thread(target=pump, args=(p.stderr, err, "err"), daemon=True)]
    for t in ts:
        t.start()
    deadline, status = time.monotonic() + timeout, "completed"
    while p.poll() is None:
        if cancel.is_set():
            status = "cancelled"
            break
        if time.monotonic() > deadline:
            status = "timeout"
            break
        time.sleep(0.05)
    if status != "completed":
        _kill_tree(p)
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill()
    for t in ts:
        t.join(timeout=2)
    code = p.returncode
    if status == "completed" and code != 0:
        status = "failed"
    r = ExecResult(status, code, out.text(), err.text(), bool(out.dropped or err.dropped), started=started)
    if status in ("timeout", "cancelled"):
        r.error = status
    return r


def execute(v: Verdict, task_timeout: int, cap: int, cancel: threading.Event, on_output=None,
            workspaces=None) -> ExecResult:
    t = v.operation
    started = now()
    try:
        if t == "device_status":
            return ExecResult(stdout=_status_text(workspaces or []), started=started)
        if t == "list_dir":
            entries = []
            with os.scandir(v.plan["path"]) as it:
                for e in sorted(it, key=lambda e: e.name)[:1000]:
                    try:
                        st = e.stat(follow_symlinks=False)
                        kind = "dir" if e.is_dir(follow_symlinks=False) else "link" if e.is_symlink() else "file"
                        entries.append(f"{kind}\t{st.st_size}\t{e.name}")
                    except OSError:
                        entries.append(f"?\t0\t{e.name}")
            return ExecResult(stdout="\n".join(entries), started=started)
        if t == "read_file":
            with open(v.plan["path"], "rb") as f:
                raw = f.read(v.plan["max_bytes"] + 1)
            if b"\x00" in raw[:8192]:
                return ExecResult("failed", 1, error="binary file; only text files can be read", started=started)
            trunc = len(raw) > v.plan["max_bytes"]
            return ExecResult(stdout=raw[:v.plan["max_bytes"]].decode("utf-8", "replace"),
                              truncated=trunc, started=started)
        if t == "read_files":
            return _read_files(v.plan, started)
        if t == "write_files":
            done = []
            try:
                for f in v.plan["files"]:
                    _atomic_write(f["path"], f["content"])
                    done.append(f["path"])
            except OSError as e:
                return ExecResult("failed", 1, stdout="\n".join(f"wrote {p}" for p in done),
                                  error=f"{type(e).__name__}: {e} (written so far: {len(done)})", started=started)
            return ExecResult(stdout="\n".join(f"wrote {p}" for p in done), data={"written": done}, started=started)
        if t == "write_file":
            path = v.plan["path"]
            _atomic_write(path, v.plan["content"])
            return ExecResult(stdout=f"wrote {len(v.plan['content'].encode())} bytes to {path}", started=started)
        if t == "make_dir":
            os.makedirs(v.plan["path"], exist_ok=True)
            return ExecResult(stdout=f"created {v.plan['path']}", started=started)
        if t == "delete_file":
            os.unlink(v.plan["path"])
            return ExecResult(stdout=f"deleted {v.plan['path']}", started=started)
        if t == "screenshot":
            return _screenshot(v.plan, started)
        if t in ("run_command", "run_script", "install_package"):
            return run_process(v.plan["argv"], v.plan["cwd"], min(task_timeout, 3600), cap, cancel, on_output)
        return ExecResult("failed", 1, error=f"no executor for {t}", started=started)
    except OSError as e:
        return ExecResult("failed", 1, error=f"{type(e).__name__}: {e}", started=started)


def _status_text(workspaces) -> str:
    lines = [f"hostname: {socket.gethostname()}", f"os: {platform.platform()}",
             f"python: {sys.version.split()[0]}", f"cpus: {os.cpu_count()}", f"time: {now()}"]
    for w in workspaces:
        try:
            u = shutil.disk_usage(w if os.path.exists(w) else os.path.dirname(w))
            lines.append(f"workspace: {w}  disk_free_gb: {u.free / 1e9:.1f} of {u.total / 1e9:.1f}")
        except OSError as e:
            lines.append(f"workspace: {w}  (disk info unavailable: {e})")
    return "\n".join(lines)


def _screenshot(plan: dict, started: str) -> ExecResult:
    import base64
    import io
    try:
        from PIL import Image, ImageGrab
    except ImportError:
        return ExecResult("failed", 1, error="Pillow is not installed (pip install Pillow)", started=started)
    try:
        img = ImageGrab.grab(all_screens=True) if sys.platform.startswith("win") else ImageGrab.grab()
    except Exception as e:  # no display, permission denied (macOS screen recording), etc.
        return ExecResult("failed", 1, error=f"screen capture failed: {type(e).__name__}: {e}", started=started)
    path = plan["path"]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.convert("RGB").save(path)
    data = {"path": path, "width": img.width, "height": img.height}
    if plan["return_image"]:
        small = img.convert("RGB")
        if small.width > plan["max_width"]:
            small = small.resize((plan["max_width"], round(small.height * plan["max_width"] / small.width)))
        buf = io.BytesIO()
        small.save(buf, "JPEG", quality=70)
        while buf.tell() > 700_000 and small.width > 400:   # keep the result small
            small = small.resize((small.width * 3 // 4, small.height * 3 // 4))
            buf = io.BytesIO()
            small.save(buf, "JPEG", quality=60)
        data.update(image_mime="image/jpeg", image_base64=base64.b64encode(buf.getvalue()).decode(),
                    image_width=small.width, image_height=small.height)
    return ExecResult(stdout=f"screenshot saved to {path} ({img.width}x{img.height})", data=data, started=started)


def _atomic_write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".agent-tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content.encode())
        if os.path.exists(path):
            shutil.copymode(path, tmp)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _read_files(plan: dict, started: str) -> ExecResult:
    import fnmatch
    import hashlib
    from pathlib import PurePath
    from .policy import Policy
    cands = list(plan["files"])
    if plan["root"]:
        root, seen = plan["root"], set(map(os.path.normcase, cands))
        for dp, dns, fns in os.walk(root):
            dns[:] = sorted(d for d in dns if d not in plan["skip_dirs"])
            for fn in sorted(fns):
                full = os.path.join(dp, fn)
                rel = PurePath(os.path.relpath(full, root)).as_posix()
                if any(fnmatch.fnmatch(rel, g.replace("**/", "")) or fnmatch.fnmatch(rel, g) for g in plan["globs"]) \
                        and os.path.normcase(full) not in seen:
                    cands.append(full)
                    seen.add(os.path.normcase(full))
    files, skipped, total = [], [], 0
    for p in cands:
        if len(files) >= plan["max_files"]:
            skipped.append({"path": p, "reason": "max_files reached"})
            continue
        if Policy._sensitive(p):
            skipped.append({"path": p, "reason": "secret/credential file"})
            continue
        try:
            if os.path.islink(p) or not os.path.isfile(p):
                skipped.append({"path": p, "reason": "not a regular file"})
                continue
            with open(p, "rb") as f:
                raw = f.read(plan["max_bytes_per_file"] + 1)
        except OSError as e:
            skipped.append({"path": p, "reason": str(e)})
            continue
        if b"\x00" in raw[:8192]:
            skipped.append({"path": p, "reason": "binary"})
            continue
        trunc = len(raw) > plan["max_bytes_per_file"]
        text = redact(raw[:plan["max_bytes_per_file"]].decode("utf-8", "replace"), strict_only=True)
        if total + len(text) > plan["total_cap"]:
            skipped.append({"path": p, "reason": "total size cap reached"})
            continue
        total += len(text)
        files.append({"path": p, "size": os.path.getsize(p), "truncated": trunc, "content": text,
                      "sha256": hashlib.sha256(raw).hexdigest()})
    summary = f"{len(files)} file(s), {total} chars, {len(skipped)} skipped"
    return ExecResult(stdout=summary, truncated=any(f["truncated"] for f in files) or bool(skipped),
                      data={"files": files, "skipped": skipped, "total_chars": total}, started=started)
