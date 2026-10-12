"""Document types (templates) and proposals: design §5, §6."""

from fastapi.testclient import TestClient

from webdoc import templates

from .conftest import H, Mcp, make_token, sign_in

ARCH = """## Overview

Store for notes.

## Components

API and UI.

## Data flow

Requests go to the API.

## Decisions

Postgres.
"""


def personal(c) -> dict:
    return c.get("/api/home").json()["personal"][0]


# --- skeleton parsing -------------------------------------------------------------

def test_outline_and_starter():
    sk = "## A\n<!-- write A -->\n\n### A1 (optional)\n<!-- sub -->\n\n## B (Optional)\nkeep me\n"
    assert templates.outline(sk) == [
        {"path": ["A"], "required": True, "guidance": "write A"},
        {"path": ["A", "A1"], "required": False, "guidance": "sub"},
        {"path": ["B"], "required": False, "guidance": ""}]
    assert templates.starter(sk) == "## A\n\n### A1\n\n## B\nkeep me\n"


def test_check_reports_missing_sections_and_fields():
    spec = templates.BUILTINS["architecture"]
    gaps = templates.check(spec, "## Overview\n\nx\n", {})
    assert gaps == {"missing_sections": ["Components", "Data flow", "Decisions"],
                    "missing_fields": ["status"]}
    assert templates.check(spec, ARCH, {"status": "draft"}) == {
        "missing_sections": [], "missing_fields": []}


def test_template_validation():
    for bad in [dict(type="Bad Type"), dict(skeleton="no headings"),
                dict(fields=[{"name": "title"}]), dict(fields=[{"name": "a"}, {"name": "a"}])]:
        kw = dict(type="adr", name="ADR", skeleton="## X", fields=[], default_tags=[]) | bad
        try:
            templates.validate(kw["type"], kw["name"], kw["skeleton"], kw["fields"], kw["default_tags"])
        except templates.TemplateError:
            continue
        raise AssertionError(f"accepted {bad}")


# --- REST ---------------------------------------------------------------------

def test_typed_document_from_the_ui(client):
    d = personal(client)
    types = {t["type"] for t in client.get(f"/api/domains/{d['id']}/templates").json()["templates"]}
    assert {"architecture", "decision", "issue-resolution"} <= types
    r = client.post("/api/documents", json={"domain_id": d["id"], "title": "webdoc",
                                            "type": "architecture"}, headers=H)
    assert r.status_code == 422 and "missing fields: status" in r.json()["detail"]
    r = client.post("/api/documents", json={"domain_id": d["id"], "title": "webdoc",
                                            "type": "architecture", "fields": {"status": "draft"}},
                    headers=H)
    doc = r.json()
    assert r.status_code == 201 and doc["type"] == "architecture" and doc["fields"] == {"status": "draft"}
    assert doc["body"].startswith("## Overview\n\n## Components") and "<!--" not in doc["body"]
    assert doc["tags"] == ["architecture"]
    # Deleting a required section later is allowed but shows up in the check.
    r = client.put(f"/api/documents/{doc['id']}", json={
        "base_revision": 1, "body": "## Overview\n", "fields": {"status": "accepted", "owner": "me"}},
        headers=H).json()
    assert r["template"]["check"]["missing_sections"] == ["Components", "Data flow", "Decisions"]
    assert r["fields"] == {"status": "accepted", "owner": "me"}
    r = client.put(f"/api/documents/{doc['id']}", json={"base_revision": 2,
                   "fields": {"owner": None}}, headers=H).json()
    assert r["fields"] == {"status": "accepted"}
    # Restoring the first revision brings its fields back exactly.
    r = client.post(f"/api/documents/{doc['id']}/revisions/1/restore", headers=H).json()
    assert r["fields"] == {"status": "draft"}


def test_own_template_overrides_builtin_and_is_versioned(client):
    d = personal(client)
    r = client.post("/api/templates", json={"type": "architecture", "name": "My architecture",
                    "skeleton": "## Summary\n<!-- one paragraph -->\n", "fields": []}, headers=H)
    assert r.status_code == 201, r.text
    t = r.json()
    assert t["source"] == "personal"
    resolved = {x["type"]: x for x in client.get(f"/api/domains/{d['id']}/templates").json()["templates"]}
    assert resolved["architecture"]["name"] == "My architecture"
    assert client.post("/api/templates", json={"type": "architecture", "name": "Again",
                       "skeleton": "## X"}, headers=H).status_code == 422
    client.patch(f"/api/templates/{t['id']}", json={"skeleton": "## Summary\n\n## Risks (optional)\n"},
                 headers=H)
    hist = client.get(f"/api/templates/{t['id']}/revisions").json()["revisions"]
    assert [h["version"] for h in hist] == [2, 1]
    doc = client.post("/api/documents", json={"domain_id": d["id"], "title": "x",
                      "type": "architecture"}, headers=H).json()
    assert doc["body"] == "## Summary\n\n## Risks\n"


def test_template_permissions(app_and_provider, client):
    app, provider = app_and_provider
    bob = sign_in(app, provider, "bob")
    admin = sign_in(app, provider, "boss", ["webdoc-admins"])
    mod = sign_in(app, provider, "mod")
    t = client.post("/api/templates", json={"type": "memo", "name": "Memo", "skeleton": "## M"},
                    headers=H).json()
    assert bob.patch(f"/api/templates/{t['id']}", json={"name": "x"}, headers=H).status_code == 404
    assert bob.post("/api/templates", json={"scope": "public", "type": "faq", "name": "FAQ",
                    "skeleton": "## Q"}, headers=H).status_code == 403
    pub = admin.post("/api/domains", json={"name": "Fixes", "scope": "public"}, headers=H).json()
    admin.put(f"/api/domains/{pub['id']}/grants", json={"username": "mod", "role": "moderator"},
              headers=H)
    r = mod.post("/api/templates", json={"domain_id": pub["id"], "type": "fix", "name": "Fix",
                 "skeleton": "## Problem\n\n## Solution"}, headers=H)
    assert r.status_code == 201 and r.json()["source"] == "domain"
    assert mod.post("/api/templates", json={"scope": "public", "type": "faq", "name": "FAQ",
                    "skeleton": "## Q"}, headers=H).status_code == 403
    assert [x["type"] for x in mod.get("/api/templates").json()["templates"]] == ["fix"]


