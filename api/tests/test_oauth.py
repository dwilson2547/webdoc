"""MCP OAuth: discovery, registration, consent, code exchange, refresh, revocation."""

import base64
import hashlib
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from webdoc.config import Settings
from webdoc.main import create_app

from .conftest import FakeProvider, H, sign_in
from .conftest import Mcp

REDIRECT = "http://127.0.0.1:9999/callback"
VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()


@pytest.fixture
def env(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path}/o.db", public_url="http://localhost",
                        oidc_issuer="https://auth.example/app/", oidc_client_id="wd",
                        oidc_client_secret="s", max_document_bytes=20_000)
    provider = FakeProvider(settings)
    app = create_app(settings, provider)
    with TestClient(app) as http:
        yield app, provider, http


def register(http, redirect=REDIRECT, name="Claude Code"):
    return http.post("/register", json={"redirect_uris": [redirect], "client_name": name,
                                        "token_endpoint_auth_method": "none",
                                        "grant_types": ["authorization_code", "refresh_token"],
                                        "response_types": ["code"]})


def authorize(http, client_id, state="st4te"):
    r = http.get("/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
        "code_challenge": CHALLENGE, "code_challenge_method": "S256", "state": state,
        "resource": "http://localhost/mcp"}, follow_redirects=False)
    assert r.status_code == 302, r.text
    loc = r.headers["location"]
    assert loc.startswith("http://localhost/oauth/consent?req=")
    return parse_qs(urlsplit(loc).query)["req"][0]


def consent_form(web, req):
    r = web.get(f"/oauth/consent?req={req}")
    assert r.status_code == 200, r.text
    return r, re.search(r'name="nonce" value="([^"]+)"', r.text).group(1)


def approve(web, req, nonce, decision="allow"):
    return web.post("/oauth/consent", data={"req": req, "nonce": nonce, "decision": decision},
                    follow_redirects=False)


def connect(app, provider, http, user="alice"):
    """Run the whole browser flow; returns (token response, signed-in web client)."""
    client_id = register(http).json()["client_id"]
    req = authorize(http, client_id)
    web = sign_in(app, provider, user)
    _, nonce = consent_form(web, req)
    r = approve(web, req, nonce)
    assert r.status_code == 303
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert q["state"] == ["st4te"]
    tok = http.post("/token", data={"grant_type": "authorization_code", "code": q["code"][0],
                                    "redirect_uri": REDIRECT, "client_id": client_id,
                                    "code_verifier": VERIFIER})
    assert tok.status_code == 200, tok.text
    return {**tok.json(), "client_id": client_id, "code": q["code"][0]}, web


def test_discovery(env):
    _, _, http = env
    r = http.post("/mcp", json={}, headers={"Accept": "application/json, text/event-stream"})
    assert r.status_code == 401
    challenge = r.headers["www-authenticate"]
    assert 'resource_metadata="http://localhost/.well-known/oauth-protected-resource/mcp"' in challenge
    prm = http.get("/.well-known/oauth-protected-resource/mcp").json()
    assert prm["resource"] == "http://localhost/mcp"
    assert prm["authorization_servers"] == ["http://localhost/"] or \
        prm["authorization_servers"] == ["http://localhost"]
    assert http.get("/.well-known/oauth-protected-resource").status_code == 200
    meta = http.get("/.well-known/oauth-authorization-server").json()
    assert meta["registration_endpoint"].endswith("/register")
    assert meta["code_challenge_methods_supported"] == ["S256"]


def test_registration_rejects_remote_http_redirects(env):
    _, _, http = env
    assert register(http, "http://evil.example/cb").status_code == 400
    assert register(http, "javascript:alert(1)").status_code == 400
    assert register(http, "https://vscode.dev/redirect").status_code == 201
    assert register(http, "cursor://anysphere.cursor-mcp/oauth").status_code == 201


def test_full_flow_and_mcp_calls(env):
    app, provider, http = env
    tok, web = connect(app, provider, http)
    assert tok["access_token"].startswith("wdo_") and tok["refresh_token"].startswith("wdr_")
    mcp = Mcp(http, tok["access_token"])
    d = mcp.call("list_domains")["domains"][0]
    assert d["slug"] == "notes"
    doc = mcp.call("create_note", title="From the web bot", domain="notes", body="hi")
    rev = web.get(f"/api/documents/{doc['id']}/revisions").json()["revisions"][0]
    assert (rev["by"], rev["client"], rev["source"]) == ("alice", "Claude Code", "mcp")

    grants = web.get("/api/oauth/grants").json()["grants"]
    assert [g["client_name"] for g in grants] == ["Claude Code"]


