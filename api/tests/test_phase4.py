"""Edit leases, live update events and share links: design §7, §14 phase 4."""

import asyncio
from datetime import timedelta

from fastapi.testclient import TestClient

from webdoc.models import DocumentLease, utcnow

from .conftest import H, Mcp, sign_in


def personal(c) -> dict:
    return c.get("/api/home").json()["personal"][0]


def doc(c, domain_id, title="Plan", body="## A\n\none\n") -> dict:
    r = c.post("/api/documents", json={"domain_id": domain_id, "title": title, "body": body},
               headers=H)
    assert r.status_code == 201, r.text
    return r.json()


def tab(name: str) -> dict:
    return {**H, "X-Webdoc-Tab": name}


# --- leases ---------------------------------------------------------------------

def test_editor_lease_blocks_other_tabs_and_agents(mcp: tuple[Mcp, TestClient]):
    agent, web = mcp
    x = doc(web, personal(web)["id"])
    r = web.post(f"/api/documents/{x['id']}/lease", headers=tab("t1"))
    assert r.status_code == 200 and r.json()["lease"]["kind"] == "ui"
    assert web.get(f"/api/documents/{x['id']}").json()["lease"]["label"] == "alice"

    # The holding tab saves; another tab and an agent can't.
    ok = web.put(f"/api/documents/{x['id']}", json={"base_revision": 1, "title": "Plan 2"},
                 headers=tab("t1"))
    assert ok.status_code == 200
    r = web.put(f"/api/documents/{x['id']}", json={"base_revision": 2, "title": "Plan 3"},
                headers=tab("t2"))
    assert r.status_code == 423 and "alice is editing" in r.json()["detail"]["message"]
    err = agent.error("edit_document", document_id=x["id"],
                      ops=[{"op": "append", "content": "more"}])
    assert "alice is editing" in err and "don't retry" in err
    assert "alice is editing" in agent.call("get_document", document_id=x["id"])["being_edited"]
    assert web.delete(f"/api/documents/{x['id']}", headers=tab("t2")).status_code == 423

    # Taking over needs force; then the first tab is the one locked out.
    assert web.post(f"/api/documents/{x['id']}/lease", headers=tab("t2")).status_code == 423
    assert web.post(f"/api/documents/{x['id']}/lease?force=true",
                    headers=tab("t2")).status_code == 200
    assert web.put(f"/api/documents/{x['id']}", json={"base_revision": 2, "title": "x"},
                   headers=tab("t1")).status_code == 423

    # Releasing frees it for everyone.
    assert web.delete(f"/api/documents/{x['id']}/lease", headers=tab("t1")).json() == \
        {"released": False}
    assert web.delete(f"/api/documents/{x['id']}/lease", headers=tab("t2")).json() == \
        {"released": True}
    agent.call("edit_document", document_id=x["id"], ops=[{"op": "append", "content": "more"}])


def test_agent_lock_and_expiry(mcp: tuple[Mcp, TestClient], app_and_provider):
    agent, web = mcp
    app, provider = app_and_provider
    x = doc(web, personal(web)["id"])
    lock = agent.call("lock_document", document_id=x["id"], minutes=3)["lock"]
    assert lock["kind"] == "agent" and lock["label"] == "alice via token:laptop"
    assert 170 < lock["seconds_left"] <= 180
    # Its own writes go through; the browser's don't, until it takes over.
    agent.call("edit_document", document_id=x["id"], ops=[{"op": "append", "content": "a"}])
    r = web.put(f"/api/documents/{x['id']}", json={"base_revision": 2, "title": "y"},
                headers=tab("t1"))
    assert r.status_code == 423 and r.json()["detail"]["lease"]["kind"] == "agent"
    assert agent.call("unlock_document", document_id=x["id"]) == {"released": True}
    assert web.put(f"/api/documents/{x['id']}", json={"base_revision": 2, "title": "y"},
                   headers=tab("t1")).status_code == 200

    # A lapsed lease doesn't block anyone.
    assert web.post(f"/api/documents/{x['id']}/lease", headers=tab("t1")).status_code == 200
    with app.state.db.session() as s:
        s.get(DocumentLease, x["id"]).expires_at = utcnow() - timedelta(seconds=1)
    agent.call("edit_document", document_id=x["id"], ops=[{"op": "append", "content": "b"}])
    assert web.get(f"/api/documents/{x['id']}").json()["lease"] is None

    # Readers can't take leases; only people who may edit.
    bob = sign_in(app, provider, "bob")
    web.put(f"/api/domains/{personal(web)['id']}/grants", json={"username": "bob", "role": "reader"},
            headers=H)
    assert bob.post(f"/api/documents/{x['id']}/lease", headers=tab("b")).status_code == 403


# --- live updates ---------------------------------------------------------------

