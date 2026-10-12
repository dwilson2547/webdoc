"""Search, find and the activity feed: design §8, §9.4."""

from fastapi.testclient import TestClient

from webdoc import indexer, search

from .conftest import H, Mcp, make_token, sign_in

AUTH = """Intro line about the service.

## Auth

Sign-in goes through authentik with PKCE. Tokens are hashed.

### Sessions

Cookie sessions last 30 days.

## Storage

Postgres with pgvector holds the embeddings.

```sh
# not a heading
psql -c 'select 1'
```
"""


def personal(c) -> dict:
    return c.get("/api/home").json()["personal"][0]


def doc(c, domain_id, title, body="", kind="document", **kw) -> dict:
    r = c.post("/api/documents", json={"domain_id": domain_id, "title": title, "body": body,
                                       "kind": kind, **kw}, headers=H)
    assert r.status_code == 201, r.text
    return r.json()


def index(app) -> int:
    return app.state.indexer.run_once()


# --- chunking -------------------------------------------------------------------

def test_chunks_follow_headings_and_skip_fenced_ones():
    chunks = indexer.chunk("Webdoc", AUTH)
    assert [c["heading"] for c in chunks] == ["", "Auth", "Auth > Sessions", "Storage"]
    assert chunks[0]["content"] == "Webdoc\n\nIntro line about the service."
    assert chunks[1]["content"].startswith("Webdoc\nAuth\n\nSign-in goes through")
    assert "# not a heading" in chunks[3]["content"]
    assert indexer.chunk("Empty", "") == [{"heading": "", "line": 0, "content": "Empty"}]
    assert indexer.chunk("Only heads", "## A\n\n## B\n") == [
        {"heading": "", "line": 0, "content": "Only heads"}]


def test_long_sections_split_at_paragraphs():
    para = "word " * 150                       # ~750 chars
    pieces = indexer._pieces(f"{para}\n\n{para}\n\n{'x' * 2500}")
    assert len(pieces) == 5 and all(len(p) <= indexer.MAX_CHARS for p in pieces)


def test_only_changed_chunks_are_re_embedded(app_and_provider):
    app, provider = app_and_provider
    c = sign_in(app, provider, "alice")
    d = personal(c)
    x = doc(c, d["id"], "Webdoc", AUTH)
    with app.state.db.session() as s:
        from webdoc.models import Document
        assert indexer.index_document(s, app.state.indexer.embedder, s.get(Document, x["id"])) == 4
    r = c.put(f"/api/documents/{x['id']}", json={
        "base_revision": 1, "body": AUTH.replace("30 days", "14 days")}, headers=H)
    assert r.status_code == 200
    with app.state.db.session() as s:
        assert indexer.pending(s) == 1
        assert indexer.index_document(s, app.state.indexer.embedder, s.get(Document, x["id"])) == 1
        assert indexer.pending(s) == 0


# --- search ---------------------------------------------------------------------

def test_search_ranks_by_content_and_labels_sources(app_and_provider):
    app, provider = app_and_provider
    admin = sign_in(app, provider, "root", ["webdoc-admins"])
    pub = admin.post("/api/domains", json={"name": "Fixes", "scope": "public"}, headers=H).json()
    doc(admin, pub["id"], "Traefik 404 after cert renewal", "## Fix\n\nRestart the ingress pods.")
    alice = sign_in(app, provider, "alice")
    mine = personal(alice)
    doc(alice, mine["id"], "Webdoc", AUTH)
    doc(alice, mine["id"], "Groceries", "eggs, milk", kind="note")
    bob = sign_in(app, provider, "bob")
    doc(bob, personal(bob)["id"], "Bob's secret authentik notes", "authentik PKCE secrets")

    r = alice.get("/api/search", params={"q": "authentik PKCE sign-in"}).json()
    assert r["pending"] == 3 and r["results"] == []
    index(app)
    r = alice.get("/api/search", params={"q": "authentik PKCE sign-in"}).json()
    assert r["semantic"] and r["pending"] == 0
    titles = [x["title"] for x in r["results"]]
    assert titles[0] == "Webdoc" and "Bob's secret authentik notes" not in titles
    top = r["results"][0]
    assert top["source"] == "personal" and top["heading"] == "Auth"
    assert "authentik" in top["snippet"] and set(top["matched"]) >= {"semantic", "keyword"}

    r = alice.get("/api/search", params={"q": "ingress restart"}).json()
    assert r["results"][0]["source"] == "public" and r["results"][0]["domain"] == "Fixes"
    anon = TestClient(app).get("/api/search", params={"q": "authentik ingress"}).json()
    assert {x["source"] for x in anon["results"]} == {"public"}

    # Filters narrow without leaking.
    r = alice.get("/api/search", params={"q": "milk", "kind": "note"}).json()
    assert [x["title"] for x in r["results"]] == ["Groceries"]
    r = alice.get("/api/search", params={"q": "authentik", "source": "public"}).json()
    assert all(x["source"] == "public" for x in r["results"])
    r = alice.get("/api/search", params={"q": "pkce", "author": "bob"}).json()
    assert r["results"] == []
    r = alice.get("/api/search", params={"q": "x", "domain_id": personal(bob)["id"]})
    assert r.status_code == 404


