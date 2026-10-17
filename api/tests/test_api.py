"""REST API: sign-in, home, domains and roles, documents, history, export."""

import io
import zipfile

from fastapi.testclient import TestClient

from .conftest import H, sign_in


def personal(c) -> dict:
    return c.get("/api/home").json()["personal"][0]


def new_doc(c, domain_id, title="Plan", body="# Plan\n\n## Goals\n\nShip.\n", **kw):
    r = c.post("/api/documents", json={"domain_id": domain_id, "title": title, "body": body, **kw},
               headers=H)
    assert r.status_code == 201, r.text
    return r.json()


def test_health_and_config(client):
    assert client.get("/api/health").json()["ok"]
    assert client.get("/api/config").json()["oidc"] is True


def test_new_user_gets_a_personal_domain(client):
    home = client.get("/api/home").json()
    assert home["public"] == [] and home["shared"] == []
    assert [d["slug"] for d in home["personal"]] == ["notes"]
    assert home["personal"][0]["role"] == "owner"


def test_writes_need_the_csrf_header(client):
    d = personal(client)
    r = client.post("/api/documents", json={"domain_id": d["id"], "title": "x"})
    assert r.status_code == 403


def test_admin_comes_from_the_authentik_group(app_and_provider):
    app, provider = app_and_provider
    boss = sign_in(app, provider, "boss", ["webdoc-admins"])
    assert boss.get("/api/me").json()["is_admin"] is True
    # Dropped from the group: demoted at the next sign-in.
    again = sign_in(app, provider, "boss", [])
    assert again.get("/api/me").json()["is_admin"] is False


def test_only_admins_create_public_domains(app_and_provider, client):
    app, provider = app_and_provider
    r = client.post("/api/domains", json={"name": "Fixes", "scope": "public"}, headers=H)
    assert r.status_code == 403
    admin = sign_in(app, provider, "boss", ["webdoc-admins"])
    r = admin.post("/api/domains", json={"name": "Fixes", "scope": "public"}, headers=H)
    assert r.status_code == 201 and r.json()["scope"] == "public"
    assert [d["slug"] for d in client.get("/api/home").json()["public"]] == ["fixes"]


def test_signed_out_visitors_read_public_domains_only(app_and_provider, client):
    app, provider = app_and_provider
    admin = sign_in(app, provider, "boss", ["webdoc-admins"])
    pub = admin.post("/api/domains", json={"name": "Fixes", "scope": "public"}, headers=H).json()
    doc = new_doc(admin, pub["id"], "Fix A")
    secret = new_doc(client, personal(client)["id"], "Secret")
    anon = TestClient(app)
    home = anon.get("/api/home").json()
    assert [d["slug"] for d in home["public"]] == ["fixes"] and home["personal"] == []
    r = anon.get(f"/api/documents/{doc['id']}")
    assert r.status_code == 200 and r.json()["can_edit"] is False
    assert anon.get(f"/api/documents/{doc['id']}/revisions").status_code == 200
    assert anon.get(f"/api/documents/{secret['id']}").status_code == 404
    titles = [d["title"] for d in anon.get("/api/documents").json()["documents"]]
    assert titles == ["Fix A"]
    assert anon.post("/api/documents", json={"domain_id": pub["id"], "title": "x"},
                     headers=H).status_code == 401


def test_domain_taxonomy_is_closed_to_others(app_and_provider, client):
    app, provider = app_and_provider
    bob = sign_in(app, provider, "bob")
    d = personal(client)
    assert bob.get(f"/api/domains/{d['id']}").status_code == 404
    assert bob.patch(f"/api/domains/{d['id']}", json={"name": "Mine"}, headers=H).status_code == 404


def test_domain_rename_is_recorded(client):
    d = personal(client)
    r = client.patch(f"/api/domains/{d['id']}", json={"name": "Inbox", "description": "x"},
                     headers=H)
    assert r.status_code == 200 and r.json()["name"] == "Inbox"
    revs = client.get(f"/api/domains/{d['id']}/revisions").json()["revisions"]
    assert [(r["revision"], r["name"], r["by"]) for r in revs] == [(2, "Inbox", "alice"),
                                                                   (1, "Notes", "alice")]


def test_duplicate_domain_slug_refused(client):
    r = client.post("/api/domains", json={"name": "Notes"}, headers=H)
    assert r.status_code == 422 and "already" in r.json()["detail"]


def test_only_an_empty_domain_can_be_deleted(client):
    d = client.post("/api/domains", json={"name": "Scratch"}, headers=H).json()
    doc = new_doc(client, d["id"])
    assert client.delete(f"/api/domains/{d['id']}", headers=H).status_code == 422
    client.delete(f"/api/documents/{doc['id']}", headers=H)
    assert client.delete(f"/api/domains/{d['id']}", headers=H).status_code == 422  # trash counts
    assert client.delete(f"/api/documents/{doc['id']}/purge", headers=H).status_code == 204
    assert client.delete(f"/api/domains/{d['id']}", headers=H).status_code == 204