def test_events_publish_after_commit_only(app_and_provider):
    app, provider = app_and_provider
    web = sign_in(app, provider, "alice")
    x = doc(web, personal(web)["id"])
    broker = app.state.broker

    async def run():
        q = broker.subscribe(x["id"])
        # The TestClient runs the app in its own thread; events cross over
        # with call_soon_threadsafe, so let the loop turn before reading.
        await asyncio.to_thread(lambda: web.put(
            f"/api/documents/{x['id']}", json={"base_revision": 1, "body": "new"}, headers=H))
        await asyncio.to_thread(lambda: web.put(           # conflict: rolled back, no event
            f"/api/documents/{x['id']}", json={"base_revision": 1, "body": "nope"}, headers=H))
        await asyncio.to_thread(lambda: web.post(f"/api/documents/{x['id']}/lease",
                                                 headers=tab("t1")))
        await asyncio.to_thread(lambda: web.delete(f"/api/documents/{x['id']}/lease",
                                                   headers=tab("t1")))
        await asyncio.to_thread(lambda: web.delete(f"/api/documents/{x['id']}", headers=H))
        got = []
        while True:
            try:
                got.append(await asyncio.wait_for(q.get(), 0.5))
            except asyncio.TimeoutError:
                break
        broker.unsubscribe(x["id"], q)
        return got

    got = asyncio.run(run())
    assert [e["type"] for e in got] == ["revision", "lease", "lease", "trashed"]
    assert got[0]["revision"] == 2 and got[0]["by"] == "alice" and got[0]["client"] == "web"
    assert got[1]["lease"]["label"] == "alice" and got[2]["lease"] is None
    assert broker.subscriber_count(x["id"]) == 0


def test_event_stream_needs_read_access(app_and_provider):
    app, provider = app_and_provider
    alice = sign_in(app, provider, "alice")
    x = doc(alice, personal(alice)["id"])
    bob = sign_in(app, provider, "bob")
    assert bob.get(f"/api/documents/{x['id']}/events").status_code == 404
    assert TestClient(app).get(f"/api/documents/{x['id']}/events").status_code == 404


# --- share links ----------------------------------------------------------------

def test_share_links(app_and_provider):
    app, provider = app_and_provider
    alice = sign_in(app, provider, "alice")
    d = personal(alice)
    x = doc(alice, d["id"], "Trip plan", "## Day 1\n\nHike.\n")
    assert alice.get(f"/api/documents/{x['id']}").json()["can_share"] is True

    link = alice.post(f"/api/documents/{x['id']}/shares", headers=H).json()
    token = link["url"].rsplit("/s/", 1)[1]
    anon = TestClient(app)
    r = anon.get(f"/api/s/{token}")
    assert r.status_code == 200 and r.json()["title"] == "Trip plan"
    assert "Hike." in r.json()["body"] and "id" not in r.json()
    assert r.headers["x-robots-tag"] == "noindex, nofollow"
    # The link shows this document only: not its domain, history or anything else.
    assert anon.get(f"/api/documents/{x['id']}").status_code == 404
    assert anon.get(f"/api/documents/{x['id']}/revisions").status_code == 404
    shown = alice.get(f"/api/documents/{x['id']}/shares").json()["shares"]
    assert shown[0]["views"] == 1 and shown[0]["by"] == "alice"
    acts = [e["summary"] for e in alice.get("/api/activity").json()["events"]]
    assert "made a read-only link to Trip plan" in acts

    # An editor on a shared domain can't hand out links; the owner can.
    bob = sign_in(app, provider, "bob")
    alice.put(f"/api/domains/{d['id']}/grants", json={"username": "bob", "role": "editor"},
              headers=H)
    assert bob.get(f"/api/documents/{x['id']}").json()["can_share"] is False
    assert bob.post(f"/api/documents/{x['id']}/shares", headers=H).status_code == 403
    assert bob.delete(f"/api/shares/{link['id']}", headers=H).status_code == 403

    # Trashed: hidden; restored: back; revoked: gone.
    alice.delete(f"/api/documents/{x['id']}", headers=H)
    assert anon.get(f"/api/s/{token}").status_code == 404
    alice.post(f"/api/documents/{x['id']}/restore", headers=H)
    assert anon.get(f"/api/s/{token}").status_code == 200
    assert alice.delete(f"/api/shares/{link['id']}", headers=H).status_code == 204
    assert anon.get(f"/api/s/{token}").status_code == 404
    assert anon.get("/api/s/nonsense").status_code == 404


def test_public_documents_need_no_links(app_and_provider):
    app, provider = app_and_provider
    admin = sign_in(app, provider, "root", ["webdoc-admins"])
    pub = admin.post("/api/domains", json={"name": "Fixes", "scope": "public"}, headers=H).json()
    x = doc(admin, pub["id"])
    assert admin.get(f"/api/documents/{x['id']}").json()["can_share"] is False
    r = admin.post(f"/api/documents/{x['id']}/shares", headers=H)
    assert r.status_code == 422 and "open to everyone" in r.json()["detail"]
