"""Private relay between the Brain (LLM) and the laptop agent.

HTTPS + DB-backed polling only; no WebSockets, no inbound laptop port.
"""
import asyncio
import contextlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from . import schemas
from .auth import (get_db, hash_token, new_device_token, require_admin, require_brain,
                   require_device, require_enroll, require_user)
from .config import Settings, load_settings
from .db import AuditLog, Device, Memory, Message, Task, make_session_factory, utcnow

TERMINAL = {"completed", "failed", "timeout", "cancelled", "rejected", "blocked", "expired"}
INFLIGHT = ("claimed", "running")
STATIC = Path(__file__).parent / "static"


def audit(db: Session, actor: str, event: str, request_id=None, device_id=None, detail=None):
    db.add(AuditLog(actor=actor, event=event, request_id=request_id, device_id=device_id,
                    detail=(detail or None) and str(detail)[:1000]))


def iso(d: datetime | None) -> str | None:
    if d is None:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.isoformat()


def aware(d: datetime | None) -> datetime | None:
    return d if d is None or d.tzinfo else d.replace(tzinfo=timezone.utc)


def is_online(dev: Device, s: Settings) -> bool:
    ls = aware(dev.last_seen)
    return bool(ls) and not dev.revoked and (utcnow() - ls).total_seconds() <= s.online_window_s


def device_json(dev: Device, s: Settings) -> dict:
    return {"device_id": dev.id, "name": dev.name, "online": is_online(dev, s),
            "agent_state": dev.agent_state if is_online(dev, s) else "offline",
            "last_heartbeat": iso(dev.last_seen), "info": dev.info, "revoked": dev.revoked,
            "created_at": iso(dev.created_at)}


def task_json(t: Task, full: bool = True) -> dict:
    d = {"request_id": t.request_id, "device_id": t.device_id, "task_type": t.task_type,
         "status": t.status, "risk": t.risk, "reason": t.reason,
         "timeout_seconds": t.timeout_seconds, "created_at": iso(t.created_at),
         "claimed_at": iso(t.claimed_at), "decided_at": iso(t.decided_at),
         "started_at": iso(t.started_at), "completed_at": iso(t.completed_at),
         "exit_code": t.exit_code, "cancel_requested": t.cancel_requested,
         "decision_note": t.decision_note, "error": t.error}
    if full:
        d.update(arguments=t.arguments, stdout=t.stdout, stderr=t.stderr,
                 output_truncated=t.output_truncated, data=t.result_data)
    return d


MEM_LIMIT = 500


def mem_json(m: Memory, full: bool = True) -> dict:
    d = {"id": m.id, "kind": m.kind, "title": m.title, "tags": m.tags, "pinned": m.pinned,
         "source": m.source, "count": m.count, "updated_at": iso(m.updated_at)}
    if full:
        d["body"] = m.body
    return d


def upsert_memory(db: Session, kind: str, title: str, body: str = "", tags: str = "", pinned=None,
                  source: str = "brain", bump: bool = False) -> Memory:
    m = db.scalars(select(Memory).where(Memory.kind == kind, Memory.title == title)).one_or_none()
    if m is None:
        if db.scalar(select(func.count(Memory.id))) >= MEM_LIMIT:  # drop the oldest unpinned auto/lesson entry
            old = db.scalars(select(Memory).where(Memory.pinned.is_(False)).order_by(Memory.updated_at).limit(1)).first()
            if old:
                db.delete(old)
        m = Memory(kind=kind, title=title, body=body, tags=tags, pinned=bool(pinned), source=source)
        db.add(m)
    else:
        if bump:
            m.count += 1
        if body:
            m.body = body
        if tags:
            m.tags = tags
        if pinned is not None:
            m.pinned = pinned
        m.updated_at = utcnow()
    return m


