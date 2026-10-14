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


class BrainMessage(BaseModel):
    device_id: str
    text: str = Field(min_length=1, max_length=4000)


class ChatIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    device_id: str | None = None
    wait: int = Field(default=20, ge=0, le=25)


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
