"""Viewers by default, editors from the authentik group, and reports on
personal documents that admins review without browsing (design §4)."""

from fastapi.testclient import TestClient

from webdoc import oidc
from webdoc.models import User

from .conftest import H, Mcp, make_token, sign_in


def personal(c) -> list:
    return c.get("/api/home").json()["personal"]


# --- viewers and editors ----------------------------------------------------------

def test_new_users_are_viewers_unless_in_the_editor_group(app_and_provider):
    app, provider = app_and_provider
    viewer = sign_in(app, provider, "vic", [])
    me = viewer.get("/api/me").json()
    assert me["is_editor"] is False and personal(viewer) == []
    r = viewer.post("/api/domains", json={"name": "Mine"}, headers=H)
    assert r.status_code == 403 and "viewers" in r.json()["detail"]
    assert viewer.get("/api/templates").status_code == 200
    assert viewer.post("/api/templates", json={"type": "x", "name": "X", "skeleton": "## A\n"},
                       headers=H).status_code in (403, 422)

    editor = sign_in(app, provider, "eve")
    assert editor.get("/api/me").json()["is_editor"] is True
    assert [d["slug"] for d in personal(editor)] == ["notes"]
    admin = sign_in(app, provider, "root", ["webdoc-admins"])
    assert admin.get("/api/me").json()["is_editor"] is True


def test_the_viewer_ceiling_caps_every_role(app_and_provider):
    app, provider = app_and_provider
    alice = sign_in(app, provider, "alice")
    d = personal(alice)[0]
    doc = alice.post("/api/documents", json={"domain_id": d["id"], "title": "Plan", "body": "x"},
                     headers=H).json()
    sign_in(app, provider, "vic", [])
    assert alice.put(f"/api/domains/{d['id']}/grants", json={"username": "vic", "role": "editor"},
                     headers=H).status_code == 200
    vic = sign_in(app, provider, "vic", [])
    shared = vic.get("/api/home").json()["shared"][0]
    assert shared["role"] == "reader"
    assert vic.get(f"/api/documents/{doc['id']}").json()["can_edit"] is False
    assert vic.put(f"/api/documents/{doc['id']}", json={"base_revision": 1, "title": "y"},
                   headers=H).status_code == 403

    # Promoted at the next sign-in: the grant applies, and they get a domain of their own.
    vic = sign_in(app, provider, "vic", ["webdoc-editors"])
    assert vic.get("/api/home").json()["shared"][0]["role"] == "editor"
    assert [x["slug"] for x in personal(vic)] == ["notes"]
    # Demoted again: their own domain stays, read-only.
    vic = sign_in(app, provider, "vic", [])
    mine = personal(vic)[0]
    assert mine["role"] == "reader"
    assert vic.post("/api/documents", json={"domain_id": mine["id"], "title": "t", "body": ""},
                    headers=H).status_code == 403


def test_viewers_read_and_report_in_forums_only(app_and_provider):
    app, provider = app_and_provider
    admin = sign_in(app, provider, "root", ["webdoc-admins"])
    d = admin.post("/api/domains", json={"name": "Fixes", "scope": "public"}, headers=H).json()
    admin.patch(f"/api/domains/{d['id']}/contributions", json={"accepts": True}, headers=H)
    doc = admin.post("/api/documents", json={"domain_id": d["id"], "kind": "contribution",
                                             "title": "Fix", "body": "Do this."}, headers=H).json()
    admin.post(f"/api/documents/{doc['id']}/publish", json={}, headers=H)

    vic = sign_in(app, provider, "vic", [])
    assert vic.get(f"/api/domains/{d['id']}").json()["can_contribute"] is False
    r = vic.post("/api/documents", json={"domain_id": d["id"], "kind": "contribution",
                                         "title": "x", "body": "y"}, headers=H)
    assert r.status_code == 422 and "viewers" in r.json()["detail"]
    got = vic.get(f"/api/documents/{doc['id']}").json()
    assert got["contribution"]["can"]["reply"] is False and got["contribution"]["can"]["report"]
    assert vic.post(f"/api/documents/{doc['id']}/replies", json={"kind": "comment", "body": "hi"},
                    headers=H).status_code == 422
    assert vic.post(f"/api/documents/{doc['id']}/confirm", json={}, headers=H).status_code == 422
    assert vic.post("/api/reports", json={"document_id": doc["id"], "reason": "spam"},
                    headers=H).status_code == 201

    # Their agents are read-only too.
    with TestClient(app) as http:
        agent = Mcp(http, make_token(vic, "laptop"))
        assert agent.call("search", query="fix")["results"]
        assert "viewers" in agent.error("draft_contribution", domain="fixes", title="t")
        assert "viewers" in agent.error("propose_domain", name="Mine", description="",
                                        rationale="want one")


