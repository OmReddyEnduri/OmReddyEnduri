"""Permission policy: the agent is the final authority. Classifies every task as
auto / ask / block BEFORE anything runs. Fail closed: unknown or malformed => block."""
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import config_dir

AUTO, ASK, BLOCK = "auto", "ask", "block"
MAX_READ = 1_000_000
MAX_WRITE = 5_000_000

SENSITIVE_PARTS = {".ssh", ".aws", ".gnupg", ".kube", ".docker", ".azure", ".config/gcloud"}
SENSITIVE_NAME = re.compile(
    r"(?i)^(\.env(\..+)?|id_(rsa|dsa|ecdsa|ed25519)(\.pub)?|.*\.(pem|key|p12|pfx|kdbx|ppk|keystore)|"
    r"\.npmrc|\.pypirc|\.netrc|\.git-credentials|credentials(\.json)?|secrets?(\..+)?|"
    r"shadow|passwd|login data|cookies|key[34]\.db|logins\.json)$")
SAFE_SUFFIX = (".example", ".sample", ".template")
PROTECTED_WRITE = re.compile(
    r"(?i)^(\.bashrc|\.bash_profile|\.zshrc|\.zprofile|\.profile|\.bash_logout|authorized_keys|"
    r"known_hosts|sudoers|crontab)$")

DANGEROUS_EXES = {
    "sudo", "su", "doas", "runas", "pkexec", "mkfs", "fdisk", "sfdisk", "parted", "wipefs",
    "diskpart", "format", "shutdown", "reboot", "halt", "poweroff", "init", "passwd", "visudo",
    "mimikatz", "bcdedit", "vssadmin", "cmdkey", "setenforce", "iptables", "ufw", "csrutil",
    "systemctl", "launchctl", "crontab", "at", "schtasks", "sc", "net", "reg", "regedit",
    "chown", "usermod", "useradd", "userdel", "chattr",
}
SHELLS = {"sh", "bash", "zsh", "fish", "dash", "ksh", "csh", "cmd", "powershell", "pwsh", "wsl"}
DELETERS = {"rm", "rmdir", "del", "erase", "rd", "remove-item", "unlink", "shred"}
STRING_DANGERS = [
    (re.compile(r"(?i)\brm\s+(-\S*\s+)*-\S*[rR]\S*\s+(-\S+\s+)*(/|~|\$HOME|\*|\.\.?)(\s|$|/\s)"), "recursive delete of a broad path"),
    (re.compile(r"(?i)\bmkfs(\.\w+)?\b|\bdiskpart\b|\bformat\s+[a-z]:|Format-Volume|\bwipefs\b"), "disk formatting"),
    (re.compile(r"(?i)\bdd\b[^\n]*\bof=/dev/"), "raw write to a block device"),
    (re.compile(r">\s*/dev/(sd|nvme|hd|disk)"), "raw write to a block device"),
    (re.compile(r":\(\)\s*\{"), "fork bomb"),
    (re.compile(r"(?i)(curl|wget|iwr|Invoke-WebRequest|irm)\b[^\n|]*\|\s*(sudo\s+)?(sh|bash|zsh|iex|Invoke-Expression|python\d*)\b"), "pipe from network to interpreter"),
    (re.compile(r"(?i)chmod\s+-R\s+[0-7]*7[0-7]*\s+/(\s|$)|chown\s+-R\s+\S+\s+/(\s|$)"), "recursive permission change on /"),
    (re.compile(r"(?i)Set-MpPreference|DisableRealtimeMonitoring|netsh\s+advfirewall\s+set\s+\S+\s+state\s+off|ufw\s+disable|setenforce\s+0|spctl\s+--master-disable|csrutil\s+disable|Stop-Service\s+.*(defender|windefend)"), "disables security protection"),
    (re.compile(r"(?i)Remove-Item\b[^\n]*-Recurse[^\n]*([A-Za-z]:\\?(\s|$|\")|\$env:(USERPROFILE|HOMEPATH|SystemRoot))"), "recursive delete of a broad path"),
    (re.compile(r"(?i)(\.ssh\b|id_rsa|id_ed25519|/etc/shadow|\.aws/credentials|\.gnupg|\.netrc|\.git-credentials|Login\s*Data|Cookies\.sqlite|logins\.json|security\s+find-(generic|internet)-password|\blsass\b|\bNTDS\.dit\b|\bSAM\b\s+(hive|save)|reg\s+save\s+hklm\\sam|vaultcmd|cmdkey\s+/list|mimikatz|dump.*(credential|password)|keychain)"), "credential access"),
    (re.compile(r"(?i)(laptop[-_ ]?agent|LaptopAgent)[/\\]|config\.json.*device_token|device_token"), "touches the agent's own configuration"),
]
PKG_NAME = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9._/@-]{0,100}(\[[A-Za-z0-9,_-]+\])?((==|>=|<=|~=|<|>|@)[A-Za-z0-9.*+!_-]{1,40})?$")
INTERPRETERS = {".py": "python3", ".sh": "bash", ".js": "node", ".mjs": "node", ".ps1": "pwsh", ".rb": "ruby"}
ALLOWED_INTERPRETERS = {"python3", "python", "bash", "sh", "node", "pwsh", "ruby"}


