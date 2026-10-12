"""Public contributions: drafts and the human publish gate, the secret scan,
replies, confirmations, moderation, reports and rate limits (design §12)."""

import pytest
from fastapi.testclient import TestClient

from webdoc import contributions, scan

from .conftest import H, Mcp, make_token, sign_in

ISSUE = """## Observed symptom

Traefik returns 404 for every route after cert renewal.

## Root cause

The ingress controller cached the old TLS secret.

## Troubleshooting steps taken

Checked certificates and ingress logs.

## Fix

Restart the ingress controller pods.
"""
FIELDS = {"severity": "High: every route down", "component": "ingress"}


@pytest.fixture
def forum(app_and_provider):
    """A public domain open to contributions, an admin, and two members."""
    app, provider = app_and_provider
    admin = sign_in(app, provider, "root", ["webdoc-admins"])
    d = admin.post("/api/domains", json={"name": "Fixes", "scope": "public"}, headers=H).json()
    r = admin.patch(f"/api/domains/{d['id']}/contributions", json={"accepts": True, "types": []},
                    headers=H)
    assert r.status_code == 200 and r.json()["accepts_contributions"]
    bob = sign_in(app, provider, "bob")
    alice = sign_in(app, provider, "alice")
    return app, provider, admin, d, bob, alice


def draft(c, d, title="Traefik 404 after cert renewal", body=ISSUE, **kw) -> dict:
    r = c.post("/api/documents", json={"domain_id": d["id"], "kind": "contribution", "title": title,
                                       "body": body, **kw}, headers=H)
    assert r.status_code == 201, r.text
    return r.json()


def publish(c, doc, ack=False):
    return c.post(f"/api/documents/{doc['id']}/publish", json={"ack_warnings": ack}, headers=H)


# --- scan -----------------------------------------------------------------------

def test_scan_blocks_secrets_and_flags_private_details():
    r = scan.scan("key AKIAIOSFODNN7EXAMPLE\npassword = hunter2abc\nssh 10.0.0.5 at db.lan\n"
                  "token: required\nsee /home/daniel/x and ops@example.com")
    assert {f["kind"] for f in r["secrets"]} == {"AWS access key", "password or secret"}
    assert {f["kind"] for f in r["warnings"]} == {"private IP address", "internal host name",
                                                 "home directory path"}
    assert all("hunter2abc" not in f["excerpt"] for f in r["secrets"])
    with pytest.raises(scan.Blocked) as e:
        scan.check("ip 192.168.1.4")
    assert e.value.needs_ack
    assert scan.check("ip 192.168.1.4", ack_warnings=True)["warnings"]


# --- drafts and publishing ------------------------------------------------------

def test_only_moderators_open_a_domain_to_contributions(forum):
    app, provider, admin, d, bob, alice = forum
    assert bob.patch(f"/api/domains/{d['id']}/contributions", json={"accepts": False},
                     headers=H).status_code == 403
    mine = bob.get("/api/home").json()["personal"][0]
    assert bob.patch(f"/api/domains/{mine['id']}/contributions", json={"accepts": True},
                     headers=H).status_code == 422
    assert bob.get(f"/api/domains/{d['id']}").json()["can_contribute"] is True
    # A contribution needs a domain that takes them.
    r = bob.post("/api/documents", json={"domain_id": mine["id"], "kind": "contribution",
                                         "title": "x", "body": "y"}, headers=H)
    assert r.status_code == 422


def test_accepted_types_are_enforced(forum):
    app, provider, admin, d, bob, alice = forum
    admin.patch(f"/api/domains/{d['id']}/contributions",
                json={"accepts": True, "types": ["issue-resolution"]}, headers=H)
    r = bob.post("/api/documents", json={"domain_id": d["id"], "kind": "contribution",
                                         "title": "x", "body": "y"}, headers=H)
    assert r.status_code == 422 and "issue-resolution" in r.json()["detail"]
    doc = draft(bob, d, type="issue-resolution", fields=FIELDS)
    assert doc["type"] == "issue-resolution" and doc["status"] == "draft"
    assert admin.patch(f"/api/domains/{d['id']}/contributions",
                       json={"accepts": True, "types": ["nope"]}, headers=H).status_code == 422


