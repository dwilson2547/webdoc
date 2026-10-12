"""Section-level edits on markdown (design §7).

A section is an ATX heading ("## Auth") and everything after it up to the next
heading of the same or a higher level, so it includes its subsections.
Headings inside fenced code blocks don't count. A section is named by its
heading path, e.g. ["Architecture", "Auth"]: each entry matches a heading's
text (case-insensitive) and each later entry must sit inside the one before,
at any depth. A path may skip levels as long as it stays unambiguous.

Edits splice whole lines and only normalise the blank lines at the splice
points, so the rest of the document comes back byte for byte.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


class SectionError(ValueError):
    pass


@dataclass(frozen=True)
class Heading:
    level: int
    text: str
    line: int          # index of the heading line
    end: int           # first line after the section
    path: tuple[str, ...]


def parse(body: str) -> list[Heading]:
    lines = body.split("\n")
    found: list[tuple[int, str, int]] = []
    fence: str | None = None
    for i, line in enumerate(lines):
        f = FENCE.match(line)
        if f:
            mark = f.group(1)
            if fence is None:
                fence = mark
            elif mark[0] == fence[0] and len(mark) >= len(fence) and not line.strip()[len(mark):]:
                fence = None
            continue
        if fence is not None:
            continue
        m = ATX.match(line)
        if m:
            found.append((len(m.group(1)), (m.group(2) or "").strip(), i))
    out: list[Heading] = []
    stack: list[tuple[int, str]] = []
    for k, (level, text, line) in enumerate(found):
        end = next((ln for lv, _, ln in found[k + 1:] if lv <= level), len(lines))
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, text))
        out.append(Heading(level, text, line, end, tuple(t for _, t in stack)))
    return out


def outline(body: str) -> list[dict]:
    return [{"path": list(h.path), "level": h.level} for h in parse(body)]


def _norm(t: str) -> str:
    return " ".join(t.lower().split())


def _matches(h: Heading, path: list[str]) -> bool:
    want = [_norm(p) for p in path]
    have = [_norm(p) for p in h.path]
    if not want or have[-1] != want[-1]:
        return False
    i = 0
    for part in have[:-1]:
        if i < len(want) - 1 and part == want[i]:
            i += 1
    return i == len(want) - 1


def find(body: str, path: list[str] | str) -> Heading:
    if isinstance(path, str):
        path = [path]
    path = [p for p in (path or []) if p and p.strip()]
    if not path:
        raise SectionError("heading_path is empty")
    heads = parse(body)
    hits = [h for h in heads if _matches(h, path)]
    if len(hits) == 1:
        return hits[0]
    shown = " > ".join(path)
    if not hits:
        known = "; ".join(" > ".join(h.path) for h in heads[:40]) or "the document has no headings"
        raise SectionError(f"no section {shown!r}. Sections: {known}")
    raise SectionError(f"{shown!r} matches {len(hits)} sections: "
                       + "; ".join(" > ".join(h.path) for h in hits)
                       + ". Give more of the path.")


def _block(content: str) -> list[str]:
    lines = (content or "").replace("\r\n", "\n").split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def _splice(pre: list[str], mid: list[str], post: list[str]) -> str:
    pre, post = list(pre), list(post)
    while pre and not pre[-1].strip():
        pre.pop()
    while post and not post[0].strip():
        post.pop(0)
    out: list[str] = []
    for part in (pre, mid, post):
        if part:
            if out:
                out.append("")
            out.extend(part)
    while out and not out[-1].strip():
        out.pop()
    text = "\n".join(out)
    return text + "\n" if text else ""


def get(body: str, path: list[str] | str) -> str:
    h = find(body, path)
    return "\n".join(body.split("\n")[h.line:h.end]).rstrip() + "\n"


def replace(body: str, path: list[str] | str, content: str) -> str:
    """Replace everything under the heading (subsections included); the
    heading line stays. A repeated heading at the top of `content` is dropped."""
    h = find(body, path)
    lines = body.split("\n")
    block = _block(content)
    if block:
        m = ATX.match(block[0])
        if m and len(m.group(1)) == h.level and _norm(m.group(2) or "") == _norm(h.text):
            block = _block("\n".join(block[1:]))
    return _splice(lines[:h.line + 1], block, lines[h.end:])


def append(body: str, path: list[str] | str, content: str) -> str:
    """Add content at the end of the section, after any subsections."""
    h = find(body, path)
    lines = body.split("\n")
    return _splice(lines[:h.end], _block(content), lines[h.end:])


def insert(body: str, heading: str, content: str = "", *, after: list[str] | None = None,
           parent: list[str] | None = None, level: int | None = None) -> str:
    """Add a new section: after an existing one (same level), as the last
    child of one (one level deeper), or at the end of the document."""
    heading = " ".join((heading or "").split())
    if not heading:
        raise SectionError("heading is empty")
    if after and parent:
        raise SectionError("give after or parent, not both")
    lines = body.split("\n")
    if after:
        h = find(body, after)
        pos, lv = h.end, level or h.level
    elif parent:
        h = find(body, parent)
        pos, lv = h.end, level or min(h.level + 1, 6)
    else:
        pos, lv = len(lines), level or 2
    if not 1 <= lv <= 6:
        raise SectionError("level must be 1-6")
    block = ["#" * lv + " " + heading] + ([""] + _block(content) if _block(content) else [])
    return _splice(lines[:pos], block, lines[pos:])


def delete(body: str, path: list[str] | str) -> str:
    h = find(body, path)
    lines = body.split("\n")
    return _splice(lines[:h.line], [], lines[h.end:])


def replace_text(body: str, old: str, new: str) -> str:
    """Exact-text replacement; `old` must occur exactly once."""
    if not old:
        raise SectionError("find text is empty")
    n = body.count(old)
    if n != 1:
        raise SectionError(f"find text occurs {n} times; it must occur exactly once"
                           + (" (add surrounding text to make it unique)" if n else ""))
    return body.replace(old, new, 1)


def apply(body: str, ops: list[dict]) -> tuple[str, list[str]]:
    """Apply edit operations in order. Returns the new body and a short
    description of each step, for the revision summary."""
    done: list[str] = []
    for i, op in enumerate(ops or []):
        kind = (op or {}).get("op")
        path = op.get("heading_path")
        where = " > ".join(path) if isinstance(path, list) else str(path or "")
        try:
            if kind == "replace_section":
                body = replace(body, path, op.get("content", ""))
                done.append(f"replaced §{where}")
            elif kind == "append_to_section":
                body = append(body, path, op.get("content", ""))
                done.append(f"appended to §{where}")
            elif kind == "insert_section":
                body = insert(body, op.get("heading", ""), op.get("content", ""),
                              after=op.get("after"), parent=op.get("parent"), level=op.get("level"))
                done.append(f"added §{op.get('heading', '')}")
            elif kind == "delete_section":
                body = delete(body, path)
                done.append(f"deleted §{where}")
            elif kind == "replace_text":
                body = replace_text(body, op.get("find", ""), op.get("replace", ""))
                done.append("replaced text")
            elif kind == "append":
                body = _splice(body.split("\n"), _block(op.get("content", "")), [])
                done.append("appended")
            else:
                raise SectionError(f"unknown op {kind!r}")
        except SectionError as exc:
            raise SectionError(f"op {i + 1} ({kind}): {exc}") from None
    return body, done
