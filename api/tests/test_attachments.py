"""Attachments: upload, serving, permissions, share links, purge and export."""

import io
import zipfile

from fastapi.testclient import TestClient

from .conftest import H, sign_in
from .test_api import new_doc, personal

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32


def upload(c, doc, data=PNG, name="shot one.png", ctype="image/png"):
    return c.post(f"/api/documents/{doc['id']}/attachments", content=data,
                  headers={**H, "X-Filename": name.replace(" ", "%20"), "Content-Type": ctype})


def test_upload_and_serve(client, store):
    d = personal(client)
    doc = new_doc(client, d["id"])
    a = upload(client, doc).json()
    assert a["image"] and a["name"] == "shot one.png"
    assert a["url"] == f"/api/attachments/{a['id']}/shot%20one.png"
    assert a["markdown"] == f"![shot one.png]({a['url']})"
    r = client.get(a["url"])
    assert r.status_code == 200 and r.content == PNG
    assert r.headers["content-type"] == "image/png"
    assert r.headers["content-disposition"].startswith("inline")
    assert "sandbox" in r.headers["content-security-policy"]
    # Anything that isn't a sniffed raster image downloads, whatever it claims to be.
    svg = upload(client, doc, b"<svg onload=alert(1)>", "x.svg", "image/svg+xml").json()
    r = client.get(svg["url"])
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["content-disposition"].startswith("attachment")
    full = client.get(f"/api/documents/{doc['id']}").json()
    assert [x["name"] for x in full["attachments"]] == ["x.svg", "shot one.png"]
    assert client.delete(f"/api/attachments/{svg['id']}", headers=H).status_code == 204
    assert client.get(svg["url"]).status_code == 404
    assert len(store.objects) == 1


def test_limits_and_permissions(app_and_provider, client, settings):
    d = personal(client)
    doc = new_doc(client, d["id"])
    assert upload(client, doc, b"").status_code == 422
    big = b"x" * (settings.max_attachment_bytes + 1)
    assert upload(client, doc, big).status_code == 413
    a = upload(client, doc).json()
    assert client.post(f"/api/documents/{doc['id']}/attachments", content=PNG).status_code == 403
    app, provider = app_and_provider
    bob = sign_in(app, provider, "bob")
    assert bob.get(a["url"]).status_code == 404
    assert upload(bob, doc).status_code == 404
    client.put(f"/api/domains/{d['id']}/grants", json={"username": "bob", "role": "reader"},
               headers=H)
    assert bob.get(a["url"]).status_code == 200
    assert upload(bob, doc).status_code == 403
    assert bob.delete(f"/api/attachments/{a['id']}", headers=H).status_code == 403
    assert TestClient(app).get(a["url"]).status_code == 404


def test_share_link_serves_the_documents_files_only(app_and_provider, client):
    d = personal(client)
    doc = new_doc(client, d["id"])
    other = new_doc(client, d["id"], "Other")
    a = upload(client, doc).json()
    b = upload(client, other).json()
    link = client.post(f"/api/documents/{doc['id']}/shares", headers=H).json()
    token = link["url"].rstrip("/").split("/")[-1]
    anon = TestClient(app_and_provider[0])
    assert anon.get(f"/api/s/{token}/attachments/{a['id']}/x.png").content == PNG
    assert anon.get(f"/api/s/{token}/attachments/{b['id']}").status_code == 404
    views = client.get(f"/api/documents/{doc['id']}/shares").json()["shares"][0]["views"]
    assert views == 0


def test_purge_drops_files_and_export_includes_them(client, store):
    d = personal(client)
    top = new_doc(client, d["id"], "Top")
    doc = new_doc(client, d["id"], "Shots", parent_id=top["id"])
    a = upload(client, doc).json()
    client.put(f"/api/documents/{doc['id']}", json={
        "base_revision": 1, "body": f"See {a['markdown']} here.\n"}, headers=H)
    z = zipfile.ZipFile(io.BytesIO(client.get(f"/api/domains/{d['id']}/export").content))
    path = f"notes/_attachments/{a['id']}/shot one.png"
    assert path in z.namelist() and z.read(path) == PNG
    assert z.read("notes/Top/Shots.md").decode().endswith(
        f"See ![shot one.png](../_attachments/{a['id']}/shot%20one.png) here.\n")
    client.delete(f"/api/documents/{top['id']}", headers=H)
    client.delete(f"/api/documents/{top['id']}/purge", headers=H)
    assert store.objects == {}
