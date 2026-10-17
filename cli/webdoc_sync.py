#!/usr/bin/env python3
"""webdoc-sync: keep a folder of markdown in step with a webdoc domain.

    webdoc-sync pull <domain> <dir> [--under <page>]   first pull: clone into dir
    webdoc-sync pull [<dir>]                           later pulls: update dir
    webdoc-sync status [<dir>]                         what changed, here and there
    webdoc-sync push [<dir>] [--delete] [--force] [--dry-run]

<domain> and <page> are ids or webdoc URLs (…/domain/<id>, …/d/<id>). The
server comes from --server or WEBDOC_URL on the first pull and is remembered.
The token is a personal access token (webdoc → Agents) from WEBDOC_TOKEN, or
~/.config/webdoc/token.

The folder is laid out like webdoc's export: `Page.md` beside a `Page/`
folder of its child pages, files under `_attachments/`. Frontmatter carries
each page's webdoc_id and the revision it was pulled at.

push sends only files changed since the last pull or push; a file whose page
changed on the server meanwhile is a conflict and is not written (--force
writes it anyway; the other version stays in the page's history). New files
become pages, moving a file to another folder moves its page, and the
frontmatter `title` is the title (the filename only sets the layout). Deleting
a file trashes its page only with --delete. pull never overwrites a file you
changed, and removes a file whose page is gone only if you hadn't changed it.

Standard library only; Python 3.9+. State lives in <dir>/.webdoc/state.json.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

STATE = ".webdoc/state.json"
ATTACH_DIR = "_attachments"
ID_RE = re.compile(r"[0-9a-f]{32}")


class SyncError(Exception):
    pass


# --- talking to the server -------------------------------------------------------

class Http:
    """urllib with a bearer token. Tests swap in another object with the same
    `request(method, path, body=None) -> bytes` method."""

    def __init__(self, server: str, token: str):
        self.server, self.token = server.rstrip("/"), token

    def request(self, method: str, path: str, body: bytes | None = None) -> bytes:
        req = urllib.request.Request(self.server + path, data=body, method=method, headers={
            "Authorization": f"Bearer {self.token}", "User-Agent": "webdoc-sync",
            **({"Content-Type": "application/zip"} if body is not None else {})})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return r.read()
        except urllib.error.HTTPError as exc:
            raise SyncError(f"{method} {path}: HTTP {exc.code}: {_detail(exc.read())}") from None
        except urllib.error.URLError as exc:
            raise SyncError(f"can't reach {self.server}: {exc.reason}") from None


def _detail(raw: bytes) -> str:
    try:
        d = json.loads(raw).get("detail")
        return d if isinstance(d, str) else json.dumps(d)
    except (ValueError, AttributeError):
        return raw.decode(errors="replace")[:300]


def find_token() -> str:
    tok = os.environ.get("WEBDOC_TOKEN", "").strip()
    if not tok:
        f = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "webdoc" / "token"
        if f.is_file():
            tok = f.read_text().strip()
    if not tok:
        raise SyncError("no token: set WEBDOC_TOKEN or write one to ~/.config/webdoc/token "
                        "(make it in webdoc under Agents)")
    return tok


def ref_id(ref: str, kind: str) -> str:
    """An id from an id or a webdoc URL."""
    m = re.search(r"/(?:domain|d)/([0-9a-f]{32})", ref)
    if m:
        return m.group(1)
    if ID_RE.fullmatch(ref.strip()):
        return ref.strip()
    raise SyncError(f"{ref!r} isn't a {kind} id or webdoc URL")


# --- local files -----------------------------------------------------------------

def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def frontmatter(data: bytes) -> dict:
    """The few frontmatter keys this tool reads (webdoc_id, revision), without
    a YAML parser."""
    text = data.decode("utf-8", errors="replace").replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 3)
    out: dict = {}
    for line in text[4:end if end > 0 else 0].split("\n"):
        k, sep, v = line.partition(":")
        if sep and k.strip() in ("webdoc_id", "revision"):
            v = v.strip().strip("'\"")
            out[k.strip()] = int(v) if k.strip() == "revision" and v.isdigit() else v
    return out


def set_frontmatter(data: bytes, values: dict) -> bytes:
    text = data.decode("utf-8", errors="replace")
    lines = [f"{k}: {v}" for k, v in values.items()]
    if text.startswith("---\n") and text.find("\n---", 3) > 0:
        end = text.find("\n---", 3)
        head = [ln for ln in text[4:end].split("\n")
                if ln.partition(":")[0].strip() not in values]
        return ("---\n" + "\n".join(head + lines) + text[end:]).encode()
    return ("---\n" + "\n".join(lines) + "\n---\n\n" + text).encode()


def local_files(root: Path) -> dict[str, bytes]:
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if p.is_file() and not any(part.startswith(".") for part in rel.split("/")):
            out[rel] = p.read_bytes()
    return out


def prune_dirs(root: Path, rel: str) -> None:
    d = (root / rel).parent
    while d != root and d.is_dir() and not any(d.iterdir()):
        d.rmdir()
        d = d.parent


# --- state -----------------------------------------------------------------------

class Repo:
    def __init__(self, root: Path, http=None):
        self.root = root
        self.path = root / STATE
        self.state = json.loads(self.path.read_text()) if self.path.is_file() else None
        self.http = http

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.state, indent=1, sort_keys=True))

    def need(self) -> dict:
        if self.state is None:
            raise SyncError(f"{self.root} isn't a webdoc-sync folder; pull <domain> <dir> first")
        return self.state

    def query(self) -> str:
        u = self.state.get("under_id")
        return f"?under={u}" if u else ""

    def by_id(self) -> dict[str, str]:
        return {v["id"]: k for k, v in self.state["files"].items()}


# --- commands --------------------------------------------------------------------

def clone(root: Path, http, domain: str, under: str | None, server: str) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    if (root / STATE).exists():
        raise SyncError(f"{root} is already a webdoc-sync folder; run pull there")
    if any(not p.name.startswith(".") for p in root.iterdir()):
        raise SyncError(f"{root} isn't empty; pull into a new or empty folder")
    repo = Repo(root, http)
    repo.state = {"server": server, "domain_id": domain, "under_id": under, "files": {},
                  "attachments": []}
    return pull(repo)


def pull(repo: Repo) -> dict:
    st = repo.need()
    manifest = json.loads(repo.http.request("GET", f"/api/domains/{st['domain_id']}/manifest"
                                                  + repo.query()))
    st["domain_slug"] = manifest["domain"]["slug"]
    st["root_parent_id"] = (manifest["under"] or {}).get("parent_id")
    z = zipfile.ZipFile(io.BytesIO(repo.http.request(
        "GET", f"/api/domains/{st['domain_id']}/export" + repo.query())))
    prefix = st["domain_slug"] + "/"
    server = {n[len(prefix):]: z.read(n) for n in z.namelist() if n.startswith(prefix)}
    by_id = repo.by_id()
    report = {"written": [], "removed": [], "kept": [], "moved": []}
    seen: set[str] = set()
    new_files: dict = {}

    for rel, data in server.items():
        if rel.startswith(ATTACH_DIR + "/"):
            target = repo.root / rel
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            continue
        fm = frontmatter(data)
        wid = fm.get("webdoc_id")
        if not wid:
            continue
        seen.add(wid)
        old = by_id.get(wid)
        entry = {"id": wid, "revision": fm.get("revision"), "sha": sha(data)}
        if old is not None:
            cur = repo.root / old
            if cur.is_file() and sha(cur.read_bytes()) != st["files"][old]["sha"]:
                report["kept"].append(old)           # changed here; push first
                new_files[old] = st["files"][old]
                continue
            if old != rel and cur.is_file():
                cur.unlink()
                prune_dirs(repo.root, old)
                report["moved"].append(f"{old} -> {rel}")
        target = repo.root / rel
        if target.is_file() and old != rel and rel not in st["files"] \
                and sha(target.read_bytes()) != entry["sha"]:
            report["kept"].append(rel)               # an untracked file in the way
            continue
        if not target.is_file() or target.read_bytes() != data:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            report["written"].append(rel)
        new_files[rel] = entry

    for rel, entry in st["files"].items():
        if entry["id"] in seen:
            continue
        cur = repo.root / rel
        if cur.is_file() and sha(cur.read_bytes()) != entry["sha"]:
            report["kept"].append(rel)               # gone there, changed here
            new_files[rel] = entry
        elif cur.is_file():
            cur.unlink()
            prune_dirs(repo.root, rel)
            report["removed"].append(rel)
    st["files"] = new_files
    st["attachments"] = manifest["attachments"]
    repo.save()
    return report


def changes(repo: Repo) -> dict:
    """Local changes since the last pull or push."""
    st = repo.need()
    files = local_files(repo.root)
    by_id = repo.by_id()
    out = {"changed": [], "new": [], "moved": [], "deleted": [], "files": files}
    present: set[str] = set()
    for rel, data in files.items():
        if not rel.endswith(".md") or rel.startswith(ATTACH_DIR + "/"):
            continue
        wid = frontmatter(data).get("webdoc_id")
        old = by_id.get(wid) if wid else None
        if old is None:
            out["new"].append(rel)
            continue
        present.add(wid)
        if old != rel:
            out["moved"].append(rel)
        elif sha(data) != st["files"][old]["sha"]:
            out["changed"].append(rel)
    out["deleted"] = [rel for rel, e in st["files"].items() if e["id"] not in present]
    return out


def status(repo: Repo) -> dict:
    c = changes(repo)
    st = repo.state
    manifest = json.loads(repo.http.request("GET", f"/api/domains/{st['domain_id']}/manifest"
                                                  + repo.query()))
    known = {e["id"]: e for e in st["files"].values()}
    server_changed = [p["title"] for p in manifest["pages"]
                      if p["id"] in known and p["revision"] != known[p["id"]]["revision"]]
    server_new = [p["title"] for p in manifest["pages"] if p["id"] not in known]
    server_gone = [rel for rel, e in st["files"].items()
                   if e["id"] not in {p["id"] for p in manifest["pages"]}]
    return {"changed": c["changed"], "new": c["new"], "moved": c["moved"], "deleted": c["deleted"],
            "server_changed": server_changed, "server_new": server_new, "server_gone": server_gone}


def push(repo: Repo, delete: bool = False, force: bool = False, dry_run: bool = False) -> dict:
    st = repo.need()
    c = changes(repo)
    send = c["changed"] + c["new"] + c["moved"]
    trash = [st["files"][rel]["id"] for rel in c["deleted"]] if delete else []
    if not send and not trash:
        return {"nothing": True, "deleted_kept": c["deleted"]}
    known = set(st.get("attachments", []))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, data in c["files"].items():
            parts = rel.split("/")
            if parts[0] == ATTACH_DIR and len(parts) > 1 and parts[1] in known:
                continue                              # already on the server
            z.writestr(rel, data)
        z.writestr(".webdoc/push.json", json.dumps(
            {"changed": send, "check_revisions": not force, "trash": trash}))
    q = {"dry_run": "true"} if dry_run else {}
    if st.get("root_parent_id"):
        q["under"] = st["root_parent_id"]
    path = f"/api/domains/{st['domain_id']}/import" + ("?" + urllib.parse.urlencode(q) if q else "")
    result = json.loads(repo.http.request("POST", path, buf.getvalue()))
    result["deleted_kept"] = [] if delete else c["deleted"]
    if dry_run:
        return result
    # New files learn their page id; written files count as pulled, so the
    # refresh below replaces them with the server's copy.
    for item in result["created"]:
        if not item.get("path"):
            continue
        p = repo.root / item["path"]
        data = set_frontmatter(p.read_bytes(), {"webdoc_id": item["id"]})
        p.write_bytes(data)
        st["files"][item["path"]] = {"id": item["id"], "revision": 0, "sha": sha(data)}
    for item in result["updated"] + result["unchanged"]:
        if item.get("path") and item["path"] in c["files"]:
            old = repo.by_id().get(item["id"])
            if old is not None and old != item["path"]:
                st["files"].pop(old)
            st["files"][item["path"]] = {"id": item["id"], "revision": item.get("revision"),
                                         "sha": sha(c["files"][item["path"]])}
    for rel in c["deleted"] if delete else []:
        st["files"].pop(rel, None)
    repo.save()
    result["pull"] = pull(repo)
    return result


# --- the command line ------------------------------------------------------------

def show(title: str, items) -> None:
    items = list(items)
    if items:
        print(f"{title}:")
        for x in items:
            print(f"  {x}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="webdoc-sync", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pull", help="clone a domain into a folder, or update one")
    p.add_argument("args", nargs="*", help="<domain> <dir> the first time; [<dir>] after")
    p.add_argument("--under", help="only this page and the pages below it (first pull)")
    p.add_argument("--server", default=os.environ.get("WEBDOC_URL"), help="webdoc's URL (first pull)")
    s = sub.add_parser("status", help="what changed here and on the server")
    s.add_argument("dir", nargs="?", default=".")
    u = sub.add_parser("push", help="send local changes")
    u.add_argument("dir", nargs="?", default=".")
    u.add_argument("--delete", action="store_true", help="trash pages whose files were deleted")
    u.add_argument("--force", action="store_true", help="write even where the server changed since")
    u.add_argument("--dry-run", action="store_true", help="show what would happen")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "pull" and len(a.args) == 2:
            if not a.server:
                raise SyncError("first pull needs --server or WEBDOC_URL")
            domain = ref_id(a.args[0], "domain")
            under = ref_id(a.under, "page") if a.under else None
            r = clone(Path(a.args[1]), Http(a.server, find_token()), domain, under, a.server)
            print(f"pulled {len(r['written'])} pages into {a.args[1]}")
            return 0
        root = Path(a.args[0] if a.cmd == "pull" and a.args else getattr(a, "dir", ".") or ".")
        repo = Repo(root)
        repo.http = Http(repo.need()["server"], find_token())
        if a.cmd == "pull":
            r = pull(repo)
            show("updated", r["written"])
            show("moved", r["moved"])
            show("removed (gone on the server)", r["removed"])
            show("kept (changed here; push or discard first)", r["kept"])
            if not any(r.values()):
                print("up to date")
        elif a.cmd == "status":
            r = status(repo)
            show("changed here", r["changed"])
            show("new here", r["new"])
            show("moved here", r["moved"])
            show("deleted here (push --delete trashes them)", r["deleted"])
            show("changed on the server", r["server_changed"])
            show("new on the server", r["server_new"])
            show("gone on the server", r["server_gone"])
            if not any(r.values()):
                print("in step")
        else:
            r = push(repo, a.delete, a.force, a.dry_run)
            if r.get("nothing"):
                print("nothing to push")
                show("deleted here (push --delete trashes them)", r["deleted_kept"])
                return 0
            prefix = "would be " if a.dry_run else ""
            show(prefix + "created", (x["path"] for x in r["created"]))
            show(prefix + "updated", (x["path"] for x in r["updated"]))
            show(prefix + "trashed", (x["title"] for x in r["trashed"]))
            show(prefix + "attached", (x["path"] for x in r["attachments"]))
            show("conflicts (changed on the server; pull, merge, push again, or --force)",
                 (f"{x['path']}: {x['reason']}" for x in r["conflicts"]))
            show("skipped", (f"{x['path']}: {x['reason']}" for x in r["skipped"]))
            show("warnings", r["warnings"])
            show("deleted here, not sent (use --delete)", r["deleted_kept"])
            return 1 if r["conflicts"] else 0
    except SyncError as exc:
        print(f"webdoc-sync: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
