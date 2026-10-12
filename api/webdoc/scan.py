"""Second line of defence before anything goes public (design §12).

The first is the human gate: an agent only drafts, a person publishes after
reading. This scan runs at publish time on contributions, replies and
confirmation notes. Secrets (keys, tokens, passwords in assignments, private
key blocks, credentials in URLs) block publishing outright. Details that look
private (LAN addresses, internal host names, home directory paths, email
addresses) are warnings the person must acknowledge.

Findings carry a masked excerpt, never the full match, so the scan result
itself doesn't repeat a secret.
"""

from __future__ import annotations

import re

SECRETS: list[tuple[str, re.Pattern]] = [
    ("private key", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})")),
    ("GitLab token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("API key", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("JSON web token", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("webdoc token", re.compile(r"\bwd[opr]_[A-Za-z0-9_-]{20,}")),
    ("credentials in a URL", re.compile(r"\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]{3,}@")),
    ("password or secret", re.compile(
        r"(?i)\b(?:password|passwd|pwd|secret|client_secret|api[_-]?key|access[_-]?token|"
        r"auth[_-]?token|token)\b[\"']?\s*[:=]\s*[\"']?([^\s\"'`,;]{8,})")),
]

WARNINGS: list[tuple[str, re.Pattern]] = [
    ("private IP address", re.compile(
        r"\b(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b")),
    ("internal host name", re.compile(
        r"\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:local|lan|internal|intranet|corp|home\.arpa)\b", re.I)),
    ("home directory path", re.compile(r"(?:/home/|/Users/|C:\\Users\\)[A-Za-z0-9_.-]+")),
    ("email address", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
]

# Obvious placeholders in a "password = ..." line aren't secrets.
PLACEHOLDER = re.compile(r"(?i)^(?:<.*>|\$\{?.*|\{\{.*|x{3,}.*|\*{3,}.*|\.{3,}|changeme|"
                         r"example.*|your[_-]?.*|placeholder|redacted|secret|password|none|null|"
                         r"true|false|os\.environ.*|env\(.*|process\.env.*)$")
EXAMPLE_EMAIL = re.compile(r"(?i)@(?:example\.(?:com|org|net)|users\.noreply\.github\.com)$")


def _secret_like(value: str) -> bool:
    """A value after "password =" that looks generated rather than a word or
    a placeholder: letters and digits mixed, or long."""
    if PLACEHOLDER.match(value):
        return False
    mixed = re.search(r"\d", value) and re.search(r"[A-Za-z]", value)
    return bool(mixed) or len(value) >= 20


def _mask(text: str) -> str:
    text = text.strip()
    return text[:4] + "…" if len(text) > 6 else text[:2] + "…"


def scan(text: str) -> dict:
    """{"secrets": [...], "warnings": [...]}, each finding {kind, line, excerpt}."""
    secrets, warnings = [], []
    for n, line in enumerate((text or "").split("\n"), 1):
        for kind, pat in SECRETS:
            for m in pat.finditer(line):
                value = m.group(1) if pat.groups else m.group(0)
                if kind == "password or secret" and not _secret_like(value):
                    continue
                secrets.append({"kind": kind, "line": n, "excerpt": _mask(value)})
        for kind, pat in WARNINGS:
            for m in pat.finditer(line):
                if kind == "email address" and EXAMPLE_EMAIL.search(m.group(0)):
                    continue
                warnings.append({"kind": kind, "line": n, "excerpt": _mask(m.group(0))})
    return {"secrets": secrets[:50], "warnings": warnings[:50]}


def clean(result: dict) -> bool:
    return not result["secrets"] and not result["warnings"]


class Blocked(ValueError):
    """Publishing refused by the scan. `needs_ack` when only warnings stand in
    the way and the person may publish anyway."""

    def __init__(self, result: dict):
        self.result = result
        self.needs_ack = not result["secrets"]
        if result["secrets"]:
            kinds = sorted({f["kind"] for f in result["secrets"]})
            msg = ("this looks like it contains secrets (" + ", ".join(kinds) + "); remove them "
                   "before publishing")
        else:
            kinds = sorted({f["kind"] for f in result["warnings"]})
            msg = ("this may contain private details (" + ", ".join(kinds) + "); check them, "
                   "then publish anyway if they're meant to be public")
        super().__init__(msg)


def check(text: str, ack_warnings: bool = False) -> dict:
    """Raise Blocked unless the text may go public; returns the scan result."""
    r = scan(text)
    if r["secrets"] or (r["warnings"] and not ack_warnings):
        raise Blocked(r)
    return r