# --- proposals ----------------------------------------------------------------

def test_agent_proposes_a_domain_and_the_user_approves(mcp):
    agent, web = mcp
    r = agent.call("propose_domain", name="Robotics", description="Robot builds",
                   rationale="Notes about the arm don't fit Notes")
    assert r["status"] == "pending"
    assert "rationale" in agent.error("propose_domain", name="X", description="", rationale=" ")
    assert "already exists" in agent.error("propose_domain", name="Notes", description="",
                                           rationale="dup")
    # Still closed: nothing exists until approved.
    assert "no domain 'robotics'" in agent.error("create_note", title="x", domain="robotics")
    assert web.get("/api/proposals/count").json() == {"pending": 1}
    p = web.get("/api/proposals").json()["to_decide"][0]
    assert (p["kind"], p["by"], p["client"]) == ("domain.create", "alice", "token:laptop")
    r = web.post(f"/api/proposals/{p['id']}/approve",
                 json={"edits": {"description": "Robot arm and rover builds"}}, headers=H).json()
    assert r["status"] == "approved" and r["decision_note"] == "approved with changes"
    d = next(x for x in agent.call("list_domains")["domains"] if x["slug"] == "robotics")
    assert d["description"] == "Robot arm and rover builds"
    assert agent.call("list_proposals", status="approved")["proposals"][0]["result_id"] == d["id"]


def test_domain_change_and_rejection(mcp):
    agent, web = mcp
    agent.call("propose_domain_change", domain="notes", name="Inbox", rationale="clearer")
    p = web.get("/api/proposals").json()["to_decide"][0]
    assert p["payload"]["current"]["name"] == "Notes"
    web.post(f"/api/proposals/{p['id']}/reject", json={"note": "keep it"}, headers=H)
    mine = agent.call("list_proposals")["proposals"][0]
    assert (mine["status"], mine["decision_note"]) == ("rejected", "keep it")
    assert web.post(f"/api/proposals/{p['id']}/approve", json={}, headers=H).status_code == 403


def test_template_proposal_create_then_update(mcp):
    agent, web = mcp
    agent.call("propose_template", type="runbook", name="Runbook",
               skeleton="## Steps\n<!-- numbered -->", rationale="we keep writing these",
               fields=[{"name": "service", "required": True}])
    p = web.get("/api/proposals").json()["to_decide"][0]
    assert p["kind"] == "template.create"
    web.post(f"/api/proposals/{p['id']}/approve", json={}, headers=H)
    t = agent.call("get_template", type="runbook", domain="notes")
    assert t["source"] == "personal" and t["sections"][0]["guidance"] == "numbered"
    agent.call("propose_template", type="runbook", name="Runbook", rationale="add rollback",
               skeleton="## Steps\n\n## Rollback")
    p = web.get("/api/proposals").json()["to_decide"][0]
    assert p["kind"] == "template.update" and p["payload"]["current_version"] == 1
    web.post(f"/api/proposals/{p['id']}/approve", json={}, headers=H)
    assert agent.call("get_template", type="runbook", domain="notes")["version"] == 2
    # Typed creation through MCP.
    err = agent.error("create_document", title="Deploy", domain="notes", type="runbook")
    assert "missing fields: service" in err
    doc = agent.call("create_document", title="Deploy", domain="notes", type="runbook",
                     fields={"service": "webdoc"})
    got = agent.call("get_document", document_id=doc["id"])
    assert got["type"] == "runbook" and got["template_check"] == {"missing_sections": [],
                                                                 "missing_fields": []}


def test_only_the_right_people_decide(app_and_provider):
    app, provider = app_and_provider
    alice = sign_in(app, provider, "alice")
    bob = sign_in(app, provider, "bob")
    admin = sign_in(app, provider, "boss", ["webdoc-admins"])
    with TestClient(app) as http:
        agent = Mcp(http, make_token(alice))
        agent.call("propose_domain", name="Robotics", description="", rationale="r")
        agent.call("propose_domain", name="Recipes", description="", rationale="r", scope="public")
    alice_queue = [p["payload"]["name"] for p in alice.get("/api/proposals").json()["to_decide"]]
    admin_queue = [p["payload"]["name"] for p in admin.get("/api/proposals").json()["to_decide"]]
    assert alice_queue == ["Robotics"] and admin_queue == ["Recipes"]
    assert bob.get("/api/proposals").json()["to_decide"] == []
    pid = alice.get("/api/proposals").json()["to_decide"][0]["id"]
    assert bob.post(f"/api/proposals/{pid}/approve", json={}, headers=H).status_code == 404
    assert admin.post(f"/api/proposals/{pid}/approve", json={}, headers=H).status_code == 404
    # The author can withdraw their own.
    mine = alice.get("/api/proposals").json()["mine"]
    public = next(p for p in mine if p["payload"]["name"] == "Recipes")
    assert alice.post(f"/api/proposals/{public['id']}/withdraw", headers=H).json()["status"] == "withdrawn"