def test_code_is_single_use_and_needs_the_verifier(env):
    app, provider, http = env
    tok, _ = connect(app, provider, http)
    again = http.post("/token", data={"grant_type": "authorization_code", "code": tok["code"],
                                      "redirect_uri": REDIRECT, "client_id": tok["client_id"],
                                      "code_verifier": VERIFIER})
    assert again.status_code == 400

    client_id = tok["client_id"]
    req = authorize(http, client_id)
    web = sign_in(app, provider, "alice")
    _, nonce = consent_form(web, req)
    code = parse_qs(urlsplit(approve(web, req, nonce).headers["location"]).query)["code"][0]
    bad = http.post("/token", data={"grant_type": "authorization_code", "code": code,
                                    "redirect_uri": REDIRECT, "client_id": client_id,
                                    "code_verifier": "w" * 64})
    assert bad.status_code == 400


def test_refresh_rotates_and_revoke_disconnects(env):
    app, provider, http = env
    tok, web = connect(app, provider, http)
    r = http.post("/token", data={"grant_type": "refresh_token", "client_id": tok["client_id"],
                                  "refresh_token": tok["refresh_token"]})
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["access_token"] != tok["access_token"]
    assert Mcp(http, tok["access_token"]).rpc("tools/list").status_code == 401
    assert Mcp(http, new["access_token"]).rpc("tools/list").status_code == 200
    # The old refresh token is gone too.
    assert http.post("/token", data={"grant_type": "refresh_token", "client_id": tok["client_id"],
                                     "refresh_token": tok["refresh_token"]}).status_code == 400

    gid = web.get("/api/oauth/grants").json()["grants"][0]["id"]
    assert web.delete(f"/api/oauth/grants/{gid}", headers=H).status_code == 204
    assert Mcp(http, new["access_token"]).rpc("tools/list").status_code == 401


def test_consent_is_bound_to_the_page_and_user(env):
    app, provider, http = env
    client_id = register(http).json()["client_id"]
    req = authorize(http, client_id)
    alice = sign_in(app, provider, "alice")
    page, nonce = consent_form(alice, req)
    assert "Claude Code" in page.text and "127.0.0.1:9999" in page.text
    assert "form-action 'self' http://127.0.0.1:9999" in page.headers["content-security-policy"]
    assert page.headers["x-frame-options"] == "DENY"

    assert approve(alice, req, "forged").status_code == 400
    bob = sign_in(app, provider, "bob")
    assert bob.get(f"/oauth/consent?req={req}").status_code == 403
    assert approve(bob, req, nonce).status_code == 400

    denied = approve(alice, req, nonce, "deny")
    assert denied.status_code == 303
    assert "error=access_denied" in denied.headers["location"]
    # A decided request is gone.
    assert alice.get(f"/oauth/consent?req={req}").status_code == 400


def test_consent_sends_signed_out_users_through_sign_in(env):
    app, provider, http = env
    client_id = register(http).json()["client_id"]
    req = authorize(http, client_id)
    anon = TestClient(app)
    r = anon.get(f"/oauth/consent?req={req}", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("/api/auth/oidc/login?next=")
    r = anon.get(r.headers["location"], follow_redirects=False)
    state = r.headers["location"].split("state=")[1].split("&")[0]
    provider.subjects["code-carol"] = ("carol", [])
    r = anon.get(f"/api/auth/oidc/callback?code=code-carol&state={state}", follow_redirects=False)
    assert r.headers["location"] == f"/oauth/consent?req={req}"
    assert anon.get(r.headers["location"]).status_code == 200


def test_login_ignores_foreign_next(env):
    app, provider, _ = env
    anon = TestClient(app)
    r = anon.get("/api/auth/oidc/login?next=https://evil.example/", follow_redirects=False)
    state = r.headers["location"].split("state=")[1].split("&")[0]
    provider.subjects["code-dave"] = ("dave", [])
    r = anon.get(f"/api/auth/oidc/callback?code=code-dave&state={state}", follow_redirects=False)
    assert r.headers["location"] == "/"


def test_wrong_resource_is_refused(env):
    _, _, http = env
    client_id = register(http).json()["client_id"]
    r = http.get("/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
        "code_challenge": CHALLENGE, "code_challenge_method": "S256", "state": "s",
        "resource": "https://other.example/mcp"}, follow_redirects=False)
    assert r.status_code == 302 and "error=invalid_target" in r.headers["location"]


def test_pat_still_works(env):
    app, provider, http = env
    web = sign_in(app, provider, "alice")
    raw = web.post("/api/tokens", json={"name": "ci"}, headers=H).json()["token"]
    assert Mcp(http, raw).rpc("tools/list").status_code == 200