def test_without_an_editor_group_everyone_is_an_editor(app_and_provider):
    app, _ = app_and_provider
    with app.state.db.session() as s:
        u = oidc.resolve_user(s, {"iss": "i", "sub": "x", "preferred_username": "x", "groups": []},
                              "webdoc-admins", None)
        assert u.is_editor and not u.is_admin


# --- reports on personal documents ------------------------------------------------

def test_admins_review_reported_personal_documents_and_nothing_else(app_and_provider):
    app, provider = app_and_provider
    admin = sign_in(app, provider, "root", ["webdoc-admins"])
    alice = sign_in(app, provider, "alice")
    d = personal(alice)[0]
    doc = alice.post("/api/documents", json={"domain_id": d["id"], "title": "Rant",
                                             "body": "original text"}, headers=H).json()
    other = alice.post("/api/documents", json={"domain_id": d["id"], "title": "Other",
                                               "body": "x"}, headers=H).json()
    sign_in(app, provider, "bob")
    alice.put(f"/api/domains/{d['id']}/grants", json={"username": "bob", "role": "reader"},
              headers=H)
    bob = sign_in(app, provider, "bob")

    # Owners don't report their own; a reader can.
    assert alice.get(f"/api/documents/{doc['id']}").json()["can_report"] is False
    assert bob.get(f"/api/documents/{doc['id']}").json()["can_report"] is True
    assert bob.post("/api/reports", json={"document_id": doc["id"], "reason": "abusive"},
                    headers=H).status_code == 201
    alice.put(f"/api/documents/{doc['id']}", json={"base_revision": 1, "body": "edited after"},
              headers=H)

    # The admin sees the report, but still can't browse the domain.
    rep = admin.get("/api/reports").json()["reports"][0]
    assert rep["scope"] == "personal" and rep["reason"] == "abusive"
    assert admin.get("/api/counts").json() == {"drafts": 0, "proposals": 0, "reports": 1,
                                               "moderates": True}
    assert admin.get(f"/api/documents/{doc['id']}").status_code == 404
    assert admin.get(f"/api/documents/{other['id']}").status_code == 404
    assert admin.get(f"/api/documents?domain={d['id']}").status_code == 404

    # Review shows the document, and what was reported if it changed since.
    r = admin.get(f"/api/reports/{rep['id']}/review").json()
    assert r["body"] == "edited after" and r["owner"] == "alice"
    assert r["reported"] == {"revision": 1, "title": "Rant", "body": "original text"}
    admin.get(f"/api/reports/{rep['id']}/review")
    seen = [e["summary"] for e in alice.get("/api/activity").json()["events"]]
    assert seen.count("an admin (root) opened Rant to review a report") == 1

    # Others can't review: not the reporter, not the owner, not a public-domain moderator.
    for c in (bob, alice):
        assert c.get(f"/api/reports/{rep['id']}/review").status_code == 404

    # Hiding takes it away from the people it's shared with; the owner sees why.
    admin.post(f"/api/reports/{rep['id']}/resolve", json={"action": "hide", "note": "abuse"},
               headers=H)
    assert bob.get(f"/api/documents/{doc['id']}").status_code == 404
    mine = alice.get(f"/api/documents/{doc['id']}").json()
    assert mine["status"] == "hidden" and mine["hidden_reason"] == "abuse"
    assert admin.get(f"/api/reports/{rep['id']}/review").status_code == 422

    # Undo puts it back.
    recent = admin.get("/api/reports").json()["recent"]
    assert recent[0]["status"] == "actioned"
    assert admin.post(f"/api/reports/{rep['id']}/undo", headers=H).json()["status"] == "dismissed"
    assert bob.get(f"/api/documents/{doc['id']}").status_code == 200


def test_public_domain_moderators_dont_get_personal_reports(app_and_provider):
    app, provider = app_and_provider
    admin = sign_in(app, provider, "root", ["webdoc-admins"])
    pub = admin.post("/api/domains", json={"name": "Fixes", "scope": "public"}, headers=H).json()
    sign_in(app, provider, "mod")
    admin.put(f"/api/domains/{pub['id']}/grants", json={"username": "mod", "role": "moderator"},
              headers=H)
    mod = sign_in(app, provider, "mod")
    alice = sign_in(app, provider, "alice")
    d = personal(alice)[0]
    doc = alice.post("/api/documents", json={"domain_id": d["id"], "title": "x", "body": "y"},
                     headers=H).json()
    sign_in(app, provider, "bob")
    alice.put(f"/api/domains/{d['id']}/grants", json={"username": "bob", "role": "reader"},
              headers=H)
    bob = sign_in(app, provider, "bob")
    bob.post("/api/reports", json={"document_id": doc["id"], "reason": "r"}, headers=H)
    assert mod.get("/api/reports").json()["reports"] == []
    assert len(admin.get("/api/reports").json()["reports"]) == 1