def test_a_draft_is_its_authors_alone_until_published(forum):
    app, provider, admin, d, bob, alice = forum
    doc = draft(bob, d)
    assert doc["status"] == "draft" and doc["can_edit"] and doc["contribution"]["can"]["publish"]
    anon = TestClient(app)
    app.state.indexer.run_once()
    for c in (alice, admin, anon):
        assert c.get(f"/api/documents/{doc['id']}").status_code == 404
        assert c.get(f"/api/documents?domain={d['id']}").json()["total"] == 0
        assert c.get("/api/search", params={"q": "traefik cert renewal"}).json()["results"] == []
        assert c.get("/api/search", params={"q": "Traefik", "mode": "find"}).json()["results"] == []
        titles = [e["title"] for e in c.get("/api/activity").json()["events"]]
        assert "Traefik 404 after cert renewal" not in titles
    assert admin.get("/api/home").json()["public"][0]["documents"] == 0
    # The author sees it everywhere, and in their drafts.
    assert bob.get(f"/api/documents?domain={d['id']}").json()["total"] == 1
    assert bob.get("/api/search", params={"q": "Traefik", "mode": "find"}).json()["results"]
    assert bob.get("/api/counts").json()["drafts"] == 1
    assert [x["id"] for x in bob.get("/api/drafts").json()["contributions"]] == [doc["id"]]
    # Nobody else can publish it.
    assert publish(admin, doc).status_code == 404

    r = publish(bob, doc)
    assert r.status_code == 200 and r.json()["status"] == "published"
    assert anon.get(f"/api/documents/{doc['id']}").json()["contribution"]["author"] == "bob"
    app.state.indexer.run_once()
    assert anon.get("/api/search", params={"q": "traefik cert renewal"}).json()["results"]
    events = [e["summary"] for e in anon.get("/api/activity").json()["events"]]
    assert "published Traefik 404 after cert renewal" in events
    assert bob.get("/api/counts").json()["drafts"] == 0


def test_publishing_runs_the_secret_scan(forum):
    app, provider, admin, d, bob, alice = forum
    doc = draft(bob, d, body=ISSUE + "\nThe token: ghp_" + "a" * 36 + "\n")
    r = publish(bob, doc, ack=True)
    assert r.status_code == 422 and r.json()["detail"]["needs_ack"] is False
    assert r.json()["detail"]["scan"]["secrets"][0]["kind"] == "GitHub token"

    doc = draft(bob, d, title="Other", body=ISSUE + "\nOn 192.168.0.65.\n")
    assert bob.get(f"/api/documents/{doc['id']}/scan").json()["warnings"]
    r = publish(bob, doc)
    assert r.status_code == 422 and r.json()["detail"]["needs_ack"] is True
    assert publish(bob, doc, ack=True).status_code == 200

    # Editing it afterwards goes public at once, so it's scanned too.
    cur = bob.get(f"/api/documents/{doc['id']}").json()
    r = bob.put(f"/api/documents/{doc['id']}", json={
        "base_revision": cur["revision"], "body": ISSUE + "\npassword = s3cretValue9\n"}, headers=H)
    assert r.status_code == 422 and r.json()["detail"]["scan"]["secrets"]
    r = bob.put(f"/api/documents/{doc['id']}", json={
        "base_revision": cur["revision"], "body": ISSUE + "\nAlso check 10.1.2.3.\n",
        "ack_warnings": True}, headers=H)
    assert r.status_code == 200
    # Contributions stay where they were posted.
    mine = bob.get("/api/home").json()["personal"][0]
    r = bob.put(f"/api/documents/{doc['id']}", json={"base_revision": r.json()["revision"],
                                                     "domain_id": mine["id"]}, headers=H)
    assert r.status_code == 422


# --- agents ---------------------------------------------------------------------

def test_agents_draft_with_a_duplicate_check_and_never_publish(forum):
    app, provider, admin, d, bob, alice = forum
    first = draft(alice, d)
    publish(alice, first)
    app.state.indexer.run_once()
    with TestClient(app) as http:
        agent = Mcp(http, make_token(bob, "laptop"))
        names = {t["name"] for t in agent.rpc("tools/list").json()["result"]["tools"]}
        assert {"draft_contribution", "reply", "confirm", "list_drafts"} <= names
        assert not any("publish" in n for n in names)

        r = agent.call("draft_contribution", domain="fixes", title="Traefik gives 404 after renewal",
                       body="routes 404 after cert-manager renewed the certificate")
        assert r["created"] is False and r["possible_duplicates"][0]["id"] == first["id"]
        # Found by title words too, before the indexer has seen a new post.
        fresh = draft(alice, d, title="Longhorn volume stuck attaching")
        publish(alice, fresh)
        r = agent.call("draft_contribution", domain="fixes", title="Volume stuck attaching in Longhorn")
        assert [x["id"] for x in r["possible_duplicates"]] == [fresh["id"]]
        r = agent.call("draft_contribution", domain="fixes", title="Traefik gives 404 after renewal",
                       body="different cause", checked_duplicates=True)
        assert r["created"] and r["status"] == "draft"
        assert agent.call("list_drafts")["contributions"][0]["id"] == r["id"]
        agent.call("edit_document", document_id=r["id"], ops=[{"op": "append", "content": "more"}])

        # Published contributions are edited by a person in the web UI.
        publish(bob, {"id": r["id"]}, ack=True)
        err = agent.error("edit_document", document_id=r["id"], ops=[{"op": "append", "content": "x"}])
        assert "published" in err and "web UI" in err

        # Replies are drafts too.
        rep = agent.call("reply", document_id=first["id"], kind="solution", body="Delete the secret.")
        assert rep["status"] == "draft"
        thread = alice.get(f"/api/documents/{first['id']}").json()["contribution"]
        assert thread["replies"] == []
        doc = agent.call("get_document", document_id=first["id"])
        assert doc["contribution"]["replies"][0]["status"] == "draft"


