"""The sync tool (cli/webdoc_sync.py) against the app, through a TestClient."""

import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from .conftest import H, make_token
from .test_api import new_doc, personal
from .test_attachments import PNG, upload

_spec = importlib.util.spec_from_file_location(
    "webdoc_sync", Path(__file__).resolve().parents[2] / "cli" / "webdoc_sync.py")
sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync)


class Via:
    """webdoc_sync.Http over a TestClient."""

    def __init__(self, app, token):
        self.c, self.h = TestClient(app), {"Authorization": f"Bearer {token}"}

    def request(self, method, path, body=None):
        r = self.c.request(method, path, content=body, headers=self.h)
        if r.status_code >= 400:
            raise sync.SyncError(f"HTTP {r.status_code}: {r.text}")
        return r.content


@pytest.fixture
def setup(app_and_provider, client, tmp_path):
    d = personal(client)
    top = new_doc(client, d["id"], "Infra")
    k8s = new_doc(client, d["id"], "k8s", parent_id=top["id"])
    a = upload(client, k8s).json()
    client.put(f"/api/documents/{k8s['id']}", json={"base_revision": 1,
               "body": f"Cluster. {a['markdown']}\n"}, headers=H)
    http = Via(app_and_provider[0], make_token(client, "sync"))
    root = tmp_path / "wiki"
    sync.clone(root, http, d["id"], None, "http://testserver")
    return client, d, root, http, {"top": top, "k8s": k8s}


def repo(root, http):
    return sync.Repo(root, http)


def test_clone_lays_out_the_tree(setup):
    client, d, root, http, docs = setup
    files = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
    att = [f for f in files if f.startswith("_attachments/")]
    assert [f for f in files if not f.startswith(("_attachments/", ".webdoc/"))] == \
        ["Infra.md", "Infra/k8s.md"]
    assert len(att) == 1 and (root / att[0]).read_bytes() == PNG
    assert "../_attachments/" in (root / "Infra/k8s.md").read_text()
    assert sync.status(repo(root, http)) == {
        "changed": [], "new": [], "moved": [], "deleted": [], "server_changed": [],
        "server_new": [], "server_gone": []}
    with pytest.raises(sync.SyncError, match="already"):
        sync.clone(root, http, d["id"], None, "x")


def test_edit_new_move_and_push(setup):
    client, d, root, http, docs = setup
    k8s = root / "Infra/k8s.md"
    k8s.write_text(k8s.read_text().replace("Cluster.", "The cluster."))
    (root / "Infra/Traefik.md").write_text("# Traefik\n\nIngress.\n")
    (root / "Apps").mkdir()
    (root / "Apps.md").write_text("---\ntitle: Apps\n---\nApps live here.\n")
    (root / "Apps/img.png").write_bytes(PNG)
    (root / "Apps/webdoc.md").write_text("# webdoc\n\n![shot](img.png)\n")
    st = sync.status(repo(root, http))
    assert st["changed"] == ["Infra/k8s.md"]
    assert sorted(st["new"]) == ["Apps.md", "Apps/webdoc.md", "Infra/Traefik.md"]
    r = sync.push(repo(root, http))
    assert [x["path"] for x in r["updated"]] == ["Infra/k8s.md"]
    assert sorted(x["path"] for x in r["created"]) == ["Apps.md", "Apps/webdoc.md", "Infra/Traefik.md"]
    assert r["conflicts"] == []
    body = client.get(f"/api/documents/{docs['k8s']['id']}").json()["body"]
    assert body.startswith("The cluster. ![shot one.png](/api/attachments/")
    # After the refresh pull everything is in step and the new files know their ids.
    assert sync.frontmatter((root / "Infra/Traefik.md").read_bytes())["webdoc_id"]
    assert not any(v for k, v in sync.status(repo(root, http)).items())
    # Moving a file moves its page.
    (root / "Infra/Traefik.md").rename(root / "Apps/Traefik.md")
    r = sync.push(repo(root, http))
    tid = sync.frontmatter((root / "Apps/Traefik.md").read_bytes())["webdoc_id"]
    page = client.get(f"/api/documents/{tid}").json()
    assert [p["title"] for p in page["path"]] == ["Apps"]
    assert not any(v for k, v in sync.status(repo(root, http)).items())


