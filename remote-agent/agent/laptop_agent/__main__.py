import argparse
import getpass
import os
import platform
import signal
import socket
import sys
import time

from . import config as C


def is_privileged() -> bool:
    if os.name == "posix":
        return os.geteuid() == 0
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def cmd_init(a):
    from .client import register
    url = (a.relay_url or input("Relay URL (https://…): ")).rstrip("/")
    token = os.environ.get("ENROLL_TOKEN") or getpass.getpass("Enrollment token: ")
    ws = a.workspace or [input("Approved workspace directory (absolute path): ")]
    ws = [os.path.abspath(os.path.expanduser(w)) for w in ws]
    for w in ws:
        os.makedirs(w, exist_ok=True)
    name = a.name or socket.gethostname()
    info = {"os": platform.platform(), "hostname": socket.gethostname(), "python": platform.python_version()}
    cfg = C.Config(relay_url=url, workspaces=ws)
    cfg.validate()
    r = register(url, token, name, info)
    cfg.device_id, cfg.device_token = r["device_id"], r["device_token"]
    p = C.save(cfg)
    print(f"Registered device {cfg.device_id}. Config saved to {p} (mode 600).")


def cmd_workspace(a):
    cfg = C.load()
    cfg.workspaces = [os.path.abspath(os.path.expanduser(w)) for w in a.paths]
    cfg.validate()
    for w in cfg.workspaces:
        os.makedirs(w, exist_ok=True)
    C.save(cfg)
    print("Approved workspaces now:", *cfg.workspaces, sep="\n  ")
    print("Restart the agent for this to take effect.")


def cmd_run(a):
    if is_privileged():
        sys.exit("Refusing to run as administrator/root. Start the agent as a normal user.")
    cfg = C.load()
    cfg.validate()
    if C.ensure_local_key(cfg):
        print("Generated your local API key (see the Connect page in the app).")
    from .audit import Audit
    from .engine import AgentEngine
    if a.headless:
        from .console import ConsoleApprover
        from .localserver import LocalServer
        eng = AgentEngine(cfg, ConsoleApprover(), Audit(cfg.secrets))
        eng.add_listener(lambda k, d: print(f"[{k}] {d}") if k in ("state", "notice") else None)
        srv = LocalServer(eng, cfg.ui_port)
        srv.start()
        eng.start()
        print(f"Headless agent running. Local UI: {srv.url}")
        print(f"Local API: http://127.0.0.1:{cfg.ui_port}/v1  key: {cfg.local_api_key}")
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))
        try:
            while eng.state not in ("auth_failed", "disconnected"):
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        eng.shutdown()
        srv.stop()
        return
    from .gui import run_gui
    sys.exit(run_gui(lambda ap: AgentEngine(cfg, ap, Audit(cfg.secrets)), cfg.ui_port))


def main():
    p = argparse.ArgumentParser(prog="laptop_agent")
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init", help="register this laptop with the relay")
    i.add_argument("--relay-url")
    i.add_argument("--name")
    i.add_argument("--workspace", action="append")
    i.set_defaults(fn=cmd_init)
    w = sub.add_parser("workspace", help="set the approved workspace folder(s), e.g. D:\\om")
    w.add_argument("paths", nargs="+")
    w.set_defaults(fn=cmd_workspace)
    r = sub.add_parser("run", help="start the agent")
    r.add_argument("--headless", action="store_true", help="no desktop window; console approvals")
    r.set_defaults(fn=cmd_run)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