def test_title_matches_before_indexing_and_trash_hides(app_and_provider):
    app, provider = app_and_provider
    c = sign_in(app, provider, "alice")
    x = doc(c, personal(c)["id"], "Kubernetes upgrade runbook", "steps")
    r = c.get("/api/search", params={"q": "upgrade runbook"}).json()
    assert [y["title"] for y in r["results"]] == ["Kubernetes upgrade runbook"]
    assert r["results"][0]["matched"] == ["title"]
    index(app)
    assert c.delete(f"/api/documents/{x['id']}", headers=H).status_code == 204
    assert c.get("/api/search", params={"q": "upgrade runbook"}).json()["results"] == []


def test_shared_domains_are_labelled_shared(app_and_provider):
    app, provider = app_and_provider
    alice = sign_in(app, provider, "alice")
    bob = sign_in(app, provider, "bob")
    d = personal(alice)
    doc(alice, d["id"], "Shared plan", "the migration plan")
    assert alice.put(f"/api/domains/{d['id']}/grants", json={"username": "bob", "role": "reader"},
                     headers=H).status_code == 200
    index(app)
    r = bob.get("/api/search", params={"q": "migration plan"}).json()
    assert r["results"][0]["source"] == "shared" and r["results"][0]["owner"] == "alice"


# --- find -----------------------------------------------------------------------

def test_find_is_literal_with_word_boundaries(app_and_provider):
    app, provider = app_and_provider
    c = sign_in(app, provider, "alice")
    d = personal(c)
    a = doc(c, d["id"], "Errors", "## Boot\n\nGot E-1234 then\nerror E-12345 again\n\n"
                                  "## Calls\n\nfoo.bar() failed; Foo.Bar() too\n")
    doc(c, d["id"], "Other", "nothing here about E-123 at all")

    r = c.get("/api/search", params={"q": "E-1234", "mode": "find"}).json()
    assert [x["title"] for x in r["results"]] == ["Errors"]
    hit = r["results"][0]
    assert hit["count"] == 1 and hit["matches"] == [
        {"line": 3, "heading": "Boot", "text": "Got E-1234 then", "in": "body"}]

    r = c.get("/api/search", params={"q": "E-1234", "mode": "find", "whole_word": "false"}).json()
    assert r["results"][0]["count"] == 2

    r = c.get("/api/search", params={"q": "foo.bar()", "mode": "find"}).json()
    assert r["results"][0]["count"] == 2
    r = c.get("/api/search", params={"q": "foo.bar()", "mode": "find",
                                     "case_sensitive": "true"}).json()
    assert r["results"][0]["count"] == 1 and r["results"][0]["matches"][0]["heading"] == "Calls"

    r = c.get("/api/search", params={"q": "errors", "mode": "find"}).json()
    assert r["results"][0]["matches"][0]["in"] == "title" and r["results"][0]["id"] == a["id"]
    assert c.get("/api/search", params={"q": "%", "mode": "find"}).json()["results"] == []


def test_find_pattern_edges():
    assert search.pattern("x+y", True, False).search("a x+y b")
    assert not search.pattern("cat", True, False).search("concatenate")
    assert search.pattern("cat", False, False).search("conCATenate")
    assert search.pattern(".env", True, False).search("copy .env.example")


# --- activity -------------------------------------------------------------------

def test_activity_feed_follows_read_access(app_and_provider):
    app, provider = app_and_provider
    admin = sign_in(app, provider, "root", ["webdoc-admins"])
    pub = admin.post("/api/domains", json={"name": "Fixes", "scope": "public"}, headers=H).json()
    doc(admin, pub["id"], "Public fix", "x")
    alice = sign_in(app, provider, "alice")
    doc(alice, personal(alice)["id"], "Private thing", "x")

    mine = alice.get("/api/activity").json()["events"]
    assert {e["title"] for e in mine} >= {"Public fix", "Private thing"}
    anon = TestClient(app).get("/api/activity").json()["events"]
    assert "Private thing" not in {e["title"] for e in anon}
    bob = sign_in(app, provider, "bob")
    assert bob.get(f"/api/activity?domain_id={personal(alice)['id']}").status_code == 404
    one = alice.get(f"/api/activity?domain_id={pub['id']}&limit=1").json()
    assert len(one["events"]) == 1 and one["more"]


# --- MCP ------------------------------------------------------------------------

def test_mcp_search_find_and_whats_new(mcp: tuple[Mcp, TestClient], app_and_provider):
    agent, web = mcp
    app, _ = app_and_provider
    d = personal(web)
    doc(web, d["id"], "Webdoc", AUTH)
    index(app)

    r = agent.call("search", query="how do sessions work", domain=d["slug"])
    assert r["results"][0]["title"] == "Webdoc"
    assert r["results"][0]["url"].startswith("http://testserver/d/")
    r = agent.call("find", text="pgvector")
    assert r["results"][0]["matches"][0]["heading"] == "Storage"

    first = agent.call("whats_new")
    titles = {c["title"] for c in first["changes"]}
    assert "Webdoc" in titles
    assert agent.call("whats_new")["changes"] == []          # the mark moved

    # The agent's own writes aren't news to it; the user's web edits are.
    agent.call("create_note", title="agent note", domain=d["slug"], body="hi")
    assert agent.call("whats_new")["changes"] == []
    doc(web, d["id"], "Later", "from the web")
    news = agent.call("whats_new")["changes"]
    assert [c["title"] for c in news] == ["Later"] and news[0]["by"] == ["alice"]

    # since= looks back without moving the mark.
    back = agent.call("whats_new", since="2000-01-01T00:00:00", include_own=True)
    assert {"agent note", "Later", "Webdoc"} <= {c["title"] for c in back["changes"]}
    assert agent.call("whats_new")["changes"] == []
    assert "ISO" in agent.error("whats_new", since="yesterday")
