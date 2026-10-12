"""MCP tools: what an agent can and cannot do."""

from fastapi.testclient import TestClient

from .conftest import H, Mcp, make_token, sign_in

BODY = "# Design\n\n## Goals\n\nShip.\n\n## Auth\n\nauthentik.\n"


def test_no_tool_can_change_the_taxonomy(mcp):
    agent, _ = mcp
    names = {t["name"] for t in agent.rpc("tools/list").json()["result"]["tools"]}
    assert {"list_domains", "create_document", "edit_document", "diff_versions"} <= names
    assert not [n for n in names if "domain" in n and n != "list_domains"]


def test_unknown_domain_is_refused_with_a_pointer_to_the_user(mcp):
    agent, _ = mcp
    err = agent.error("create_note", title="x", domain="cooking")
    assert "no domain 'cooking'" in err and "web UI" in err


def test_create_read_and_edit_by_section(mcp):
    agent, web = mcp
    doc = agent.call("create_document", title="Design", domain="notes", body=BODY,
                     tags=["infra"], project=None)
    assert doc["url"].endswith("/d/" + doc["id"])
    got = agent.call("get_document", document_id=doc["id"], heading_path=["Auth"])
    assert got["section"] == "## Auth\n\nauthentik.\n" and "body" not in got
    assert [o["path"] for o in got["outline"]][-1] == ["Design", "Auth"]
    r = agent.call("edit_document", document_id=doc["id"], ops=[
        {"op": "replace_section", "heading_path": ["Auth"], "content": "OIDC via authentik."},
        {"op": "insert_section", "heading": "Risks", "content": "None yet.", "after": ["Auth"]}])
    assert r["revision"] == 2 and r["applied"] == ["replaced §Auth", "added §Risks"]
    full = web.get(f"/api/documents/{doc['id']}").json()
    assert full["body"].endswith("## Auth\n\nOIDC via authentik.\n\n## Risks\n\nNone yet.\n")
    rev = web.get(f"/api/documents/{doc['id']}/revisions").json()["revisions"][0]
    assert (rev["client"], rev["source"], rev["summary"]) == (
        "token:laptop", "mcp", "replaced §Auth; added §Risks")
    err = agent.error("edit_document", document_id=doc["id"],
                      ops=[{"op": "delete_section", "heading_path": ["Nope"]}])
    assert "no section 'Nope'" in err


def test_section_edits_apply_on_top_of_concurrent_human_edits(mcp):
    agent, web = mcp
    doc = agent.call("create_document", title="Design", domain="notes", body=BODY)
    # The user edits Goals in the browser after the agent read revision 1.
    web.put(f"/api/documents/{doc['id']}", json={
        "base_revision": 1, "body": BODY.replace("Ship.", "Ship by Friday.")}, headers=H)
    agent.call("edit_document", document_id=doc["id"],
               ops=[{"op": "append_to_section", "heading_path": ["Auth"], "content": "Plus PATs."}])
    body = web.get(f"/api/documents/{doc['id']}").json()["body"]
    assert "Ship by Friday." in body and "Plus PATs." in body


def test_whole_body_rewrite_needs_a_current_base_revision(mcp):
    agent, web = mcp
    doc = agent.call("create_document", title="Design", domain="notes", body=BODY)
    assert "base_revision" in agent.error("update_document", document_id=doc["id"], body="x")
    web.put(f"/api/documents/{doc['id']}", json={"base_revision": 1, "title": "D2"}, headers=H)
    err = agent.error("update_document", document_id=doc["id"], body="x", base_revision=1)
    assert "now at revision 2" in err
    assert agent.call("update_document", document_id=doc["id"], body="x",
                      base_revision=2)["revision"] == 3


def test_tag_hints_on_new_tags(mcp):
    agent, _ = mcp
    agent.call("create_note", title="a", domain="notes", tags=["kubernetes"])
    r = agent.call("create_note", title="b", domain="notes", tags=["Kubernetes", "kubernete"])
    assert r["tag_hints"] == {"kubernete": ["kubernetes"]}


def test_projects_listing_and_trash(mcp):
    agent, _ = mcp
    agent.call("create_project", domain="notes", name="Photo Dump")
    doc = agent.call("create_note", title="Ingress gotcha", domain="notes", project="photo-dump")
    agent.call("create_note", title="Other", domain="notes")
    lst = agent.call("list_documents", domain="notes", project="Photo Dump")
    assert [d["title"] for d in lst["documents"]] == ["Ingress gotcha"]
    assert agent.call("list_documents", title_contains="gotcha")["total"] == 1
    assert agent.call("list_domains")["domains"][0]["projects"] == ["Photo Dump"]
    agent.call("delete_document", document_id=doc["id"])
    assert agent.call("list_documents", trashed=True)["documents"][0]["id"] == doc["id"]
    assert "not found" in agent.error("get_document", document_id=doc["id"])
    agent.call("restore_document", document_id=doc["id"])
    assert agent.call("get_document", document_id=doc["id"])["title"] == "Ingress gotcha"


def test_versions_diff_and_restore(mcp):
    agent, _ = mcp
    doc = agent.call("create_document", title="Design", domain="notes", body=BODY)
    agent.call("save_version", document_id=doc["id"], label="before rewrite")
    agent.call("edit_document", document_id=doc["id"],
               ops=[{"op": "replace_text", "find": "Ship.", "replace": "Ship v2."}])
    diff = agent.call("diff_versions", document_id=doc["id"], from_revision=1)["diff"]
    assert "-Ship.\n" in diff and "+Ship v2.\n" in diff
    vs = agent.call("list_versions", document_id=doc["id"])
    assert vs["current"] == 2 and vs["revisions"][1]["label"] == "before rewrite"
    old = agent.call("get_document", document_id=doc["id"], revision=1)
    assert "Ship.\n" in old["body"]
    assert agent.call("restore_version", document_id=doc["id"], revision=1)["revision"] == 3


def test_agent_has_its_users_role_and_no_more(app_and_provider):
    app, provider = app_and_provider
    alice = sign_in(app, provider, "alice")
    bob = sign_in(app, provider, "bob")
    d = alice.get("/api/home").json()["personal"][0]
    doc = alice.post("/api/documents", json={"domain_id": d["id"], "title": "Plan"},
                     headers=H).json()
    alice.put(f"/api/domains/{d['id']}/grants", json={"username": "bob", "role": "reader"},
              headers=H)
    with TestClient(app) as http:
        agent = Mcp(http, make_token(bob))
        assert agent.call("get_document", document_id=doc["id"])["title"] == "Plan"
        # bob's own "notes" and alice's shared one have the same slug.
        assert "names 2 domains" in agent.error("list_documents", domain="notes")
        assert "can't do that" in agent.error("create_note", title="x", domain=d["id"])
        assert "can't do that" in agent.error(
            "edit_document", document_id=doc["id"], ops=[{"op": "append", "content": "x"}])


def test_mcp_needs_a_token(app_and_provider):
    app, _ = app_and_provider
    with TestClient(app) as http:
        assert Mcp(http, "wdp_nope").rpc("tools/list").status_code == 401
