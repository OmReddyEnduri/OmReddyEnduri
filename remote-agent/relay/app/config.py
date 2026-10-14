import os
from dataclasses import dataclass


def _tok(name: str, required: bool) -> str:
    v = os.environ.get(name, "")
    if required and len(v) < 24:
        raise RuntimeError(f"{name} must be set and at least 24 characters")
    return v


@dataclass
class Settings:
    database_url: str
    brain_token: str
    admin_token: str
    enroll_token: str
    user_token: str = ""            # optional: lets YOU talk to the Brain via /api/v1/chat
    online_window_s: int = 30        # heartbeat age that still counts as online
    lost_window_s: int = 90          # in-flight tasks fail after this much silence
    queue_ttl_s: int = 900           # queued tasks expire after this long
    max_output_bytes: int = 256 * 1024
    max_args_bytes: int = 8 * 1024 * 1024
    agent_online_s: int = 45          # an agent counts as online if it called the relay within this window
    message_ttl_s: int = 3600         # chat messages are a transit buffer: delivered ones are purged after this
    task_ttl_h: int = 48              # finished tasks (and their output) are purged after this


def load_settings() -> Settings:
    url = os.environ.get("DATABASE_URL", "sqlite:///./relay.db")
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return Settings(
        database_url=url,
        brain_token=_tok("BRAIN_API_TOKEN", True),
        admin_token=_tok("ADMIN_TOKEN", True),
        enroll_token=_tok("ENROLL_TOKEN", True),
        user_token=_tok("USER_API_TOKEN", False),
        online_window_s=int(os.environ.get("ONLINE_WINDOW_SECONDS", 30)),
        lost_window_s=int(os.environ.get("LOST_WINDOW_SECONDS", 90)),
        queue_ttl_s=int(os.environ.get("QUEUE_TTL_SECONDS", 900)),
        message_ttl_s=int(os.environ.get("MESSAGE_TTL_SECONDS", 3600)),
        task_ttl_h=int(os.environ.get("TASK_TTL_HOURS", 48)),
    )
