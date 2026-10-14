import re
from typing import Any, Literal
from pydantic import BaseModel, Field, field_validator

ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")

TASK_TYPES = (
    "device_status", "list_dir", "read_file", "write_file", "make_dir",
    "delete_file", "run_command", "run_script", "install_package",
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
