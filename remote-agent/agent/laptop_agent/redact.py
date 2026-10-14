import re

_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"\b(?:ghp|gho|ghs|ghu|github_pat)_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bdev_[A-Za-z0-9_-]{30,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)\b((?:[A-Za-z0-9_]*?)(?:password|passwd|secret|token|api[_-]?key|access[_-]?key)[A-Za-z0-9_]*)"
               r"(\s*[=:]\s*)(?!\[REDACTED\])(\"[^\"\n]{3,}\"|'[^'\n]{3,}'|[^\s\"',;]{3,})"),
]


def redact(text: str, extra_secrets: list[str] | None = None, strict_only: bool = False) -> str:
    if not text:
        return text
    for s in extra_secrets or []:
        if s and len(s) >= 6:
            text = text.replace(s, "[REDACTED]")
    for p in _PATTERNS[:-1]:
        text = p.sub("[REDACTED]", text)
    if strict_only:  # source code: only well-known token shapes, never rewrite `name = value`
        return text
    return _PATTERNS[-1].sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)
