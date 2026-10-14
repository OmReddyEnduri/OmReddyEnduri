"""Agent configuration. The device token is stored in a 0600 file under the user config dir."""
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlparse


def config_dir() -> Path:
    if os.environ.get("LAPTOP_AGENT_HOME"):
        return Path(os.environ["LAPTOP_AGENT_HOME"])
    if sys.platform.startswith("win"):
        return Path(os.environ.get("APPDATA", Path.home())) / "LaptopAgent"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "LaptopAgent"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "laptop-agent"


@dataclass
class Config:
    relay_url: str = ""
    device_id: str = ""
    device_token: str = ""
    workspaces: list[str] = field(default_factory=list)
    poll_interval_s: float = 3.0       # spec: 2-5 s
    heartbeat_interval_s: float = 10.0
    ui_port: int = 8765
    max_output_bytes: int = 128 * 1024
    max_task_seconds: int = 3600
    local_api_key: str = ""            # key for the localhost /v1 endpoint (generated on first run)
    security_level: int = 2            # 1 low (ask least) .. 3 high (ask most)

    def validate(self) -> None:
        u = urlparse(self.relay_url)
        local = u.hostname in ("127.0.0.1", "localhost", "::1")
        if u.scheme != "https" and not (u.scheme == "http" and local):
            raise ValueError("relay_url must be https:// (http:// allowed only for localhost testing)")
        if not (2 <= self.poll_interval_s <= 5):
            raise ValueError("poll_interval_s must be between 2 and 5")
        if self.security_level not in (1, 2, 3):
            raise ValueError("security_level must be 1, 2 or 3")
        if self.ui_port < 1024 or self.ui_port > 65535:
            raise ValueError("ui_port out of range")
        if not self.workspaces:
            raise ValueError("at least one approved workspace is required")
        for w in self.workspaces:
            if not os.path.isabs(w):
                raise ValueError(f"workspace must be absolute: {w}")

    @property
    def secrets(self) -> list[str]:
        return [self.device_token] if self.device_token else []


def config_path() -> Path:
    return config_dir() / "config.json"


def load() -> Config:
    p = config_path()
    if not p.exists():
        raise FileNotFoundError(f"No config at {p}. Run: python -m laptop_agent init")
    data = json.loads(p.read_text())
    cfg = Config(**{k: v for k, v in data.items() if k in Config.__dataclass_fields__})
    cfg.relay_url = cfg.relay_url.rstrip("/")
    return cfg


def ensure_local_key(cfg: Config) -> bool:
    """Generate the localhost API key on first run. Returns True if a new key was made."""
    if cfg.local_api_key:
        return False
    import secrets
    cfg.local_api_key = "lak_" + secrets.token_urlsafe(30)
    save(cfg)
    return True


def save(cfg: Config) -> Path:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(asdict(cfg), f, indent=2)
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return p
