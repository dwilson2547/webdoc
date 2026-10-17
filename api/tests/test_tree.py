"""The page tree (tree.py) and the projects → pages migration."""

import io
import zipfile

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from importlib.resources import files

from .conftest import H, sign_in
from .test_api import new_doc, personal


def tree_of(c, d) -> list[tuple]:
    pages = c.get(f"/api/domains/{d['id']}/tree").json()["pages"]
    titles = {p["id"]: p["title"] for p in pages}
    return [(p["title"], titles.get(p["parent_id"]), p["position"]) for p in pages]


def move(c, doc, parent=None, position=None):
    return c.post(f"/api/documents/{doc['id']}/move",
                  json={"parent_id": parent["id"] if parent else None, "position": position},
                  headers=H)


def test_children_order_and_reordering(client):
    d = personal(client)
    top = new_doc(client, d["id"], "Infra")
    a = new_doc(client, d["id"], "A", parent_id=top["id"])
    b = new_doc(client, d["id"], "B", parent_id=top["id"])
    c = new_doc(client, d["id"], "C", parent_id=top["id"])
    assert [x["title"] for x in client.get(f"/api/documents/{top['id']}").json()["children"]] \
        == ["A", "B", "C"]
    # Reordering among siblings is not a revision.
    r = move(client, c, top, 0).json()
    assert r["revision"] == 1
    assert [x["title"] for x in client.get(f"/api/documents/{top['id']}").json()["children"]] \
        == ["C", "A", "B"]
    # Changing the parent is one, and lands last unless told otherwise.
    r = move(client, a).json()
    assert r["parent_id"] is None and r["revision"] == 2
    assert tree_of(client, d) == [("Infra", None, 0), ("C", "Infra", 0), ("B", "Infra", 1),
                                  ("A", None, 1)]
    revs = client.get(f"/api/documents/{a['id']}/revisions").json()["revisions"]
    assert revs[0]["summary"] == "changed moved to the top"
    assert move(client, b, b).status_code == 422


def test_no_cycles_and_no_cross_domain_parents(app_and_provider, client):
    d = personal(client)
    top = new_doc(client, d["id"], "Top")
    mid = new_doc(client, d["id"], "Mid", parent_id=top["id"])
    low = new_doc(client, d["id"], "Low", parent_id=mid["id"])
    r = move(client, top, low)
    assert r.status_code == 422 and "under itself" in r.json()["detail"]
    other = client.post("/api/domains", json={"name": "Work"}, headers=H).json()
    elsewhere = new_doc(client, other["id"], "Elsewhere")
    assert move(client, low, elsewhere).status_code == 422
    r = client.post("/api/documents", json={"domain_id": other["id"], "title": "x",
                                            "parent_id": top["id"]}, headers=H)
    assert r.status_code == 422
    # Someone who can't see the parent can't file under it.
    app, provider = app_and_provider
    bob = sign_in(app, provider, "bob")
    bd = personal(bob)
    r = bob.post("/api/documents", json={"domain_id": bd["id"], "title": "x",
                                         "parent_id": top["id"]}, headers=H)
    assert r.status_code == 404


def test_trash_takes_the_subtree_and_restore_brings_it_back(client):
    d = personal(client)
    top = new_doc(client, d["id"], "Top")
    mid = new_doc(client, d["id"], "Mid", parent_id=top["id"])
    low = new_doc(client, d["id"], "Low", parent_id=mid["id"])
    # Other was trashed on its own earlier; it stays trashed when Top comes back.
    other = new_doc(client, d["id"], "Other", parent_id=mid["id"])
    client.delete(f"/api/documents/{other['id']}", headers=H)
    client.delete(f"/api/documents/{top['id']}", headers=H)
    assert tree_of(client, d) == []
    trash = client.get(f"/api/documents?domain={d['id']}&trashed=true").json()
    assert trash["total"] == 4
    client.post(f"/api/documents/{top['id']}/restore", headers=H)
    assert [t for t, _, _ in tree_of(client, d)] == ["Top", "Mid", "Low"]
    # Restoring a page whose parent is still trashed puts it at the nearest live ancestor.
    client.delete(f"/api/documents/{mid['id']}", headers=H)
    client.post(f"/api/documents/{low['id']}/restore", headers=H)
    assert tree_of(client, d) == [("Top", None, 0), ("Low", "Top", 0)]
    # Purge removes the trashed subtree; a page restored out of it meanwhile stays.
    client.delete(f"/api/documents/{top['id']}", headers=H)
    client.post(f"/api/documents/{low['id']}/restore", headers=H)
    assert tree_of(client, d) == [("Low", None, 0)]
    assert client.delete(f"/api/documents/{top['id']}/purge", headers=H).status_code == 204
    left = client.get(f"/api/documents?domain={d['id']}&trashed=true").json()
    assert left["total"] == 0
    assert [t for t, _, _ in tree_of(client, d)] == ["Low"]