def test_conflicts_force_and_pull_keeps_local_edits(setup):
    client, d, root, http, docs = setup
    k8s = root / "Infra/k8s.md"
    k8s.write_text(k8s.read_text().replace("Cluster.", "Local edit."))
    client.put(f"/api/documents/{docs['k8s']['id']}", json={"base_revision": 2,
               "body": "Server edit.\n"}, headers=H)
    assert sync.status(repo(root, http))["server_changed"] == ["k8s"]
    # pull leaves the local edit alone.
    r = sync.pull(repo(root, http))
    assert r["kept"] == ["Infra/k8s.md"] and "Local edit." in k8s.read_text()
    r = sync.push(repo(root, http))
    assert [x["path"] for x in r["conflicts"]] == ["Infra/k8s.md"]
    assert client.get(f"/api/documents/{docs['k8s']['id']}").json()["body"] == "Server edit.\n"
    r = sync.push(repo(root, http), force=True)
    assert [x["path"] for x in r["updated"]] == ["Infra/k8s.md"]
    assert "Local edit." in client.get(f"/api/documents/{docs['k8s']['id']}").json()["body"]
    assert not any(v for k, v in sync.status(repo(root, http)).items())


def test_deletes_need_the_flag_and_server_changes_arrive(setup):
    client, d, root, http, docs = setup
    (root / "Infra/k8s.md").unlink()
    r = sync.push(repo(root, http))
    assert r.get("nothing") and r["deleted_kept"] == ["Infra/k8s.md"]
    r = sync.push(repo(root, http), delete=True)
    assert [x["title"] for x in r["trashed"]] == ["k8s"]
    assert client.get(f"/api/documents/{docs['k8s']['id']}").json()["deleted_at"]
    # A page added and one renamed on the server show up on pull; a page
    # trashed there disappears here.
    new_doc(client, d["id"], "Backups", parent_id=docs["top"]["id"])
    client.put(f"/api/documents/{docs['top']['id']}", json={"base_revision": 1,
               "title": "Infrastructure"}, headers=H)
    r = sync.pull(repo(root, http))
    assert sorted(r["written"]) == ["Infrastructure.md", "Infrastructure/Backups.md"]
    assert not (root / "Infra.md").exists()
    client.delete(f"/api/documents/{docs['top']['id']}", headers=H)
    r = sync.pull(repo(root, http))
    assert sorted(r["removed"]) == ["Infrastructure.md", "Infrastructure/Backups.md"]


def test_subtree_clone_pushes_under_its_parent(app_and_provider, client, tmp_path):
    d = personal(client)
    top = new_doc(client, d["id"], "Infra")
    k8s = new_doc(client, d["id"], "k8s", parent_id=top["id"])
    new_doc(client, d["id"], "Other")
    http = Via(app_and_provider[0], make_token(client, "sync"))
    root = tmp_path / "k8s"
    sync.clone(root, http, d["id"], k8s["id"], "http://testserver")
    assert sorted(p.name for p in root.iterdir() if not p.name.startswith(".")) == ["k8s.md"]
    (root / "Sibling.md").write_text("# Sibling\n")
    (root / "k8s").mkdir()
    (root / "k8s/Child.md").write_text("# Child\n")
    sync.push(repo(root, http))
    tree = {p["title"]: p["parent_id"] for p in
            client.get(f"/api/domains/{d['id']}/tree").json()["pages"]}
    assert tree["Sibling"] == top["id"] and tree["Child"] == k8s["id"] and tree["k8s"] == top["id"]


def test_cli_entry_point(setup, monkeypatch, capsys):
    client, d, root, http, docs = setup
    monkeypatch.setenv("WEBDOC_TOKEN", "wdp_x")
    monkeypatch.setattr(sync, "Http", lambda server, token: http)
    assert sync.main(["status", str(root)]) == 0
    assert "in step" in capsys.readouterr().out
    assert sync.main(["pull", "not-an-id", str(root / "x"), "--server", "http://x"]) == 2
    assert sync.ref_id(f"https://w.example/domain/{d['id']}", "domain") == d["id"]