# --- replies, confirmations, resolution ------------------------------------------

def test_replies_confirmations_and_resolution(forum):
    app, provider, admin, d, bob, alice = forum
    doc = draft(bob, d)
    publish(bob, doc)
    url = f"/api/documents/{doc['id']}"

    r = alice.post(f"{url}/replies", json={"kind": "solution", "body": "Or delete the secret."},
                   headers=H)
    assert r.status_code == 201
    reply = r.json()["contribution"]["replies"][0]
    assert reply["status"] == "published" and reply["by"] == "alice"
    assert TestClient(app).get(url).json()["contribution"]["replies"][0]["body"] == \
        "Or delete the secret."
    r = alice.post(f"{url}/replies", json={"kind": "comment",
                                           "body": "key AKIAIOSFODNN7EXAMPLE"}, headers=H)
    assert r.status_code == 422
    assert alice.post(f"{url}/replies", json={"kind": "nope", "body": "x"},
                      headers=H).status_code == 422

    # Confirmations: not your own fix, generic environment only, one each.
    assert bob.post(f"{url}/confirm", json={}, headers=H).status_code == 422
    r = alice.post(f"{url}/confirm", json={"environment": "k3s 1.30 on 10.0.0.4"}, headers=H)
    assert r.status_code == 422 and "generic" in r.json()["detail"]
    alice.post(f"{url}/confirm", json={"environment": "k3s 1.30"}, headers=H)
    r = alice.post(f"{url}/confirm", json={"environment": "k3s 1.31"}, headers=H).json()
    assert r["contribution"]["confirmations"] == [
        {"by": "alice", "environment": "k3s 1.31", "at": r["contribution"]["confirmations"][0]["at"]}]
    assert r["contribution"]["confirmed_by_me"]
    r = bob.post(f"{url}/confirm", json={"reply_id": reply["id"]}, headers=H).json()
    assert r["contribution"]["replies"][0]["confirmations"] == 1
    assert len(alice.delete(f"{url}/confirm", headers=H).json()["contribution"]["confirmations"]) == 0

    # The author marks the resolution; others can't.
    assert alice.post(f"{url}/accept", json={"reply_id": reply["id"]}, headers=H).status_code == 403
    r = bob.post(f"{url}/accept", json={"reply_id": reply["id"]}, headers=H)
    assert r.json()["contribution"]["accepted_reply_id"] == reply["id"]

    # Withdrawing a published reply keeps it but hides it, and clears the resolution.
    r = alice.delete(f"/api/replies/{reply['id']}", headers=H).json()
    assert r["contribution"]["accepted_reply_id"] is None
    assert TestClient(app).get(url).json()["contribution"]["replies"] == []


# --- moderation and reports -------------------------------------------------------