def auto_lesson(db: Session, t: Task, status: str, why: str | None):
    """Record every blocked/rejected/failed task as a lesson so the Brain doesn't repeat the mistake."""
    a = t.arguments or {}
    gist = {k: (str(v)[:160]) for k, v in a.items() if k in ("path", "argv", "url", "cwd", "manager", "packages", "dir", "globs", "interpreter")}
    if t.task_type in ("write_files",):
        gist = {"paths": [str(f.get("path"))[:120] for f in (a.get("files") or [])[:5] if isinstance(f, dict)]}
    why = (why or "").strip()[:200]
    title = f"{t.task_type} {status}: {why[:90] or 'no reason given'}"
    body = (f"Seen on request {t.request_id}. Arguments (gist): {json.dumps(gist)[:500]}. Reason given: {why or 'n/a'}. "
            "Do not repeat this as-is; choose a different structured approach or ask the user.")
    upsert_memory(db, "lesson", title, body, tags=t.task_type, source="auto", bump=True)


def sweep(db: Session, s: Settings) -> None:
    """Fail in-flight tasks of silent devices; expire stale queued tasks."""
    now = utcnow()
    lost_before = now - timedelta(seconds=s.lost_window_s)
    ttl_before = now - timedelta(seconds=s.queue_ttl_s)
    gone = db.scalars(select(Task).join(Device, Device.id == Task.device_id).where(
        Task.status.in_(INFLIGHT), (Device.last_seen < lost_before) | (Device.last_seen.is_(None)))).all()
    for t in gone:
        t.status, t.error, t.completed_at = "failed", "device_disconnected", now
        audit(db, "relay", "task_failed_device_lost", t.request_id, t.device_id)
    old = db.scalars(select(Task).where(Task.status == "queued", Task.created_at < ttl_before)).all()
    for t in old:
        t.status, t.error, t.completed_at = "expired", "not_claimed_in_time", now
        audit(db, "relay", "task_expired", t.request_id, t.device_id)
    if gone or old:
        db.commit()


