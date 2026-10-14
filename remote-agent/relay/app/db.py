from datetime import datetime, timezone
from sqlalchemy import JSON, inspect, text, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Device(Base):
    __tablename__ = "devices"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    info: Mapped[dict] = mapped_column(JSON, default=dict)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    agent_state: Mapped[str] = mapped_column(String(32), default="offline")  # running|paused|offline
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    device_id: Mapped[str] = mapped_column(String(64), ForeignKey("devices.id"), index=True)
    task_type: Mapped[str] = mapped_column(String(64))
    arguments: Mapped[dict] = mapped_column(JSON, default=dict)
    reason: Mapped[str] = mapped_column(Text, default="")
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=60)
    # queued -> claimed -> (rejected|blocked|running) -> completed|failed|timeout|cancelled
    # plus: expired (never claimed), failed(device_disconnected)
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    risk: Mapped[str | None] = mapped_column(String(16), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stdout: Mapped[str | None] = mapped_column(Text, nullable=True)
    stderr: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    channel: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(80))
    event: Mapped[str] = mapped_column(String(64))
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    device_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(64), ForeignKey("devices.id"), index=True)
    direction: Mapped[str] = mapped_column(String(8))  # "user" (laptop -> brain) | "brain" (brain -> laptop)
    channel: Mapped[str] = mapped_column(String(64), default="app", server_default="app", index=True)
    to_agent: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)   # addressed to this agent (None = unrouted)
    from_agent: Mapped[str | None] = mapped_column(String(64), nullable=True)
    msg_kind: Mapped[str] = mapped_column(String(12), default="msg")                      # msg | notes (context notes for the laptop)
    context: Mapped[dict | None] = mapped_column(JSON, nullable=True)                     # context packet built by the laptop router (transient)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_to: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Agent(Base):
    """A Brain instance (one LLM Arena window). The laptop router decides which agent handles which chat."""
    __tablename__ = "agents"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), default="")
    info: Mapped[dict] = mapped_column(JSON, default=dict)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Memory(Base):
    """The Brain's persistent memory: notes, preferences, skills (procedures) and lessons (mistakes)."""
    __tablename__ = "memory"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)     # note|preference|skill|lesson|project
    title: Mapped[str] = mapped_column(String(200), index=True)
    body: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[str] = mapped_column(String(200), default="")
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(8), default="brain")   # brain|auto|user
    count: Mapped[int] = mapped_column(Integer, default=1)            # how often a lesson recurred
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


# columns added after the first deploy: create_all never alters existing tables
_NEW_COLUMNS = {
    "messages": {"channel": "VARCHAR(64) NOT NULL DEFAULT 'app'", "to_agent": "VARCHAR(64)", "from_agent": "VARCHAR(64)",
                 "msg_kind": "VARCHAR(12) NOT NULL DEFAULT 'msg'", "context": "JSON", "delivered_at": "{ts}",
                 "delivered_to": "VARCHAR(64)", "cancelled": "BOOLEAN NOT NULL DEFAULT FALSE"},
    "tasks": {"agent_id": "VARCHAR(64)", "channel": "VARCHAR(64)"},
}


def _ensure_columns(engine) -> None:
    insp = inspect(engine)
    ts = "TIMESTAMP WITH TIME ZONE" if engine.dialect.name == "postgresql" else "DATETIME"
    for table, cols in _NEW_COLUMNS.items():
        if table not in insp.get_table_names():
            continue
        have = {c["name"] for c in insp.get_columns(table)}
        for name, ddl in cols.items():
            if name not in have:
                with engine.begin() as cx:
                    cx.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl.replace('{ts}', ts)}"))
    with engine.begin() as cx:
        cx.execute(text("CREATE INDEX IF NOT EXISTS ix_messages_channel ON messages (channel)"))
        cx.execute(text("CREATE INDEX IF NOT EXISTS ix_messages_to_agent ON messages (to_agent)"))


def make_session_factory(url: str):
    kw = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kw["connect_args"] = {"check_same_thread": False}
        if ":memory:" in url:
            from sqlalchemy.pool import StaticPool
            kw["poolclass"] = StaticPool
    engine = create_engine(url, **kw)
    Base.metadata.create_all(engine)
    _ensure_columns(engine)
    return engine, sessionmaker(engine, expire_on_commit=False)