def test_search_under_a_page(client):
    d = personal(client)
    top = new_doc(client, d["id"], "Infra", body="Ingress notes.\n")
    new_doc(client, d["id"], "Traefik", body="Ingress controller.\n", parent_id=top["id"])
    new_doc(client, d["id"], "Garden", body="Ingress of water.\n")
    titles = lambda r: sorted(x["title"] for x in r["results"])  # noqa: E731
    r = client.get(f"/api/search?q=Ingress&mode=find&domain_id={d['id']}").json()
    assert titles(r) == ["Garden", "Infra", "Traefik"]
    r = client.get(f"/api/search?q=Ingress&mode=find&under={top['id']}").json()
    assert titles(r) == ["Infra", "Traefik"]
    assert [x["path"] for x in r["results"] if x["title"] == "Traefik"] == ["Infra/Traefik"]


def test_export_lays_out_the_tree(client):
    d = personal(client)
    a = new_doc(client, d["id"], "A")
    b = new_doc(client, d["id"], "B", parent_id=a["id"])
    new_doc(client, d["id"], "C", parent_id=b["id"])
    new_doc(client, d["id"], "C", parent_id=b["id"])
    z = zipfile.ZipFile(io.BytesIO(client.get(f"/api/domains/{d['id']}/export").content))
    assert z.namelist() == ["notes/A.md", "notes/A/B.md", "notes/A/B/C.md", "notes/A/B/C (2).md"]


def test_migration_turns_projects_into_pages(tmp_path):
    url = f"sqlite:///{tmp_path}/m.db"
    cfg = Config()
    cfg.set_main_option("script_location", str(files("webdoc") / "migrations"))
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0006")
        conn.execute(sa.text(
            "INSERT INTO users (id, username, is_admin, is_editor, created_at) "
            "VALUES ('u1', 'alice', 0, 1, '2026-10-01')"))
        conn.execute(sa.text(
            "INSERT INTO domains (id, owner_key, scope, owner_user_id, slug, name, description, "
            "revision, accepts_contributions, contribution_types, created_at, updated_at) VALUES "
            "('d1', 'user:u1', 'personal', 'u1', 'notes', 'Notes', '', 1, 0, '[]', "
            "'2026-10-01', '2026-10-01')"))
        conn.execute(sa.text(
            "INSERT INTO projects (id, domain_id, slug, name, description, created_by, created_at) "
            "VALUES ('p1', 'd1', 'photo-dump', 'Photo Dump', 'The photo app.', 'u1', '2026-10-01')"))
        for i, pid in (("x1", "'p1'"), ("x2", "NULL")):
            conn.execute(sa.text(
                "INSERT INTO documents (id, domain_id, project_id, kind, title, body, fields, "
                "revision, created_by, created_at, updated_at, status, locked, hidden_reason) "
                f"VALUES ('{i}', 'd1', {pid}, 'note', 'Doc {i}', '', '{{}}', 1, 'u1', "
                "'2026-10-01', '2026-10-01', 'published', 0, '')"))
        command.upgrade(cfg, "head")
        rows = conn.execute(sa.text(
            "SELECT id, title, body, parent_id FROM documents ORDER BY title")).all()
        page = [r for r in rows if r.title == "Photo Dump"][0]
        assert page.body == "The photo app." and page.parent_id is None
        assert {r.id: r.parent_id for r in rows if r.title.startswith("Doc")} == {
            "x1": page.id, "x2": None}
        assert "projects" not in sa.inspect(conn).get_table_names()
        command.downgrade(cfg, "0006")
        assert "projects" in sa.inspect(conn).get_table_names()
