"""Forums (design §16 phase 3): a domain that takes contributions, public or
private, with view / add / moderate / admin roles."""

import pytest
from fastapi.testclient import TestClient

from .conftest import H, Mcp, make_token, sign_in
from .test_phase5 import ISSUE, draft, publish


def grant(c, d, username, role):
    return c.put(f"/api/domains/{d['id']}/grants", json={"username": username, "role": role},
                 headers=H)


def reply(c, doc, body="Restart the pods."):
    return c.post(f"/api/documents/{doc['id']}/replies", json={"kind": "solution", "body": body},
                  headers=H)


@pytest.fixture
def private(app_and_provider):
    """bob's personal domain opened as a private forum, and four other people."""
    app, provider = app_and_provider
    admin = sign_in(app, provider, "root", ["webdoc-admins"])
    bob, alice, carol, dave = (sign_in(app, provider, n) for n in ("bob", "alice", "carol", "dave"))
    d = bob.post("/api/domains", json={"name": "Team"}, headers=H).json()
    r = bob.patch(f"/api/domains/{d['id']}/contributions", json={"accepts": True}, headers=H)
    assert r.status_code == 200 and r.json()["forum"] == "private"
    return app, admin, d, bob, alice, carol, dave


def test_a_private_forum_is_for_its_members(private):
    app, admin, d, bob, alice, carol, dave = private
    assert grant(bob, d, "alice", "contributor").status_code == 200
    assert grant(bob, d, "carol", "reader").status_code == 200
    doc = draft(alice, d)
    assert publish(alice, doc).status_code == 200
    # Outsiders, admins included, can't see it.
    for c in (dave, admin, TestClient(app)):
        assert c.get(f"/api/domains/{d['id']}").status_code == 404
        assert c.get(f"/api/documents/{doc['id']}").status_code == 404
    assert [x["slug"] for x in bob.get("/api/home").json()["forums"]] == ["team"]
    assert alice.get("/api/home").json()["forums"][0]["role"] == "contributor"
    # view reads and reports but doesn't post, reply or confirm.
    got = carol.get(f"/api/documents/{doc['id']}").json()
    assert got["can_report"] and not got["contribution"]["can"]["reply"]
    assert carol.get(f"/api/domains/{d['id']}").json()["can_contribute"] is False
    r = carol.post("/api/documents", json={"domain_id": d["id"], "kind": "contribution",
                                           "title": "x", "body": "y"}, headers=H)
    assert r.status_code == 422 and "add" in r.json()["detail"]
    assert reply(carol, doc).status_code == 422
    # add does all of that, but can't moderate or give roles.
    assert reply(bob, doc).status_code == 201
    assert alice.post(f"/api/documents/{doc['id']}/moderate", json={"action": "lock"},
                      headers=H).status_code == 403
    assert grant(alice, d, "dave", "reader").status_code == 403


def test_private_forum_reports_go_to_its_moderators_not_admins(private):
    app, admin, d, bob, alice, carol, dave = private
    grant(bob, d, "alice", "contributor")
    grant(bob, d, "carol", "reader")
    grant(bob, d, "dave", "moderator")
    doc = draft(alice, d)
    publish(alice, doc)
    r = carol.post("/api/reports", json={"document_id": doc["id"], "reason": "off topic"},
                   headers=H)
    assert r.status_code == 201
    assert admin.get("/api/reports").json()["reports"] == []
    assert len(bob.get("/api/reports").json()["reports"]) == 1
    rep = dave.get("/api/reports").json()["reports"][0]
    r = dave.post(f"/api/reports/{rep['id']}/resolve", json={"action": "hide"}, headers=H)
    assert r.status_code == 200
    # Hidden: the author and moderators still see it, other members don't.
    assert carol.get(f"/api/documents/{doc['id']}").status_code == 404
    assert alice.get(f"/api/documents/{doc['id']}").json()["status"] == "hidden"
    assert bob.get(f"/api/documents?domain={d['id']}").json()["total"] == 1


def test_forum_roles_and_who_hands_them_out(private):
    app, admin, d, bob, alice, carol, dave = private
    assert bob.get(f"/api/domains/{d['id']}/grants").json()["assignable"] == [
        "reader", "contributor", "moderator", "admin"]
    grant(bob, d, "dave", "moderator")
    assert dave.get(f"/api/domains/{d['id']}/grants").json()["assignable"] == [
        "reader", "contributor"]
    assert grant(dave, d, "carol", "contributor").status_code == 200
    assert grant(dave, d, "alice", "moderator").status_code == 403
    # Moderators don't change the forum's settings; its admins do.
    assert dave.patch(f"/api/domains/{d['id']}/contributions", json={"accepts": False},
                      headers=H).status_code == 403
    grant(bob, d, "alice", "admin")
    assert grant(alice, d, "carol", "moderator").status_code == 200
    r = alice.patch(f"/api/domains/{d['id']}/contributions", json={"accepts": False}, headers=H)
    assert r.status_code == 200 and r.json()["forum"] is None
    # No longer a forum: back to the wiki roles.
    assert bob.get(f"/api/domains/{d['id']}/grants").json()["assignable"] == ["reader", "editor"]


def test_public_forum_is_open_to_editors_unless_a_grant_says_otherwise(app_and_provider):
    app, provider = app_and_provider
    admin = sign_in(app, provider, "root", ["webdoc-admins"])
    bob, alice = sign_in(app, provider, "bob"), sign_in(app, provider, "alice")
    viewer = sign_in(app, provider, "vic", [])
    d = admin.post("/api/domains", json={"name": "Fixes", "scope": "public"}, headers=H).json()
    admin.patch(f"/api/domains/{d['id']}/contributions", json={"accepts": True}, headers=H)
    got = bob.get(f"/api/domains/{d['id']}").json()
    assert got["forum"] == "public" and got["role"] == "contributor" and got["can_contribute"]
    assert viewer.get(f"/api/domains/{d['id']}").json()["can_contribute"] is False
    assert TestClient(app).get(f"/api/domains/{d['id']}").json()["role"] == "reader"
    # view takes posting away from one person.
    assert grant(admin, d, "bob", "reader").status_code == 200
    r = bob.post("/api/documents", json={"domain_id": d["id"], "kind": "contribution",
                                         "title": "x", "body": "y"}, headers=H)
    assert r.status_code == 422
    # A forum admin runs it without being a site admin.
    grant(admin, d, "alice", "admin")
    assert grant(alice, d, "bob", "moderator").status_code == 200
    assert alice.patch(f"/api/domains/{d['id']}/contributions",
                       json={"accepts": True, "types": []}, headers=H).status_code == 200


def test_agents_find_duplicates_in_a_private_forum(private):
    app, admin, d, bob, alice, carol, dave = private
    grant(bob, d, "alice", "contributor")
    first = draft(alice, d, body=ISSUE)
    publish(alice, first)
    app.state.indexer.run_once()
    with TestClient(app) as http:
        agent = Mcp(http, make_token(bob, "laptop"))
        listed = {x["slug"]: x for x in agent.call("list_domains")["domains"]}
        assert listed["team"]["forum"] == "private"
        r = agent.call("draft_contribution", domain="team", title="Traefik gives 404 after renewal")
        assert r["created"] is False and r["possible_duplicates"][0]["id"] == first["id"]