def test_moderation(forum):
    app, provider, admin, d, bob, alice = forum
    doc = draft(bob, d)
    publish(bob, doc)
    other = draft(alice, d, title="Ingress 404s after certificate rotation")
    publish(alice, other)
    url = f"/api/documents/{doc['id']}"
    anon = TestClient(app)

    assert alice.post(f"{url}/moderate", json={"action": "hide"}, headers=H).status_code == 403
    r = admin.post(f"{url}/moderate", json={"action": "hide", "reason": "spam"}, headers=H)
    assert r.status_code == 200 and r.json()["status"] == "hidden"
    assert anon.get(url).status_code == 404
    seen = bob.get(url).json()
    assert seen["status"] == "hidden" and seen["contribution"]["hidden_reason"] == "spam"
    admin.post(f"{url}/moderate", json={"action": "unhide"}, headers=H)
    assert anon.get(url).status_code == 200

    # Locked: no replies, confirmations or edits by the author; moderators still edit.
    admin.post(f"{url}/moderate", json={"action": "lock"}, headers=H)
    assert alice.post(f"{url}/replies", json={"kind": "comment", "body": "hi"},
                      headers=H).status_code == 422
    cur = bob.get(url).json()
    assert cur["can_edit"] is False
    assert bob.put(url, json={"base_revision": cur["revision"], "title": "z"},
                   headers=H).status_code == 403
    assert admin.put(url, json={"base_revision": cur["revision"], "title": "Traefik 404s"},
                     headers=H).status_code == 200
    admin.post(f"{url}/moderate", json={"action": "unlock"}, headers=H)

    # A duplicate points at the original and drops out of search.
    r = admin.post(f"/api/documents/{other['id']}/moderate",
                   json={"action": "duplicate", "of": doc["id"]}, headers=H)
    assert r.json()["contribution"]["merged_into"]["id"] == doc["id"]
    app.state.indexer.run_once()
    ids = [x["id"] for x in anon.get("/api/search", params={"q": "ingress 404 certificate"}).json()["results"]]
    assert other["id"] not in ids and doc["id"] in ids
    assert admin.post(f"{url}/moderate", json={"action": "duplicate", "of": doc["id"]},
                      headers=H).status_code == 422


def test_reports(forum):
    app, provider, admin, d, bob, alice = forum
    doc = draft(bob, d)
    publish(bob, doc)
    assert admin.get("/api/counts").json()["reports"] == 0
    r = alice.post("/api/reports", json={"document_id": doc["id"], "reason": "contains spam"},
                   headers=H)
    assert r.status_code == 201
    assert TestClient(app).post("/api/reports", json={"document_id": doc["id"], "reason": "x"},
                                headers=H).status_code == 401
    # Reporters don't see the queue; moderators do.
    assert alice.get("/api/reports").json() == {"reports": [], "moderates": False}
    reps = admin.get("/api/reports").json()["reports"]
    assert reps[0]["reason"] == "contains spam" and reps[0]["by"] == "alice"
    assert admin.get("/api/counts").json()["reports"] == 1
    assert alice.post(f"/api/reports/{reps[0]['id']}/resolve", json={"action": "hide"},
                      headers=H).status_code == 404
    r = admin.post(f"/api/reports/{reps[0]['id']}/resolve", json={"action": "hide"}, headers=H)
    assert r.json()["status"] == "actioned"
    assert TestClient(app).get(f"/api/documents/{doc['id']}").status_code == 404
    # A domain moderator handles their own domain's reports.
    carol = sign_in(app, provider, "carol")
    admin.put(f"/api/domains/{d['id']}/grants", json={"username": "carol", "role": "moderator"},
              headers=H)
    admin.post(f"/api/documents/{doc['id']}/moderate", json={"action": "unhide"}, headers=H)
    alice.post("/api/reports", json={"document_id": doc["id"], "reason": "again"}, headers=H)
    rep = carol.get("/api/reports").json()["reports"][0]
    assert carol.post(f"/api/reports/{rep['id']}/resolve", json={"action": "dismiss"},
                      headers=H).json()["status"] == "dismissed"
    # Personal documents aren't reportable yet.
    mine = bob.get("/api/home").json()["personal"][0]
    x = bob.post("/api/documents", json={"domain_id": mine["id"], "title": "p", "body": "q"},
                 headers=H).json()
    assert bob.post("/api/reports", json={"document_id": x["id"], "reason": "x"},
                    headers=H).status_code == 422


# --- rate limits ----------------------------------------------------------------

def test_rate_limits(forum, monkeypatch):
    app, provider, admin, d, bob, alice = forum
    monkeypatch.setitem(contributions.LIMITS, "draft_user", (2, contributions.LIMITS["draft_user"][1]))
    draft(bob, d, title="one")
    draft(bob, d, title="two")
    r = bob.post("/api/documents", json={"domain_id": d["id"], "kind": "contribution",
                                         "title": "three", "body": "x"}, headers=H)
    assert r.status_code == 429 and "at most 2" in r.json()["detail"]

    anon = TestClient(app)
    codes = [anon.get("/api/search", params={"q": "x", "mode": "find"}).status_code
             for _ in range(31)]
    assert codes[:30] == [200] * 30 and codes[30] == 429
    assert alice.get("/api/search", params={"q": "x", "mode": "find"}).status_code == 200
