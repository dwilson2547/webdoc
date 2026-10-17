"""Archive import (importer.py), subtree export, the manifest, and tokens on the sync routes."""

import io
import json
import tarfile
import zipfile

from fastapi.testclient import TestClient

from .conftest import H, make_token
from .test_api import new_doc, personal
from .test_attachments import PNG, upload
from .test_tree import tree_of


def make_zip(files: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def do_import(c, d, data, headers=None, **params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    return c.post(f"/api/domains/{d['id']}/import" + (f"?{q}" if q else ""), content=data,
                  headers=headers if headers is not None else H)


def export(c, d, under=None):
    url = f"/api/domains/{d['id']}/export" + (f"?under={under['id']}" if under else "")
    return client_zip(c.get(url).content)


def client_zip(data: bytes) -> dict[str, bytes]:
    z = zipfile.ZipFile(io.BytesIO(data))
    return {n: z.read(n) for n in z.namelist()}


def test_round_trip_into_another_domain(client):
    d = personal(client)
    top = new_doc(client, d["id"], "Infra", tags=["ops"])
    doc = new_doc(client, d["id"], "Shots", parent_id=top["id"])
    new_doc(client, d["id"], "Loose", kind="note")
    a = upload(client, doc).json()
    client.put(f"/api/documents/{doc['id']}", json={"base_revision": 1,
               "body": f"See {a['markdown']}.\n", "fields": {"status": "draft"}}, headers=H)
    data = client.get(f"/api/domains/{d['id']}/export").content
    other = client.post("/api/domains", json={"name": "Copy"}, headers=H).json()
    r = do_import(client, other, data).json()
    assert sorted(x["title"] for x in r["created"]) == ["Infra", "Loose", "Shots"]
    assert [x["name"] for x in r["attachments"]] == ["shot one.png"]
    assert tree_of(client, other) == [("Infra", None, 0), ("Shots", "Infra", 0), ("Loose", None, 1)]
    shots = next(x for x in r["created"] if x["title"] == "Shots")
    got = client.get(f"/api/documents/{shots['id']}").json()
    assert got["fields"] == {"status": "draft"} and got["attachments"][0]["name"] == "shot one.png"
    new_url = got["attachments"][0]["url"]
    assert got["body"] == f"See ![shot one.png]({new_url}).\n"
    assert client.get(new_url).content == PNG
    loose = next(x for x in r["created"] if x["title"] == "Loose")
    assert client.get(f"/api/documents/{loose['id']}").json()["kind"] == "note"
    revs = client.get(f"/api/documents/{shots['id']}/revisions").json()["revisions"]
    assert revs[0]["source"] == "import"


def test_reimport_updates_in_place(client):
    d = personal(client)
    top = new_doc(client, d["id"], "Infra")
    doc = new_doc(client, d["id"], "k8s", parent_id=top["id"])
    files = export(client, d)
    r = do_import(client, d, make_zip(files)).json()
    assert r["created"] == [] and r["updated"] == []
    assert sorted(x["title"] for x in r["unchanged"]) == ["Infra", "k8s"]
    # Edit one file: only it gets a new revision.
    key = "notes/Infra/k8s.md"
    files[key] = files[key].replace(b"Ship.", b"Ship it.")
    r = do_import(client, d, make_zip(files)).json()
    assert [x["title"] for x in r["updated"]] == ["k8s"]
    got = client.get(f"/api/documents/{doc['id']}").json()
    assert got["revision"] == 2 and "Ship it." in got["body"]
    assert len(tree_of(client, d)) == 2


def test_generic_folder_of_markdown(client, store):
    d = personal(client)
    files = {
        "vault/Infra.md": "Infra overview, see [k8s](Infra/k8s.md).\n",
        "vault/Infra/k8s.md": "---\ntags: ops, k8s\nowner: dan\n---\n# Kubernetes\n\n![diagram](../img/a.png)\n",
        "vault/Apps/webdoc.md": "# webdoc\n\nNotes, e.g. `[x](img/a.png)`.\n\n```md\n![y](img/a.png)\n```\n",
        "vault/img/a.png": PNG,
        "vault/img/unused.png": PNG,
        "vault/.obsidian/app.json": "{}",
        "../evil.md": "x",
    }
    dry = do_import(client, d, make_zip(files), dry_run="true").json()
    assert dry["dry_run"] and tree_of(client, d) == [] and store.objects == {}
    assert sorted(x["title"] for x in dry["created"]) == ["Apps", "Infra", "Kubernetes", "webdoc"]
    r = do_import(client, d, make_zip(files)).json()
    assert tree_of(client, d) == [("Apps", None, 0), ("webdoc", "Apps", 0),
                                  ("Infra", None, 1), ("Kubernetes", "Infra", 0)]
    assert {x["path"] for x in r["skipped"]} == {"img/unused.png"}
    assert any("evil" in w for w in r["warnings"])
    k8s = next(x for x in r["created"] if x["title"] == "Kubernetes")
    infra = next(x for x in r["created"] if x["title"] == "Infra")
    got = client.get(f"/api/documents/{k8s['id']}").json()
    assert got["tags"] == ["k8s", "ops"] and got["fields"] == {"owner": "dan"}
    att = got["attachments"][0]
    assert got["body"] == f"# Kubernetes\n\n![diagram]({att['url']})\n"
    assert client.get(f"/api/documents/{infra['id']}").json()["body"] == \
        f"Infra overview, see [k8s](/d/{k8s['id']}).\n"
    # Links in code are examples: left alone, and not counted as uses of the file.
    web = next(x for x in r["created"] if x["title"] == "webdoc")
    assert client.get(f"/api/documents/{web['id']}").json()["body"] == files["vault/Apps/webdoc.md"]
    assert r["warnings"] == [w for w in r["warnings"] if "webdoc.md" not in w]


def test_tar_gz_and_bad_archives(client):
    d = personal(client)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        data = b"# Hello\n"
        info = tarfile.TarInfo("docs/hello.md")
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
    r = do_import(client, d, buf.getvalue()).json()
    assert [x["title"] for x in r["created"]] == ["Hello"]
    assert do_import(client, d, b"not an archive").status_code == 422
    assert do_import(client, d, b"").status_code == 422


def test_import_under_a_page_and_subtree_export(client):
    d = personal(client)
    top = new_doc(client, d["id"], "Infra")
    k8s = new_doc(client, d["id"], "k8s", parent_id=top["id"])
    new_doc(client, d["id"], "Argo", parent_id=k8s["id"])
    new_doc(client, d["id"], "Elsewhere")
    files = export(client, d, k8s)
    assert sorted(files) == ["notes/k8s.md", "notes/k8s/Argo.md"]
    other = new_doc(client, d["id"], "Archive")
    copy = client.post("/api/domains", json={"name": "Copy"}, headers=H).json()
    target = new_doc(client, copy["id"], "Inbox")
    do_import(client, copy, make_zip(files), under=target["id"])
    assert tree_of(client, copy) == [("Inbox", None, 0), ("k8s", "Inbox", 0), ("Argo", "k8s", 0)]
    # A page from another domain can't be the target.
    assert do_import(client, copy, make_zip(files), under=other["id"]).status_code == 422


def test_sync_manifest_conflicts_and_trash(client):
    d = personal(client)
    a = new_doc(client, d["id"], "A")
    b = new_doc(client, d["id"], "B")
    c = new_doc(client, d["id"], "C")
    # The sync tool sends paths relative to its folder, without the export's wrapper.
    files = {k.removeprefix("notes/"): v for k, v in export(client, d).items()}
    # Someone edits B on the server after the "pull".
    client.put(f"/api/documents/{b['id']}", json={"base_revision": 1, "body": "server\n"}, headers=H)
    files["A.md"] = files["A.md"].replace(b"Ship.", b"local A")
    files["B.md"] = files["B.md"].replace(b"Ship.", b"local B")
    files["New.md"] = b"# New\n"
    del files["C.md"]
    files[".webdoc/push.json"] = json.dumps({"changed": ["A.md", "B.md", "New.md"],
                                             "check_revisions": True, "trash": [c["id"]]})
    r = do_import(client, d, make_zip(files)).json()
    assert [x["title"] for x in r["updated"]] == ["A"]
    assert [(x["title"], x["reason"]) for x in r["conflicts"]] == [
        ("B", "changed on the server since revision 1 (now 2)")]
    assert [x["title"] for x in r["created"]] == ["New"]
    assert [x["title"] for x in r["trashed"]] == ["C"]
    assert client.get(f"/api/documents/{b['id']}").json()["body"] == "server\n"
    assert "local A" in client.get(f"/api/documents/{a['id']}").json()["body"]
    m = client.get(f"/api/domains/{d['id']}/manifest").json()
    assert sorted((p["title"], p["revision"]) for p in m["pages"]) == [
        ("A", 2), ("B", 2), ("New", 1)]


def test_tokens_work_on_sync_routes_only(app_and_provider, client):
    d = personal(client)
    new_doc(client, d["id"], "A")
    tok = {"Authorization": "Bearer " + make_token(client, "sync")}
    anon = TestClient(app_and_provider[0])
    assert anon.get(f"/api/domains/{d['id']}/manifest").status_code == 404
    assert anon.get(f"/api/domains/{d['id']}/manifest", headers=tok).json()["pages"][0]["title"] == "A"
    assert len(client_zip(anon.get(f"/api/domains/{d['id']}/export", headers=tok).content)) == 1
    r = do_import(anon, d, make_zip({"x/B.md": "# B\n"}), headers=tok)
    assert r.status_code == 200 and r.json()["created"][0]["title"] == "B"
    rev = client.get(f"/api/documents/{r.json()['created'][0]['id']}/revisions").json()
    assert rev["revisions"][0]["client"] == "token:sync"
    # Elsewhere a token is not a session.
    r = anon.post("/api/documents", json={"domain_id": d["id"], "title": "x"}, headers={**tok, **H})
    assert r.status_code == 401
    assert anon.get(f"/api/domains/{d['id']}/manifest",
                    headers={"Authorization": "Bearer wdp_nope"}).status_code == 401
    # A cookie import still needs the CSRF header.
    assert do_import(client, d, make_zip({"C.md": "# C\n"}), headers={}).status_code == 403