async def _sweeper(app: FastAPI):
    while True:
        await asyncio.sleep(15)
        try:
            with app.state.Session() as db:
                await asyncio.to_thread(sweep, db, app.state.settings)
        except Exception:  # keep the loop alive
            pass


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    engine, Session = make_session_factory(settings.database_url)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        t = asyncio.create_task(_sweeper(app))
        yield
        t.cancel()

    app = FastAPI(title="Remote Laptop Relay", version="1.0.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings, app.state.Session, app.state.engine = settings, Session, engine

    @app.middleware("http")
    async def sec_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; "
            "connect-src 'self'; base-uri 'none'; form-action 'none'")
        return resp

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "database": engine.dialect.name}

    @app.get("/api/v1/time")
    def server_time(_=Depends(require_brain)):
        """The Brain has no clock: this is its source of truth. IST = UTC+05:30."""
        now = utcnow()
        ist = now.astimezone(timezone(timedelta(hours=5, minutes=30)))
        return {"utc": iso(now), "ist": ist.isoformat(), "ist_human": ist.strftime("%A %d %b %Y, %I:%M:%S %p IST"),
                "epoch": int(now.timestamp())}

    # ---------------------------------------------------------------- devices
    @app.post("/api/v1/devices/register", status_code=201)
    def register(body: schemas.Register, db: Session = Depends(get_db), _=Depends(require_enroll)):
        dev_id = "dev_" + secrets.token_hex(6)
        token = new_device_token()
        db.add(Device(id=dev_id, name=body.name, info=body.info, token_hash=hash_token(token)))
        audit(db, "enroll", "device_registered", device_id=dev_id, detail=body.name)
        db.commit()
        return {"device_id": dev_id, "device_token": token,
                "note": "Store device_token now; it is never shown again."}

    @app.post("/api/v1/devices/heartbeat")
    def heartbeat(body: schemas.Heartbeat, dev: Device = Depends(require_device),
                  db: Session = Depends(get_db)):
        dev = db.merge(dev)
        dev.last_seen, dev.agent_state = utcnow(), body.agent_state
        cancels = [r for (r,) in db.execute(select(Task.request_id).where(
            Task.device_id == dev.id, Task.cancel_requested.is_(True),
            Task.status.in_(("queued", "claimed", "running"))))]
        db.commit()
        return {"ok": True, "server_time": iso(utcnow()), "cancel_requests": cancels}

    @app.get("/api/v1/devices/status")
    def devices_status(device_id: str | None = None, db: Session = Depends(get_db),
                       _=Depends(require_brain)):
        q = select(Device).where(Device.revoked.is_(False))
        if device_id:
            q = q.where(Device.id == device_id)
        devs = db.scalars(q).all()
        if device_id and not devs:
            raise HTTPException(404, "unknown device")
        return {"devices": [device_json(d, settings) for d in devs]}

    # ------------------------------------------------------------ brain tasks
    @app.post("/api/v1/tasks")
    def create_task(body: schemas.TaskCreate, db: Session = Depends(get_db), _=Depends(require_brain)):
        if len(json.dumps(body.arguments)) > settings.max_args_bytes:
            raise HTTPException(413, "arguments too large")
        dev = db.get(Device, body.device_id)
        if dev is None or dev.revoked:
            raise HTTPException(404, "unknown or revoked device")
        existing = db.scalars(select(Task).where(Task.request_id == body.request_id)).one_or_none()
        if existing:
            same = (existing.device_id, existing.task_type, existing.arguments) == (
                body.device_id, body.task_type, body.arguments)
            if not same:
                raise HTTPException(409, "request_id already used with different content")
            return JSONResponse(task_json(existing), status_code=200)  # idempotent replay
        t = Task(request_id=body.request_id, device_id=body.device_id, task_type=body.task_type,
                 arguments=body.arguments, reason=body.reason, timeout_seconds=body.timeout_seconds)
        db.add(t)
        audit(db, "brain", "task_submitted", t.request_id, t.device_id, t.task_type)
        db.commit()
        return JSONResponse(task_json(t), status_code=201)

    @app.get("/api/v1/tasks")
    def list_tasks(status: str | None = None, device_id: str | None = None,
                   limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db),
                   _=Depends(require_brain)):
        sweep(db, settings)
        q = select(Task).order_by(Task.id.desc()).limit(limit)
        if status:
            q = q.where(Task.status == status)
        if device_id:
            q = q.where(Task.device_id == device_id)
        return {"tasks": [task_json(t, full=False) for t in db.scalars(q)]}

    @app.get("/api/v1/tasks/{request_id}")
    def get_task(request_id: str, db: Session = Depends(get_db), _=Depends(require_brain)):
        sweep(db, settings)
        t = db.scalars(select(Task).where(Task.request_id == request_id)).one_or_none()
        if not t:
            raise HTTPException(404, "unknown request_id")
        return task_json(t)

    @app.post("/api/v1/tasks/{request_id}/cancel")
    def cancel_task(request_id: str, db: Session = Depends(get_db), _=Depends(require_brain)):
        t = db.scalars(select(Task).where(Task.request_id == request_id)).one_or_none()
        if not t:
            raise HTTPException(404, "unknown request_id")
        if t.status in TERMINAL:
            return task_json(t)
        t.cancel_requested = True
        if t.status == "queued":
            t.status, t.completed_at = "cancelled", utcnow()
        audit(db, "brain", "cancel_requested", t.request_id, t.device_id)
        db.commit()
        return task_json(t)

    # ------------------------------------------------------------ agent tasks
    @app.get("/api/v1/agent/tasks/next")
    def next_task(dev: Device = Depends(require_device), db: Session = Depends(get_db)):
        dev = db.merge(dev)
        dev.last_seen = utcnow()
        # Never hand out a new task while one is still in flight for this device.
        busy = db.scalars(select(Task.id).where(Task.device_id == dev.id,
                                                Task.status.in_(INFLIGHT)).limit(1)).first()
        if busy:
            db.commit()
            return {"task": None, "reason": "busy"}
        # Atomic claim: SKIP LOCKED on Postgres; single-writer on SQLite.
        t = db.scalars(select(Task).where(Task.device_id == dev.id, Task.status == "queued")
                       .order_by(Task.id).limit(1).with_for_update(skip_locked=True)).first()
        if not t:
            db.commit()
            return {"task": None}
        rows = db.execute(update(Task).where(Task.id == t.id, Task.status == "queued")
                          .values(status="claimed", claimed_at=utcnow())).rowcount
        if rows != 1:
            db.rollback()
            return {"task": None}
        audit(db, f"device:{dev.id}", "task_claimed", t.request_id, dev.id)
        db.commit()
        db.refresh(t)
        return {"task": task_json(t)}

    def _own_task(db: Session, dev: Device, request_id: str) -> Task:
        t = db.scalars(select(Task).where(Task.request_id == request_id).with_for_update()).one_or_none()
        if not t or t.device_id != dev.id:
            raise HTTPException(404, "unknown request_id")
        return t

    @app.post("/api/v1/tasks/{request_id}/decision")
    def decision(request_id: str, body: schemas.Decision, dev: Device = Depends(require_device),
                 db: Session = Depends(get_db)):
        t = _own_task(db, dev, request_id)
        new = {"approved": "running", "rejected": "rejected", "blocked": "blocked",
               "cancelled": "cancelled"}[body.decision]
        if t.status == new:  # idempotent retry
            return task_json(t, full=False)
        if t.status != "claimed":
            raise HTTPException(409, f"task is {t.status}; decision not allowed")
        now = utcnow()
        t.status, t.risk, t.decision_note, t.decided_at = new, body.risk, body.note, now
        if new == "running":
            t.started_at = now
        else:
            t.completed_at = now
        audit(db, f"device:{dev.id}", f"decision_{body.decision}", t.request_id, dev.id, body.note)
        if new in ("blocked", "rejected"):
            auto_lesson(db, t, new, body.note)
        db.commit()
        return task_json(t, full=False)

    @app.post("/api/v1/tasks/{request_id}/result")
    def result(request_id: str, body: schemas.Result, dev: Device = Depends(require_device),
               db: Session = Depends(get_db)):
        t = _own_task(db, dev, request_id)
        if t.status in TERMINAL:
            if t.status == body.status:
                return task_json(t, full=False)  # idempotent retry
            raise HTTPException(409, f"task already {t.status}")
        if t.status != "running":
            raise HTTPException(409, f"task is {t.status}; no approved execution to report")
        if body.data is not None and len(json.dumps(body.data)) > 2_000_000:
            raise HTTPException(413, "result data too large")
        cap = settings.max_output_bytes
        trunc = body.output_truncated or len(body.stdout) > cap or len(body.stderr) > cap
        t.status, t.exit_code, t.stdout, t.stderr = body.status, body.exit_code, body.stdout[:cap], body.stderr[:cap]
        t.output_truncated, t.error, t.result_data = trunc, body.error, body.data

        def parse(x):
            try:
                return aware(datetime.fromisoformat(x)) if x else None
            except ValueError:
                return None
        t.started_at = parse(body.started_at) or t.started_at
        t.completed_at = parse(body.completed_at) or utcnow()
        audit(db, f"device:{dev.id}", f"result_{body.status}", t.request_id, dev.id,
              f"exit={body.exit_code}")
        if body.status in ("failed", "timeout"):
            auto_lesson(db, t, body.status, body.error or (body.stderr or "")[-160:] or f"exit {body.exit_code}")
        db.commit()
        return task_json(t, full=False)

    # --------------------------------------------------------------- messages
    def source_of(ch: str) -> str:
        if ch == "app" or ch.startswith("app:"):
            return "the user typing in their laptop app chat"
        if ch.startswith("local:"):
            return "a local API client on the user's laptop (a separate conversation)"
        if ch.startswith("relay:"):
            return "a relay API client (a separate conversation)"
        return "another client"

    def msg_json(m: Message) -> dict:
        return {"id": m.id, "direction": m.direction, "channel": m.channel, "source": source_of(m.channel),
                "text": m.text, "created_at": iso(m.created_at)}

    def fetch_msgs(db: Session, device_id: str, directions, after_id: int, limit: int, channel: str | None = None):
        q = select(Message).where(Message.device_id == device_id, Message.direction.in_(directions))
        if channel:
            q = q.where(Message.channel == channel)
        if after_id <= 0:  # first call: newest `limit` messages, oldest first
            rows = list(reversed(db.scalars(q.order_by(Message.id.desc()).limit(limit)).all()))
        else:
            rows = db.scalars(q.where(Message.id > after_id).order_by(Message.id).limit(limit)).all()
        return rows

    @app.post("/api/v1/agent/messages", status_code=201)
    def user_says(body: schemas.UserMessage, dev: Device = Depends(require_device),
                  db: Session = Depends(get_db)):
        """Laptop app -> Brain: something the user typed."""
        m = Message(device_id=dev.id, direction="user", text=body.text, channel=body.channel)
        db.add(m)
        audit(db, f"device:{dev.id}", "user_message", device_id=dev.id, detail=body.text[:200])
        db.commit()
        return msg_json(m)

    @app.get("/api/v1/agent/messages")
    def brain_says(after_id: int = 0, limit: int = Query(20, ge=1, le=100),
                   dev: Device = Depends(require_device), db: Session = Depends(get_db)):
        """Brain -> laptop app."""
        rows = fetch_msgs(db, dev.id, ("brain",), after_id, limit)
        return {"messages": [msg_json(m) for m in rows], "last_id": rows[-1].id if rows else after_id}

    @app.get("/api/v1/messages")
    def brain_listens(device_id: str, after_id: int = 0, direction: str = "user", channel: str | None = None,
                      wait: int = Query(0, ge=0, le=25),
                      limit: int = Query(20, ge=1, le=100), db: Session = Depends(get_db),
                      _=Depends(require_brain)):
        """Brain polls what the user said (direction=user), optionally long-polling up to 25 s."""
        import time
        if direction not in ("user", "brain", "all"):
            raise HTTPException(422, "direction must be user, brain or all")
        dirs = ("user", "brain") if direction == "all" else (direction,)
        end = time.monotonic() + wait
        while True:
            rows = fetch_msgs(db, device_id, dirs, after_id, limit, channel)
            if rows or time.monotonic() >= end:
                break
            time.sleep(1)
            db.rollback()  # end the read transaction so the next poll sees new rows
        return {"messages": [msg_json(m) for m in rows], "last_id": rows[-1].id if rows else after_id}

    @app.post("/api/v1/messages", status_code=201)
    def brain_replies(body: schemas.BrainMessage, db: Session = Depends(get_db), _=Depends(require_brain)):
        dev = db.get(Device, body.device_id)
        if dev is None or dev.revoked:
            raise HTTPException(404, "unknown or revoked device")
        m = Message(device_id=dev.id, direction="brain", text=body.text, channel=body.channel)
        db.add(m)
        audit(db, "brain", "brain_message", device_id=dev.id, detail=body.text[:200])
        db.commit()
        return msg_json(m)

    # ------------------------------------------------------- brain memory
    @app.get("/api/v1/memory/brief")
    def memory_brief(db: Session = Depends(get_db), _=Depends(require_brain)):
        """ONE call at session start: everything worth remembering, as a compact digest (<= ~6000 chars)."""
        def pick(kind, n, order):
            return db.scalars(select(Memory).where(Memory.kind == kind).order_by(*order).limit(n)).all()
        pinned = db.scalars(select(Memory).where(Memory.pinned.is_(True)).order_by(Memory.updated_at.desc()).limit(20)).all()
        sections = [("PINNED", pinned),
                    ("USER PREFERENCES", pick("preference", 15, [Memory.updated_at.desc()])),
                    ("LESSONS (mistakes to avoid)", pick("lesson", 20, [Memory.count.desc(), Memory.updated_at.desc()])),
                    ("SKILLS (procedures that worked)", pick("skill", 15, [Memory.updated_at.desc()])),
                    ("PROJECTS", pick("project", 10, [Memory.updated_at.desc()])),
                    ("RECENT NOTES", pick("note", 10, [Memory.updated_at.desc()]))]
        seen, out = set(), []
        for name, rows in sections:
            lines = []
            for m in rows:
                if m.id in seen:
                    continue
                seen.add(m.id)
                body = " ".join((m.body or "").split())
                lines.append(f"- [{m.id}] {m.title}" + (f" (x{m.count})" if m.count > 1 else "") + (f": {body[:300]}" if body else ""))
            if lines:
                out.append(f"## {name}\n" + "\n".join(lines))
        text = "\n\n".join(out)[:6000] or "(memory is empty)"
        return {"text": text, "total_entries": db.scalar(select(func.count(Memory.id)))}

    @app.get("/api/v1/memory")
    def memory_list(kind: str | None = None, q: str | None = None, limit: int = Query(30, ge=1, le=100),
                    db: Session = Depends(get_db), _=Depends(require_brain)):
        stmt = select(Memory).order_by(Memory.pinned.desc(), Memory.updated_at.desc()).limit(limit)
        if kind:
            stmt = stmt.where(Memory.kind == kind)
        if q:
            like = f"%{q.lower()}%"
            stmt = stmt.where(func.lower(Memory.title).like(like) | func.lower(Memory.body).like(like)
                              | func.lower(Memory.tags).like(like))
        return {"entries": [mem_json(m) for m in db.scalars(stmt)]}

    @app.post("/api/v1/memory", status_code=201)
    def memory_save(body: schemas.MemoryIn, db: Session = Depends(get_db), _=Depends(require_brain)):
        """Create or update (same kind+title = update). Keep entries short and free of secrets."""
        m = upsert_memory(db, body.kind, body.title.strip(), body.body, body.tags, body.pinned, "brain")
        audit(db, "brain", "memory_saved", detail=f"{body.kind}: {body.title[:80]}")
        db.commit()
        return mem_json(m)

    @app.delete("/api/v1/memory/{mem_id}")
    def memory_delete(mem_id: int, db: Session = Depends(get_db), _=Depends(require_brain)):
        m = db.get(Memory, mem_id)
        if not m:
            raise HTTPException(404, "unknown memory id")
        db.delete(m)
        db.commit()
        return {"ok": True}

    # ------------------------------------------------- user <-> brain (API key)
    def _chat_device(db: Session, device_id: str | None) -> Device:
        q = select(Device).where(Device.revoked.is_(False))
        if device_id:
            q = q.where(Device.id == device_id)
        dev = db.scalars(q.order_by(Device.last_seen.desc())).first()
        if not dev:
            raise HTTPException(404, "no such device")
        return dev

    @app.post("/api/v1/chat", status_code=201)
    def user_chat(body: schemas.ChatIn, db: Session = Depends(get_db), _=Depends(require_user)):
        """You (curl/script, any machine) -> Brain. Waits up to `wait` s for the Brain's next reply."""
        import time
        dev = _chat_device(db, body.device_id)
        chan = f"relay:{body.chat_id}"
        last_brain = db.scalars(select(Message.id).where(Message.device_id == dev.id, Message.direction == "brain",
                                                         Message.channel == chan)
                                .order_by(Message.id.desc()).limit(1)).first() or 0
        m = Message(device_id=dev.id, direction="user", text=body.text, channel=chan)
        db.add(m)
        audit(db, "user-api", "user_message", device_id=dev.id, detail=body.text[:200])
        db.commit()
        end, replies = time.monotonic() + body.wait, []
        while True:
            db.rollback()
            replies = fetch_msgs(db, dev.id, ("brain",), last_brain, 20, chan)   # ONLY this conversation
            if replies or time.monotonic() >= end:
                break
            time.sleep(1)
        return {"sent": msg_json(m), "device_id": dev.id, "replies": [msg_json(r) for r in replies]}

    @app.get("/api/v1/chat")
    def user_chat_history(after_id: int = 0, limit: int = Query(20, ge=1, le=100), device_id: str | None = None,
                          chat_id: str = "default", db: Session = Depends(get_db), _=Depends(require_user)):
        dev = _chat_device(db, device_id)
        rows = fetch_msgs(db, dev.id, ("user", "brain"), after_id, limit, f"relay:{chat_id}")
        return {"device_id": dev.id, "messages": [msg_json(r) for r in rows],
                "last_id": rows[-1].id if rows else after_id}

    # ----------------------------------------------------------------- admin
    @app.get("/api/v1/admin/overview")
    def overview(db: Session = Depends(get_db), _=Depends(require_admin)):
        sweep(db, settings)
        devs = db.scalars(select(Device).order_by(Device.created_at)).all()
        tasks = db.scalars(select(Task).order_by(Task.id.desc()).limit(200)).all()
        groups = {
            "active": [t for t in tasks if t.status in ("queued", "claimed", "running")],
            "completed": [t for t in tasks if t.status == "completed"],
            "failed": [t for t in tasks if t.status in ("failed", "timeout", "expired", "cancelled")],
            "rejected": [t for t in tasks if t.status in ("rejected", "blocked")],
        }
        logs = db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(100)).all()
        return {"devices": [device_json(d, settings) for d in devs],
                "counts": {k: len(v) for k, v in groups.items()},
                "tasks": {k: [task_json(t, full=False) for t in v] for k, v in groups.items()},
                "history": [task_json(t, full=False) for t in tasks],
                "memory": [mem_json(m) for m in db.scalars(select(Memory).order_by(Memory.updated_at.desc()).limit(200))],
                "audit": [{"ts": iso(a.ts), "actor": a.actor, "event": a.event,
                           "request_id": a.request_id, "device_id": a.device_id,
                           "detail": a.detail} for a in logs]}

    @app.delete("/api/v1/admin/memory/{mem_id}")
    def admin_memory_delete(mem_id: int, db: Session = Depends(get_db), _=Depends(require_admin)):
        m = db.get(Memory, mem_id)
        if m:
            db.delete(m)
            db.commit()
        return {"ok": True}

    @app.post("/api/v1/admin/devices/{device_id}/revoke")
    def revoke(device_id: str, db: Session = Depends(get_db), _=Depends(require_admin)):
        dev = db.get(Device, device_id)
        if not dev:
            raise HTTPException(404, "unknown device")
        dev.revoked, dev.agent_state = True, "offline"
        for t in db.scalars(select(Task).where(Task.device_id == device_id,
                                               Task.status.in_(("queued", "claimed", "running")))):
            t.status, t.error, t.completed_at = "cancelled", "device_revoked", utcnow()
        audit(db, "admin", "device_revoked", device_id=device_id)
        db.commit()
        return {"ok": True}

    @app.post("/api/v1/admin/devices/{device_id}/disconnect")
    def disconnect(device_id: str, db: Session = Depends(get_db), _=Depends(require_admin)):
        """Soft disconnect: stop dispatching; cancel queued tasks; device may re-heartbeat."""
        dev = db.get(Device, device_id)
        if not dev:
            raise HTTPException(404, "unknown device")
        for t in db.scalars(select(Task).where(Task.device_id == device_id, Task.status == "queued")):
            t.status, t.error, t.completed_at = "cancelled", "device_disconnected_by_admin", utcnow()
        audit(db, "admin", "device_disconnected", device_id=device_id)
        db.commit()
        return {"ok": True}

    @app.get("/")
    @app.get("/dashboard")
    def dashboard():
        return FileResponse(STATIC / "dashboard.html", media_type="text/html")

    return app
