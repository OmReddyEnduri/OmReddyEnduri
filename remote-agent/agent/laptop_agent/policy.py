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
INTERPRETERS = {".py": "python3", ".sh": "bash", ".js": "node", ".mjs": "node", ".ps1": "powershell", ".rb": "ruby"}
ALLOWED_INTERPRETERS = {"python3", "python", "bash", "sh", "node", "pwsh", "powershell", "ruby"}


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


LEVEL_NAMES = {1: "Low", 2: "Medium", 3: "High"}
LEVEL_HELP = {
    1: "Asks only before running programs/scripts, installing packages, deleting files or taking screenshots. "
       "Creating and editing files inside your workspace is automatic.",
    2: "Creating NEW files and folders inside your workspace is automatic. Asks before overwriting files, "
       "deleting, running programs/scripts, installing packages or taking screenshots.",
    3: "Asks before every change: writes, folders, deletes, programs, scripts, installs and screenshots. "
       "Only status, listing and reading are automatic.",
}
# The level can only RELAX file writes inside the workspace. Blocks never relax; running code,
# installing, deleting and screen capture always ask.


class Policy:
    def __init__(self, workspaces: list[str], level: int = 2):
        self.level = level
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

    @staticmethod
    def _within(real: str, root: str) -> bool:
        try:  # different Windows drives raise ValueError; compare case-insensitively on Windows
            return os.path.normcase(os.path.commonpath([real, root])) == os.path.normcase(root)
        except ValueError:
            return False

    def in_workspace(self, real: str) -> bool:
        return any(self._within(real, w) for w in self.workspaces)

    def is_workspace_root(self, real: str) -> bool:
        return os.path.normcase(real) in [os.path.normcase(w) for w in self.workspaces]

    def _touches_protected(self, real: str) -> bool:
        return any(self._within(real, p) for p in self.protected)

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
            self._apply_level(v, task_type)
        except PermissionError as e:
            v.level, v.reasons = BLOCK, v.reasons + [str(e)]
        except (ValueError, TypeError, KeyError) as e:
            v.level, v.reasons = BLOCK, v.reasons + [f"invalid arguments: {e}"]
        return v

    def _apply_level(self, v: Verdict, task_type: str) -> None:
        if v.level != ASK or self.level >= 3:
            return
        new_only = not v.plan.get("exists") and not any(f.get("exists") for f in v.plan.get("files", []))
        relax = (task_type == "make_dir"
                 or (task_type in ("write_file", "write_files") and (self.level == 1 or new_only)))
        if relax:
            v.level = AUTO
            v.reasons.append(f"auto-approved by security level {self.level} ({LEVEL_NAMES[self.level]})")

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
        v.plan = {"path": real, "content": content, "exists": exists}
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

    CODE_GLOBS = ["**/*.js", "**/*.jsx", "**/*.ts", "**/*.tsx", "**/*.mjs", "**/*.json", "**/*.css", "**/*.scss",
                  "**/*.html", "**/*.md", "**/*.py", "**/*.c", "**/*.cc", "**/*.cpp", "**/*.h", "**/*.hpp",
                  "**/*.java", "**/*.go", "**/*.rs", "**/*.cs", "**/*.sh", "**/*.ps1", "**/*.yml", "**/*.yaml",
                  "**/*.toml", "**/*.txt", "**/*.sql", "**/*.vue", "**/*.svelte"]
    SKIP_DIRS = ("node_modules", ".git", "dist", "build", ".next", "__pycache__", ".venv", "venv", "target", ".cache")

    def _c_read_files(self, v, a):
        paths, root = a.get("paths"), a.get("dir")
        if paths is None and root is None:
            raise ValueError("give 'paths' (list) and/or 'dir' (+ optional 'globs')")
        plan = {"files": [], "root": None, "globs": [], "max_files": max(1, min(int(a.get("max_files", 50)), 200)),
                "max_bytes_per_file": max(1000, min(int(a.get("max_bytes_per_file", 200_000)), 500_000)),
                "total_cap": 1_200_000, "skip_dirs": list(self.SKIP_DIRS)}
        if paths is not None:
            if not (isinstance(paths, list) and 1 <= len(paths) <= 200 and all(isinstance(x, str) for x in paths)):
                raise ValueError("paths must be a list of 1-200 strings")
            for raw in paths:
                real = self._path(v, raw)
                if self._sensitive(real):
                    raise PermissionError(f"reading credential/secret files is blocked: {real}")
                plan["files"].append(real)
        if root is not None:
            r = self._path(v, root)
            if self._sensitive(r):
                raise PermissionError("credential directory is blocked")
            if not os.path.isdir(r):
                raise ValueError("dir is not an existing directory")
            globs = a.get("globs") or self.CODE_GLOBS
            if not (isinstance(globs, list) and all(isinstance(g, str) and ".." not in g and not os.path.isabs(g) for g in globs)):
                raise ValueError("globs must be relative patterns without '..'")
            plan["root"], plan["globs"] = r, globs
        v.plan, v.files, v.level = plan, plan["files"] or [plan["root"]], AUTO
        v.impact = "Read-only: sends the contents of text/code files to the Brain (secret files are skipped)."

    def _c_write_files(self, v, a):
        files, overwrite = a.get("files"), bool(a.get("overwrite", False))
        if not (isinstance(files, list) and 1 <= len(files) <= 50):
            raise ValueError("files must be a list of 1-50 {path, content}")
        out, total, seen = [], 0, set()
        for f in files:
            if not (isinstance(f, dict) and isinstance(f.get("content"), str)):
                raise ValueError("each file needs path and string content")
            real = self._path(v, f.get("path"))
            self._write_checks(real)
            if os.path.isdir(real):
                raise ValueError(f"path is a directory: {real}")
            if os.path.normcase(real) in seen:
                raise ValueError(f"duplicate path: {real}")
            seen.add(os.path.normcase(real))
            if os.path.exists(real) and not overwrite:
                raise ValueError(f"file exists (set overwrite=true): {real}")
            total += len(f["content"].encode())
            out.append({"path": real, "content": f["content"], "exists": os.path.exists(real)})
        if total > MAX_WRITE:
            raise ValueError("total content exceeds 5 MB")
        n_over = sum(1 for f in out if f["exists"])
        v.plan, v.files, v.level = {"files": out}, [f["path"] for f in out], ASK
        v.impact = f"Creates {len(out) - n_over} and overwrites {n_over} file(s), {total} bytes total."
        v.preview = "\n\n".join(f"=== {f['path']} ===\n{f['content'][:800]}" for f in out[:4])

    def _c_screenshot(self, v, a):
        import time as _t
        raw = a.get("path") or os.path.join(self.workspaces[0], "screenshots",
                                            _t.strftime("screenshot-%Y%m%d-%H%M%S.png"))
        real = self._path(v, raw)
        if os.path.splitext(real)[1].lower() not in (".png", ".jpg", ".jpeg"):
            raise ValueError("path must end in .png or .jpg")
        if os.path.isdir(real):
            raise ValueError("path is a directory")
        ri = bool(a.get("return_image", True))
        mw = int(a.get("max_width", 1280))
        v.plan = {"path": real, "return_image": ri, "max_width": max(320, min(mw, 1920))}
        v.files, v.level = [real], ASK
        v.impact = ("Captures EVERYTHING visible on all screens (may include private content) and saves it to this file."
                    + (" A downscaled copy is also SENT TO THE BRAIN (the remote LLM)." if ri else ""))

    @staticmethod
    def _py(name: str) -> str:
        import sys
        return sys.executable if name in ("python", "python3", "py") else name

    def _c_run_command(self, v, a):
        cwd = self._cwd(v, a)
        argv = a.get("argv")
        exe = self._check_argv(argv, cwd)
        v.plan = {"argv": [self._py(argv[0]), *argv[1:]], "cwd": cwd}
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
        head = [interp, "-NoProfile", "-NonInteractive", "-File"] if interp in ("powershell", "pwsh") else [self._py(interp)]
        v.plan = {"argv": [*head, real, *args], "cwd": cwd}
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
        argv = ([self._py("python3"), "-m", "pip", "install", "--disable-pip-version-check", "--"] + pk) if mgr == "pip" \
            else ["npm", "install", "--ignore-scripts", "--"] + pk
        v.plan = {"argv": argv, "cwd": cwd}
        v.level = ASK
        v.impact = f"Downloads and installs {len(pk)} {mgr} package(s) from the public registry."