@dataclass
class Verdict:
    level: str
    operation: str
    arguments: dict
    cwd: str = ""
    files: list[str] = field(default_factory=list)
    impact: str = ""
    reasons: list[str] = field(default_factory=list)
    preview: str = ""
    plan: dict = field(default_factory=dict)   # normalized values the executor will use


class Policy:
    def __init__(self, workspaces: list[str]):
        self.workspaces = [os.path.realpath(w) for w in workspaces]
        self.protected = [os.path.realpath(str(config_dir()))]

    # ---- path helpers
    def _real(self, p: str) -> str:
        p = os.path.expanduser(p)
        if not os.path.isabs(p):
            p = os.path.join(self.workspaces[0], p)
        p = os.path.normpath(p)
        head, tail = p, []
        while not os.path.lexists(head) and os.path.dirname(head) != head:
            head, t = os.path.split(head)
            tail.insert(0, t)
        return os.path.join(os.path.realpath(head), *tail)

    def in_workspace(self, real: str) -> bool:
        return any(os.path.commonpath([real, w]) == w for w in self.workspaces)

    def is_workspace_root(self, real: str) -> bool:
        return real in self.workspaces

    def _touches_protected(self, real: str) -> bool:
        return any(os.path.commonpath([real, p]) == p for p in self.protected)

    @staticmethod
    def _sensitive(real: str) -> bool:
        base = os.path.basename(real)
        if base.lower().endswith(SAFE_SUFFIX):
            return False
        parts = set(Path(real).parts)
        return bool(SENSITIVE_NAME.match(base)) or bool(parts & SENSITIVE_PARTS)

    def _path(self, v: Verdict, raw, *, must_be_inside=True):
        if not isinstance(raw, str) or not raw or "\x00" in raw:
            raise ValueError("path must be a non-empty string")
        real = self._real(raw)
        if must_be_inside and not self.in_workspace(real):
            raise PermissionError(f"path is outside the approved workspaces: {real}")
        if self._touches_protected(real):
            raise PermissionError("path is inside the agent's own configuration directory")
        return real

    # ---- main entry
    def classify(self, task_type: str, args: dict) -> Verdict:
        v = Verdict(BLOCK, task_type, args if isinstance(args, dict) else {})
        try:
            if not isinstance(args, dict):
                raise ValueError("arguments must be an object")
            fn = getattr(self, f"_c_{task_type}", None)
            if fn is None:
                raise PermissionError(f"unknown task_type: {task_type}")
            fn(v, args)
        except PermissionError as e:
            v.level, v.reasons = BLOCK, v.reasons + [str(e)]
        except (ValueError, TypeError, KeyError) as e:
            v.level, v.reasons = BLOCK, v.reasons + [f"invalid arguments: {e}"]
        return v

    def _c_device_status(self, v, a):
        v.level, v.impact = AUTO, "Read-only system summary."

    def _c_list_dir(self, v, a):
        real = self._path(v, a.get("path", "."))
        if self._sensitive(real):
            raise PermissionError("listing a credential directory is not allowed")
        v.plan, v.files, v.level, v.impact = {"path": real}, [real], AUTO, "Read-only directory listing."

    def _c_read_file(self, v, a):
        real = self._path(v, a.get("path"))
        if self._sensitive(real):
            raise PermissionError("reading credential/secret files is blocked")
        if os.path.isfile(real) and os.path.getsize(real) > MAX_READ:
            v.level, v.impact = ASK, "Large file read (>1 MB, will be truncated)."
        else:
            v.level, v.impact = AUTO, "Read-only text file read."
        mb = int(a.get("max_bytes", 200_000))
        v.plan, v.files = {"path": real, "max_bytes": max(1, min(mb, MAX_READ))}, [real]

    def _write_checks(self, real: str):
        if self._sensitive(real) or PROTECTED_WRITE.match(os.path.basename(real)):
            raise PermissionError("writing to credential, shell-startup or key files is blocked")
        if ".git" in Path(real).parts and "hooks" in Path(real).parts:
            raise PermissionError("writing git hooks is blocked")

    def _c_write_file(self, v, a):
        real = self._path(v, a.get("path"))
        self._write_checks(real)
        content = a.get("content")
        if not isinstance(content, str) or len(content.encode()) > MAX_WRITE:
            raise ValueError("content must be a string of at most 5 MB")
        exists = os.path.exists(real)
        if os.path.isdir(real):
            raise ValueError("path is a directory")
        overwrite = bool(a.get("overwrite", False))
        if exists and not overwrite:
            raise ValueError("file exists; set overwrite=true to modify it")
        v.plan = {"path": real, "content": content}
        v.files, v.level = [real], ASK
        v.impact = (f"{'Overwrites existing' if exists else 'Creates new'} file ({len(content.encode())} bytes).")
        v.preview = content[:3000]

    def _c_make_dir(self, v, a):
        real = self._path(v, a.get("path"))
        v.plan, v.files, v.level, v.impact = {"path": real}, [real], ASK, "Creates a directory (and parents)."

    def _c_delete_file(self, v, a):
        real = self._path(v, a.get("path"))
        self._write_checks(real)
        if self.is_workspace_root(real) or os.path.isdir(real):
            raise PermissionError("deleting directories or workspace roots is blocked")
        if not os.path.exists(real):
            raise ValueError("file does not exist")
        v.plan, v.files, v.level, v.impact = {"path": real}, [real], ASK, "Permanently deletes one file."

    def _cwd(self, v, a):
        cwd = self._path(v, a.get("cwd") or self.workspaces[0])
        if not os.path.isdir(cwd):
            raise ValueError("cwd is not an existing directory")
        v.cwd = cwd
        return cwd

    def _scan(self, text: str):
        for rx, why in STRING_DANGERS:
            if rx.search(text):
                raise PermissionError(f"blocked: {why}")

    def _check_argv(self, argv, cwd):
        if not (isinstance(argv, list) and argv and all(isinstance(x, str) and "\x00" not in x for x in argv)):
            raise ValueError("argv must be a non-empty list of strings (no shell strings)")
        if len(argv) > 200 or sum(map(len, argv)) > 20000:
            raise ValueError("argv too large")
        exe = os.path.basename(argv[0]).lower()
        exe = re.sub(r"\.(exe|bat|cmd|com)$", "", exe)
        if exe in DANGEROUS_EXES:
            raise PermissionError(f"blocked executable: {exe}")
        self._scan(" ".join(argv))
        if exe in DELETERS:
            flags = [x for x in argv[1:] if x.startswith("-") or x.startswith("/")]
            recursive = any(re.search(r"^-[a-zA-Z]*[rR]|^--recursive$|^/s$|^-Recurse$", f, re.I) for f in flags)
            if recursive:
                targets = [x for x in argv[1:] if not (x.startswith("-") or (x.startswith("/") and len(x) <= 3))]
                if not targets:
                    raise PermissionError("recursive delete without explicit target")
                for t in targets:
                    if any(c in t for c in "*?[~$"):
                        raise PermissionError("recursive delete with glob/variable target")
                    r = self._real(os.path.join(cwd, t) if not os.path.isabs(t) else t)
                    if not self.in_workspace(r) or self.is_workspace_root(r):
                        raise PermissionError(f"recursive delete outside/at workspace root: {r}")
        if exe in SHELLS:
            for tok in argv[1:]:
                self._scan(tok)
        for tok in argv[1:]:
            for p in self.protected:
                if p.lower() in tok.lower():
                    raise PermissionError("argument references the agent's own configuration")
        return exe

    def _c_run_command(self, v, a):
        cwd = self._cwd(v, a)
        argv = a.get("argv")
        exe = self._check_argv(argv, cwd)
        v.plan = {"argv": argv, "cwd": cwd}
        v.level = ASK
        v.impact = ("Runs a shell interpreter: arbitrary side effects possible." if exe in SHELLS
                    else "Runs a program inside the workspace; side effects depend on the program.")

    def _c_run_script(self, v, a):
        real = self._path(v, a.get("path"))
        if not os.path.isfile(real):
            raise ValueError("script does not exist")
        if os.path.getsize(real) > 200_000:
            raise ValueError("script too large to review (>200 KB)")
        cwd = self._cwd(v, {"cwd": a.get("cwd") or os.path.dirname(real)})
        interp = a.get("interpreter") or INTERPRETERS.get(os.path.splitext(real)[1].lower())
        if interp not in ALLOWED_INTERPRETERS:
            raise PermissionError(f"unsupported or disallowed interpreter: {interp}")
        text = Path(real).read_text(errors="replace")
        self._scan(text)
        args = a.get("args", [])
        if not (isinstance(args, list) and all(isinstance(x, str) for x in args)):
            raise ValueError("args must be a list of strings")
        self._check_argv([interp, real, *args], cwd)
        v.plan = {"argv": [interp, real, *args], "cwd": cwd}
        v.files, v.level, v.preview = [real], ASK, text[:3000]
        v.impact = "Executes this script; it may create, change or delete files and use the network."

    def _c_install_package(self, v, a):
        mgr, pk = a.get("manager"), a.get("packages")
        if mgr not in ("pip", "npm"):
            raise ValueError("manager must be pip or npm")
        if not (isinstance(pk, list) and 1 <= len(pk) <= 20 and all(isinstance(x, str) and PKG_NAME.match(x) for x in pk)):
            raise ValueError("packages must be 1-20 plain package specifiers (no URLs/flags)")
        if any(x.startswith("-") or "://" in x or x.startswith("git+") for x in pk):
            raise PermissionError("URL/VCS/option package specifiers are blocked")
        cwd = self._cwd(v, a)
        argv = (["python3", "-m", "pip", "install", "--disable-pip-version-check", "--"] + pk) if mgr == "pip" \
            else ["npm", "install", "--ignore-scripts", "--"] + pk
        v.plan = {"argv": argv, "cwd": cwd}
        v.level = ASK
        v.impact = f"Downloads and installs {len(pk)} {mgr} package(s) from the public registry."
