import re
from typing import Any, Literal
from pydantic import BaseModel, Field, field_validator

ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")

TASK_TYPES = (
    "device_status", "list_dir", "read_file", "write_file", "make_dir",
    "delete_file", "run_command", "run_script", "install_package", "screenshot",
    "read_files", "write_files", "open_preview", "read_image", "video_frames", "download_file",
)


class TaskCreate(BaseModel):
    request_id: str
    device_id: str
    task_type: Literal[TASK_TYPES]  # type: ignore[valid-type]
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(min_length=1, max_length=2000)
    timeout_seconds: int = Field(default=60, ge=1, le=3600)
    agent_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:-]{1,64}$")   # which agent submitted it
    channel: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:-]{1,64}$")    # which chat it is for

    @field_validator("request_id")
    @classmethod
    def _rid(cls, v: str) -> str:
        if not ID_RE.match(v):
            raise ValueError("request_id must match [A-Za-z0-9_.:-]{1,128}")
        return v


class Register(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    info: dict[str, Any] = Field(default_factory=dict)


class Heartbeat(BaseModel):
    agent_state: Literal["running", "paused"] = "running"
    active_request_id: str | None = None


class Decision(BaseModel):
    decision: Literal["approved", "rejected", "blocked", "cancelled"]
    risk: Literal["auto", "ask", "block"] | None = None
    note: str | None = Field(default=None, max_length=2000)


class Result(BaseModel):
    status: Literal["completed", "failed", "timeout", "cancelled"]
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    output_truncated: bool = False
    error: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    data: dict[str, Any] | None = None


class UserMessage(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    channel: str = Field(default="app", pattern=r"^[A-Za-z0-9_.:-]{1,64}$")
    to_agent: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:-]{1,64}$")   # set by the laptop router
    context: dict | None = None                                                       # context packet for that agent


class BrainMessage(BaseModel):
    device_id: str
    text: str = Field(min_length=1, max_length=4000)
    channel: str = Field(default="app", pattern=r"^[A-Za-z0-9_.:-]{1,64}$")  # reply goes ONLY to this conversation
    agent_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:-]{1,64}$")  # which agent is replying
    kind: Literal["msg", "notes"] = "msg"   # "notes" = context notes for the laptop's chat memory (not shown as a chat message)


class ChatIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    device_id: str | None = None
    wait: int = Field(default=20, ge=0, le=25)
    chat_id: str = Field(default="default", pattern=r"^[A-Za-z0-9_.-]{1,40}$")   # separate conversations per id


class MemoryIn(BaseModel):
    """Lenient on purpose: an LLM client sends tags as a list, long bodies, upper-case kinds, etc."""
    kind: Literal["note", "preference", "skill", "lesson", "project"]
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=4000)
    tags: str = Field(default="", max_length=200)
    pinned: bool | None = None   # None = leave unchanged on update

    @field_validator("kind", mode="before")
    @classmethod
    def _kind(cls, v):
        return v.strip().lower() if isinstance(v, str) else v

    @field_validator("title", mode="before")
    @classmethod
    def _title(cls, v):
        return str(v).strip()[:200] if v is not None else v

    @field_validator("body", mode="before")
    @classmethod
    def _body(cls, v):
        return "" if v is None else str(v)[:4000]

    @field_validator("tags", mode="before")
    @classmethod
    def _tags(cls, v):
        if v is None:
            return ""
        if isinstance(v, (list, tuple)):
            v = ", ".join(str(x) for x in v)
        return str(v)[:200]


class AgentHello(BaseModel):
    """Register / check in. Send {} to be given a free name by the relay (e.g. "Nova"); send `agent_id` to resume
    an identity you already have; send `claim` ("Nova") to take over a name whose agent is offline."""
    agent_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:-]{1,64}$")
    claim: str | None = Field(default=None, max_length=40)
    name: str = Field(default="", max_length=40)
    info: dict[str, Any] = Field(default_factory=dict)


class AgentRename(BaseModel):
    name: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,39}$")


class RouteIn(BaseModel):
    to_agent: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,64}$")
    context: dict | None = None