def test_grants_reader_editor_and_who_may_give_them(app_and_provider, client):
    app, provider = app_and_provider
    bob = sign_in(app, provider, "bob")
    carol = sign_in(app, provider, "carol")
    d = personal(client)
    doc = new_doc(client, d["id"])
    r = client.put(f"/api/domains/{d['id']}/grants", json={"username": "bob", "role": "reader"},
                   headers=H)
    assert r.status_code == 200
    assert [x["slug"] for x in bob.get("/api/home").json()["shared"]] == ["notes"]
    assert bob.get(f"/api/documents/{doc['id']}").json()["can_edit"] is False
    assert bob.put(f"/api/documents/{doc['id']}", json={"base_revision": 1, "title": "x"},
                   headers=H).status_code == 403
    client.put(f"/api/domains/{d['id']}/grants", json={"username": "bob", "role": "editor"},
               headers=H)
    assert bob.put(f"/api/documents/{doc['id']}", json={"base_revision": 1, "title": "x"},
                   headers=H).status_code == 200
    # Editors can't hand out roles, and moderators exist only on public domains.
    assert bob.put(f"/api/domains/{d['id']}/grants", json={"username": "carol", "role": "reader"},
                   headers=H).status_code == 403
    assert client.put(f"/api/domains/{d['id']}/grants",
                      json={"username": "carol", "role": "moderator"}, headers=H).status_code == 403
    # Anyone can leave.
    assert bob.delete(f"/api/domains/{d['id']}/grants/{bob.get('/api/me').json()['id']}",
                      headers=H).status_code == 204
    assert bob.get(f"/api/documents/{doc['id']}").status_code == 404
    assert carol.get(f"/api/documents/{doc['id']}").status_code == 404


def test_public_domain_moderator(app_and_provider):
    app, provider = app_and_provider
    admin = sign_in(app, provider, "boss", ["webdoc-admins"])
    mod = sign_in(app, provider, "mod")
    dave = sign_in(app, provider, "dave")
    pub = admin.post("/api/domains", json={"name": "Fixes", "scope": "public"}, headers=H).json()
    assert admin.put(f"/api/domains/{pub['id']}/grants", json={"username": "mod",
                     "role": "moderator"}, headers=H).status_code == 200
    detail = mod.get(f"/api/domains/{pub['id']}").json()
    assert detail["role"] == "moderator" and detail["assignable"] == ["reader", "editor"]
    assert mod.put(f"/api/domains/{pub['id']}/grants", json={"username": "dave",
                   "role": "editor"}, headers=H).status_code == 200
    assert mod.put(f"/api/domains/{pub['id']}/grants", json={"username": "dave",
                   "role": "moderator"}, headers=H).status_code == 403
    # Moderators don't rename or delete public domains.
    assert mod.patch(f"/api/domains/{pub['id']}", json={"name": "X"}, headers=H).status_code == 403
    assert new_doc(dave, pub["id"])["can_edit"] is True


def test_document_lifecycle_and_history(app_and_provider, client):
    d = personal(client)
    doc = new_doc(client, d["id"], tags=["Kubernetes ", "k8s"])
    assert doc["tags"] == ["kubernetes", "k8s"] and doc["revision"] == 1
    r = client.put(f"/api/documents/{doc['id']}", json={
        "base_revision": 1, "body": "# Plan\n\n## Goals\n\nShip it.\n"}, headers=H)
    assert r.json()["revision"] == 2
    # A stale save is refused, not merged.
    r = client.put(f"/api/documents/{doc['id']}", json={"base_revision": 1, "title": "x"},
                   headers=H)
    assert r.status_code == 409 and r.json()["detail"]["current"] == 2
    # Saving with nothing changed adds no revision.
    r = client.put(f"/api/documents/{doc['id']}", json={"base_revision": 2, "title": "Plan"},
                   headers=H)
    assert r.json()["revision"] == 2
    hist = client.get(f"/api/documents/{doc['id']}/revisions").json()
    assert [(x["revision"], x["by"], x["client"], x["summary"]) for x in hist["revisions"]] == [
        (2, "alice", "web", "changed body"), (1, "alice", "web", "created document")]
    diff = client.get(f"/api/documents/{doc['id']}/diff?a=1").json()["diff"]
    assert "-Ship.\n" in diff and "+Ship it.\n" in diff
    assert client.patch(f"/api/documents/{doc['id']}/revisions/1", json={"label": "first"},
                        headers=H).json()["label"] == "first"
    r = client.post(f"/api/documents/{doc['id']}/revisions/1/restore", headers=H).json()
    assert r["revision"] == 3 and "Ship.\n" in r["body"]
    named = client.get(f"/api/documents/{doc['id']}/revisions?named=true").json()["revisions"]
    assert [x["revision"] for x in named] == [1]
    third = client.get(f"/api/documents/{doc['id']}/revisions").json()["revisions"][0]
    assert third["source"] == "restore" and third["restored_from"] == 1


