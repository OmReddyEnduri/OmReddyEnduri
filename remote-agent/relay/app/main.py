"""Private relay between the Brain (LLM) and the laptop agent.

HTTPS + DB-backed polling only; no WebSockets, no inbound laptop port.
"""
import asyncio
import contextlib
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from . import schemas
from .auth import (get_db, hash_token, new_device_token, require_admin, require_brain,
                   require_device, require_enroll)
from .config import Settings, load_settings
from .db import AuditLog, Device, Task, make_session_factory, utcnow

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
        return {"ok": True}

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
        import json
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
        db.commit()
        return task_json(t, full=False)

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
                "audit": [{"ts": iso(a.ts), "actor": a.actor, "event": a.event,
                           "request_id": a.request_id, "device_id": a.device_id,
                           "detail": a.detail} for a in logs]}

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
