import json
import os
import uuid

import pytest
from fastapi.testclient import TestClient

from webdoc import oidc
from webdoc.config import Settings
from webdoc.main import create_app

H = {"X-Webdoc": "1"}


class FakeProvider(oidc.Provider):
    """authentik stand-in: any code is accepted and maps to the subject (and
    groups) recorded for it."""

    def __init__(self, settings):
        super().__init__(settings)
        self.subjects: dict[str, tuple[str, list[str]]] = {}

    def metadata(self):
        return {"authorization_endpoint": "https://auth.example/authorize"}

    def exchange(self, code, verifier, redirect_uri):
        if code not in self.subjects:
            raise oidc.SignInRefused("bad code")
        return {"id_token": code}

    def verify(self, id_token, nonce):
        sub, groups = self.subjects[id_token]
        return {"iss": self.settings.oidc_issuer, "sub": sub, "preferred_username": sub,
                "groups": groups}


@pytest.fixture
def database_url(tmp_path):
    """SQLite by default. WEBDOC_TEST_PG=postgresql://user:pw@host:port runs
    every test in a fresh database on that server (it needs pgvector), which
    exercises the Postgres search paths."""
    server = os.environ.get("WEBDOC_TEST_PG")
    if not server:
        yield f"sqlite:///{tmp_path}/t.db"
        return
    import psycopg
    name = "wdtest_" + uuid.uuid4().hex[:12]
    with psycopg.connect(server + "/postgres", autocommit=True) as c:
        c.execute(f"CREATE DATABASE {name}")
    yield f"{server}/{name}"
    with psycopg.connect(server + "/postgres", autocommit=True) as c:
        c.execute(f"DROP DATABASE {name} WITH (FORCE)")


@pytest.fixture
def settings(database_url):
    return Settings(database_url=database_url, public_url="http://testserver",
                    oidc_issuer="https://auth.example/app/", oidc_client_id="wd",
                    oidc_client_secret="s", admin_group="webdoc-admins", editor_group="webdoc-editors",
                    max_document_bytes=20_000, dev_login=True, embedder="hash",
                    background_index=False)


@pytest.fixture
def app_and_provider(settings):
    provider = FakeProvider(settings)
    return create_app(settings, provider), provider


def sign_in(app, provider, sub: str, groups: list[str] | None = None) -> TestClient:
    """Signs in through the fake authentik; an editor unless `groups` says otherwise."""
    client = TestClient(app)
    r = client.get("/api/auth/oidc/login", follow_redirects=False)
    assert r.status_code == 302
    state = r.headers["location"].split("state=")[1].split("&")[0]
    provider.subjects["code-" + sub] = (sub, ["webdoc-editors"] if groups is None else groups)
    r = client.get(f"/api/auth/oidc/callback?code=code-{sub}&state={state}",
                   follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/"
    return client


@pytest.fixture
def client(app_and_provider):
    app, provider = app_and_provider
    return sign_in(app, provider, "alice")


class Mcp:
    def __init__(self, http: TestClient, token: str):
        self.http, self.token, self.n = http, token, 0

    def rpc(self, method, params=None, token=None):
        self.n += 1
        return self.http.post("/mcp", json={"jsonrpc": "2.0", "id": self.n, "method": method,
                                            "params": params or {}},
                              headers={"Authorization": f"Bearer {token or self.token}",
                                       "Accept": "application/json, text/event-stream"})

    def call(self, tool, **args):
        r = self.rpc("tools/call", {"name": tool, "arguments": args})
        assert r.status_code == 200, r.text
        res = r.json()["result"]
        if res.get("isError"):
            raise AssertionError(res["content"][0]["text"])
        if res.get("structuredContent") is not None:
            sc = res["structuredContent"]
            return sc.get("result", sc) if set(sc) == {"result"} else sc
        return json.loads(res["content"][0]["text"])

    def error(self, tool, **args) -> str:
        r = self.rpc("tools/call", {"name": tool, "arguments": args})
        res = r.json()["result"]
        assert res.get("isError"), res
        return res["content"][0]["text"]


def make_token(web: TestClient, name: str = "agent") -> str:
    r = web.post("/api/tokens", json={"name": name}, headers=H)
    assert r.status_code == 201, r.text
    return r.json()["token"]


@pytest.fixture
def mcp(app_and_provider):
    app, provider = app_and_provider
    web = sign_in(app, provider, "alice")
    with TestClient(app) as http:
        yield Mcp(http, make_token(web, "laptop")), web