def test_trash_restore_and_purge(app_and_provider, client):
    app, provider = app_and_provider
    d = personal(client)
    doc = new_doc(client, d["id"])
    assert client.delete(f"/api/documents/{doc['id']}", headers=H).status_code == 204
    assert client.get("/api/documents").json()["total"] == 0
    trash = client.get(f"/api/documents?domain={d['id']}&trashed=true").json()["documents"]
    assert [x["id"] for x in trash] == [doc["id"]]
    # History survives the trash.
    assert client.get(f"/api/documents/{doc['id']}/revisions").json()["current"] == 1
    assert client.post(f"/api/documents/{doc['id']}/restore", headers=H).status_code == 200
    assert client.get("/api/documents").json()["total"] == 1
    # Purge needs the trash first, and an editor can't purge.
    assert client.delete(f"/api/documents/{doc['id']}/purge", headers=H).status_code == 422
    bob = sign_in(app, provider, "bob")
    client.put(f"/api/domains/{d['id']}/grants", json={"username": "bob", "role": "editor"},
               headers=H)
    assert bob.delete(f"/api/documents/{doc['id']}", headers=H).status_code == 204
    assert bob.delete(f"/api/documents/{doc['id']}/purge", headers=H).status_code == 403
    assert client.delete(f"/api/documents/{doc['id']}/purge", headers=H).status_code == 204
    assert client.get(f"/api/documents/{doc['id']}").status_code == 404


def test_moving_between_domains_takes_child_pages(client):
    d = personal(client)
    other = client.post("/api/domains", json={"name": "Work"}, headers=H).json()
    top = new_doc(client, d["id"], "Photo Dump")
    doc = new_doc(client, d["id"], parent_id=top["id"])
    assert doc["parent_id"] == top["id"] and doc["path"] == [{"id": top["id"], "title": "Photo Dump"}]
    lst = client.get(f"/api/documents?domain={d['id']}&under={top['id']}").json()
    assert lst["total"] == 1
    r = client.put(f"/api/documents/{top['id']}", json={"base_revision": 1,
                   "domain_id": other["id"]}, headers=H).json()
    assert r["domain"] == "Work" and r["parent_id"] is None
    child = client.get(f"/api/documents/{doc['id']}").json()
    assert child["domain"] == "Work" and child["parent_id"] == top["id"]
    # A parent from another domain is refused.
    stay = new_doc(client, d["id"], "Stays")
    r = client.put(f"/api/documents/{stay['id']}", json={"base_revision": 1,
                   "parent_id": top["id"]}, headers=H)
    assert r.status_code == 422


def test_filters_and_tags(client):
    d = personal(client)
    new_doc(client, d["id"], "Alpha", tags=["infra"])
    new_doc(client, d["id"], "Beta", kind="note", tags=["infra", "dns"])
    assert client.get("/api/documents?kind=note").json()["total"] == 1
    assert client.get("/api/documents?tag=INFRA").json()["total"] == 2
    assert client.get("/api/documents?q=alp").json()["documents"][0]["title"] == "Alpha"
    assert client.get("/api/tags").json()["tags"][0] == {"tag": "infra", "count": 2}


def test_document_size_limit(client):
    d = personal(client)
    r = client.post("/api/documents", json={"domain_id": d["id"], "title": "big",
                                            "body": "x" * 30_000}, headers=H)
    assert r.status_code == 422


def test_export(client):
    d = personal(client)
    p = new_doc(client, d["id"], "Infra", body="Infra notes.\n")
    doc = new_doc(client, d["id"], "Plan: v2", tags=["a"], parent_id=p["id"])
    md = client.get(f"/api/documents/{doc['id']}/export").text
    assert md.startswith("---\ntitle: 'Plan: v2'\nkind: document\ndomain: notes\nparent: Infra\n"
                         "position: 0\n")
    assert md.endswith("---\n\n# Plan\n\n## Goals\n\nShip.\n")
    z = zipfile.ZipFile(io.BytesIO(client.get(f"/api/domains/{d['id']}/export").content))
    assert z.namelist() == ["notes/Infra.md", "notes/Infra/Plan v2.md"]


def test_writing_guide(client):
    assert client.get("/api/writing-guide").json()["is_default"] is True
    r = client.put("/api/writing-guide", json={"text": "Be brief."}, headers=H).json()
    assert r == {"text": "Be brief.", "is_default": False}


def test_admins_cannot_open_other_peoples_personal_domains(app_and_provider, client):
    app, provider = app_and_provider
    admin = sign_in(app, provider, "boss", ["webdoc-admins"])
    d = personal(client)
    doc = new_doc(client, d["id"], "Private")
    assert admin.get(f"/api/domains/{d['id']}").status_code == 404
    assert admin.get(f"/api/documents/{doc['id']}").status_code == 404
    assert admin.get(f"/api/documents?domain={d['id']}").status_code == 404
