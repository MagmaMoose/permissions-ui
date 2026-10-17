"""Fake Entra ID, Microsoft Graph and GitHub behind one httpx MockTransport, and an app wired to them.

The GitHub fake keeps a real little object store (blobs, trees, commits, refs), so the client's
commits, branch rebuilds and squash merges are exercised the way GitHub answers them.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from permissions_ui.app import create_app
from permissions_ui.config import load_config

TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
ORIGIN = "https://access.example.test"
AUTHORITY = "https://login.example.test"
GITHUB_API = "https://api.github.test"
REPO = "example/access"
BOT = "permissions-bot"
TODAY = date(2026, 10, 17)
EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "catalog"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)

PEOPLE = {
    "ada.lovelace@example.com": "Ada Lovelace",
    "grace.hopper@example.com": "Grace Hopper",
    "linus.pauling@example.com": "Linus Pauling",
    "katherine.johnson@example.com": "Katherine Johnson",
    "margaret.hamilton@example.com": "Margaret Hamilton",
    "alan.turing@example.com": "Alan Turing",
    "it.desk@example.com": "IT Desk",
    "auditor@example.com": "Ann Auditor",
}


def oid(upn: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, upn))


@dataclass
class FakeEntra:
    user: str = "alan.turing@example.com"
    roles: list[str] = field(default_factory=list)
    challenge: str = ""
    nonce: str = ""

    def jwks(self) -> dict[str, Any]:
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key()))
        return {"keys": [{**jwk, "kid": "test-key", "use": "sig", "alg": "RS256"}]}

    def id_token(self) -> str:
        now = int(time.time())
        claims = {
            "iss": f"{AUTHORITY}/{TENANT}/v2.0",
            "aud": CLIENT,
            "iat": now,
            "nbf": now,
            "exp": now + 3600,
            "nonce": self.nonce,
            "tid": TENANT,
            "oid": oid(self.user),
            "name": PEOPLE.get(self.user, self.user),
            "preferred_username": self.user,
            "roles": self.roles,
        }
        return jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": "test-key"})

    def token(self, request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        grant = form["grant_type"][0]
        if grant == "client_credentials":
            return httpx.Response(200, json={"access_token": "graph-app-token", "expires_in": 3600})
        if grant == "authorization_code":
            verifier = form["code_verifier"][0]
            digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            if form["code"][0] != "good-code" or digest != self.challenge:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "AADSTS70008: bad code"})
            return httpx.Response(200, json={"token_type": "Bearer", "expires_in": 3600, "id_token": self.id_token()})
        return httpx.Response(400, json={"error": "unsupported_grant_type"})


@dataclass
class FakeGraph:
    users: dict[str, dict[str, Any]] = field(default_factory=dict)
    groups: dict[str, dict[str, Any]] = field(default_factory=dict)
    members: dict[str, set[str]] = field(default_factory=dict)
    writes: list[tuple[str, str, str]] = field(default_factory=list)

    def add_user(self, upn: str, name: str, enabled: bool = True) -> None:
        self.users[oid(upn)] = {"id": oid(upn), "displayName": name, "userPrincipalName": upn, "mail": upn, "accountEnabled": enabled}

    def add_group(self, name: str, members: list[str] = ()) -> str:  # type: ignore[assignment]
        gid = oid(f"group:{name}")
        self.groups[gid] = {"id": gid, "displayName": name, "description": f"{name} group"}
        self.members[gid] = {oid(m) for m in members}
        return gid

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != "Bearer graph-app-token":
            return httpx.Response(401)
        path = unquote(request.url.path).removeprefix("/v1.0")
        query = {k: v[0] for k, v in parse_qs(urlsplit(str(request.url)).query).items()}
        if request.method == "GET" and (m := re.fullmatch(r"/users/([^/]+)", path)):
            key = m.group(1).lower()
            found = self.users.get(key) or next((u for u in self.users.values() if u["userPrincipalName"] == key), None)
            return httpx.Response(200, json=found) if found else httpx.Response(404, json={"error": {"code": "Request_ResourceNotFound"}})
        if request.method == "GET" and path == "/users":
            text = re.search(r'displayName:([^"]+)"', query.get("$search", ""))
            needle = text.group(1).lower() if text else ""
            return httpx.Response(200, json={"value": [u for u in self.users.values() if needle in u["displayName"].lower() or needle in u["userPrincipalName"]]})
        if request.method == "GET" and (m := re.fullmatch(r"/groups/([0-9a-f-]{36})", path)):
            found = self.groups.get(m.group(1))
            return httpx.Response(200, json=found) if found else httpx.Response(404)
        if request.method == "GET" and path == "/groups":
            flt = query.get("$filter", "")
            if m := re.fullmatch(r"displayName eq '(.*)'", flt):
                return httpx.Response(200, json={"value": [g for g in self.groups.values() if g["displayName"] == m.group(1).replace("''", "'")]})
            if m := re.fullmatch(r"startswith\(displayName,'(.*)'\)", flt):
                return httpx.Response(200, json={"value": [g for g in self.groups.values() if g["displayName"].lower().startswith(m.group(1).lower())]})
        if request.method == "GET" and (m := re.fullmatch(r"/groups/([0-9a-f-]{36})/transitiveMembers/microsoft.graph.user", path)):
            ids = self.members.get(m.group(1), set())
            # Two pages, through nextLink.
            users = [self.users[i] for i in sorted(ids) if i in self.users]
            if "page" not in query and len(users) > 1:
                return httpx.Response(200, json={"value": users[:1], "@odata.nextLink": f"{request.url}&page=2"})
            return httpx.Response(200, json={"value": users[1:] if "page" in query else users})
        if request.method == "GET" and (m := re.fullmatch(r"/users/([0-9a-f-]{36})/transitiveMemberOf/microsoft.graph.group", path)):
            return httpx.Response(200, json={"value": [g for gid, g in self.groups.items() if m.group(1) in self.members.get(gid, set())]})
        if request.method == "POST" and (m := re.fullmatch(r"/groups/([0-9a-f-]{36})/members/\$ref", path)):
            user = json.loads(request.content)["@odata.id"].rsplit("/", 1)[1]
            self.members.setdefault(m.group(1), set()).add(user)
            self.writes.append(("add", m.group(1), user))
            return httpx.Response(204)
        if request.method == "DELETE" and (m := re.fullmatch(r"/groups/([0-9a-f-]{36})/members/([0-9a-f-]{36})/\$ref", path)):
            self.members.get(m.group(1), set()).discard(m.group(2))
            self.writes.append(("remove", m.group(1), m.group(2)))
            return httpx.Response(204)
        return httpx.Response(404, json={"error": {"code": "unhandled", "message": f"{request.method} {path}"}})


def _sha(*parts: Any) -> str:
    return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()  # DevSkim: ignore DS126858 - SHA-1 mimics Git's own content-addressing; this is a test fixture, not a security hash


@dataclass
class FakeGitHub:
    blobs: dict[str, str] = field(default_factory=dict)
    trees: dict[str, dict[str, str]] = field(default_factory=dict)
    commits: dict[str, dict[str, Any]] = field(default_factory=dict)
    refs: dict[str, str] = field(default_factory=dict)
    pulls: dict[int, dict[str, Any]] = field(default_factory=dict)
    comments: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    next_number: int = 100
    block_merge: bool = False

    def seed(self, files: dict[str, str]) -> None:
        tree = {}
        for path, text in files.items():
            sha = _sha("blob", text)
            self.blobs[sha] = text
            tree[path] = sha
        tree_sha = _sha("tree", tree)
        self.trees[tree_sha] = tree
        commit = _sha("commit", tree_sha, [])
        self.commits[commit] = {"tree": tree_sha, "parents": [], "message": "seed"}
        self.refs["main"] = commit

    def files(self, ref: str = "main") -> dict[str, str]:
        tree = self.trees[self.commits[self.refs[ref]]["tree"]]
        return {p: self.blobs[s] for p, s in tree.items()}

    def _pull_json(self, n: int) -> dict[str, Any]:
        p = self.pulls[n]
        return {
            "number": n,
            "title": p["title"],
            "body": p["body"],
            "state": p["state"],
            "merged": p["merged"],
            "merged_at": "2026-10-17T10:00:00Z" if p["merged"] else None,
            "labels": [{"name": label} for label in sorted(p["labels"])],
            "head": {"ref": p["head"], "sha": self.refs.get(p["head"], p.get("head_sha", ""))},
            "html_url": f"https://github.test/{REPO}/pull/{n}",
            "user": {"login": BOT},
            "created_at": "2026-10-17T09:00:00Z",
            "updated_at": "2026-10-17T09:00:00Z",
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != "Bearer gh-token":
            return httpx.Response(401, json={"message": "Bad credentials"})
        path = unquote(request.url.path)
        body = json.loads(request.content) if request.content else {}
        query = {k: v[0] for k, v in parse_qs(urlsplit(str(request.url)).query).items()}
        base = f"/repos/{REPO}"
        if path == "/user":
            return httpx.Response(200, json={"login": BOT})
        if not path.startswith(base):
            return httpx.Response(404, json={"message": "Not Found"})
        path = path[len(base) :]
        method = request.method
        if method == "GET" and (m := re.fullmatch(r"/git/ref/heads/(.+)", path)):
            return httpx.Response(200, json={"object": {"sha": self.refs[m.group(1)]}}) if m.group(1) in self.refs else httpx.Response(404, json={"message": "Not Found"})
        if method == "GET" and (m := re.fullmatch(r"/git/trees/([0-9a-f]+)", path)):
            tree = self.trees[self.commits[m.group(1)]["tree"]]
            return httpx.Response(200, json={"tree": [{"path": p, "type": "blob", "sha": s} for p, s in tree.items()]})
        if method == "GET" and (m := re.fullmatch(r"/git/blobs/([0-9a-f]+)", path)):
            return httpx.Response(200, json={"content": base64.b64encode(self.blobs[m.group(1)].encode()).decode(), "encoding": "base64"})
        if method == "GET" and (m := re.fullmatch(r"/git/commits/([0-9a-f]+)", path)):
            return httpx.Response(200, json={"sha": m.group(1), "tree": {"sha": self.commits[m.group(1)]["tree"]}})
        if method == "POST" and path == "/git/trees":
            tree = dict(self.trees[body["base_tree"]])
            for entry in body["tree"]:
                if entry.get("content") is not None:
                    sha = _sha("blob", entry["content"])
                    self.blobs[sha] = entry["content"]
                    tree[entry["path"]] = sha
                else:
                    tree.pop(entry["path"], None)
            sha = _sha("tree", tree)
            self.trees[sha] = tree
            return httpx.Response(201, json={"sha": sha})
        if method == "POST" and path == "/git/commits":
            sha = _sha("commit", body["tree"], body["parents"], body["message"], time.time_ns())
            self.commits[sha] = {"tree": body["tree"], "parents": body["parents"], "message": body["message"]}
            return httpx.Response(201, json={"sha": sha})
        if method == "POST" and path == "/git/refs":
            name = body["ref"].removeprefix("refs/heads/")
            if name in self.refs:
                return httpx.Response(422, json={"message": "Reference already exists"})
            self.refs[name] = body["sha"]
            return httpx.Response(201, json={"ref": body["ref"]})
        if method == "PATCH" and (m := re.fullmatch(r"/git/refs/heads/(.+)", path)):
            self.refs[m.group(1)] = body["sha"]
            return httpx.Response(200, json={})
        if method == "POST" and path == "/pulls":
            n = self.next_number
            self.next_number += 1
            self.pulls[n] = {"title": body["title"], "body": body["body"], "head": body["head"], "state": "open", "merged": False, "labels": set()}
            self.comments[n] = []
            return httpx.Response(201, json=self._pull_json(n))
        if method == "GET" and (m := re.fullmatch(r"/pulls/(\d+)", path)):
            return httpx.Response(200, json=self._pull_json(int(m.group(1))))
        if method == "PATCH" and (m := re.fullmatch(r"/pulls/(\d+)", path)):
            self.pulls[int(m.group(1))]["state"] = body["state"]
            return httpx.Response(200, json=self._pull_json(int(m.group(1))))
        if method == "GET" and path == "/issues":
            label, state = query.get("labels", ""), query.get("state", "open")
            found = [
                {**self._pull_json(n), "pull_request": {"merged_at": "2026-10-17T10:00:00Z" if p["merged"] else None}}
                for n, p in sorted(self.pulls.items(), reverse=True)
                if label in p["labels"] and (state == "all" or p["state"] == state)
            ]
            return httpx.Response(200, json=found)
        if m := re.fullmatch(r"/issues/(\d+)/comments", path):
            n = int(m.group(1))
            if method == "POST":
                self.comments[n].append({"id": len(self.comments[n]) + 1, "user": {"login": body.get("_as", BOT)}, "body": body["body"], "created_at": "2026-10-17T09:30:00Z"})
                return httpx.Response(201, json={})
            return httpx.Response(200, json=self.comments[n])
        if method == "POST" and (m := re.fullmatch(r"/issues/(\d+)/labels", path)):
            self.pulls[int(m.group(1))]["labels"] |= set(body["labels"])
            return httpx.Response(200, json=[])
        if method == "DELETE" and (m := re.fullmatch(r"/issues/(\d+)/labels/(.+)", path)):
            labels = self.pulls[int(m.group(1))]["labels"]
            if m.group(2) not in labels:
                return httpx.Response(404, json={"message": "Label does not exist"})
            labels.discard(m.group(2))
            return httpx.Response(200, json=[])
        if method == "POST" and re.fullmatch(r"/pulls/(\d+)/reviews", path):
            return httpx.Response(200, json={})
        if method == "PUT" and (m := re.fullmatch(r"/pulls/(\d+)/merge", path)):
            n = int(m.group(1))
            pull = self.pulls[n]
            head = self.refs[pull["head"]]
            if self.block_merge:
                return httpx.Response(405, json={"message": "Required status check is expected"})
            if body["sha"] != head or self.commits[head]["parents"] != [self.refs["main"]]:
                return httpx.Response(409, json={"message": "Head branch is out of date"})
            squashed = _sha("commit", self.commits[head]["tree"], [self.refs["main"]], body["commit_title"])
            self.commits[squashed] = {"tree": self.commits[head]["tree"], "parents": [self.refs["main"]], "message": body["commit_title"]}
            self.refs["main"] = squashed
            pull["merged"], pull["state"] = True, "closed"
            return httpx.Response(200, json={"merged": True})
        return httpx.Response(404, json={"message": f"unhandled {method} {path}"})


@dataclass
class World:
    entra: FakeEntra = field(default_factory=FakeEntra)
    graph: FakeGraph = field(default_factory=FakeGraph)
    github: FakeGitHub = field(default_factory=FakeGitHub)
    notifications: list[dict[str, Any]] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(f"{AUTHORITY}/{TENANT}/oauth2/v2.0/token"):
            return self.entra.token(request)
        if url.startswith(f"{AUTHORITY}/{TENANT}/discovery/v2.0/keys"):
            return httpx.Response(200, json=self.entra.jwks())
        if url.startswith("https://graph.microsoft.com/"):
            return self.graph.handle(request)
        if url.startswith(GITHUB_API):
            return self.github.handle(request)
        if url.startswith("https://hooks.example.test/"):
            self.notifications.append(json.loads(request.content))
            return httpx.Response(202)
        return httpx.Response(404)


def example_files(root: str = "access") -> dict[str, str]:
    return {f"{root}/{p.relative_to(EXAMPLE)}": p.read_text() for p in sorted(EXAMPLE.rglob("*.yaml"))}


@pytest.fixture
def world() -> World:
    w = World()
    for upn, name in PEOPLE.items():
        w.graph.add_user(upn, name)
    w.graph.add_group("k8s-acc-payments-write", ["ada.lovelace@example.com"])
    w.graph.add_group("k8s-prd-payments-read", ["ada.lovelace@example.com", "grace.hopper@example.com", "alan.turing@example.com"])
    w.graph.add_group("k8s-prd-cluster-admin", ["grace.hopper@example.com"])
    w.graph.add_group("grafana-viewer", ["ada.lovelace@example.com", "grace.hopper@example.com"])
    w.graph.add_group("payments-keyvault-acc", [])
    w.graph.add_group("acl-mystery-group", ["alan.turing@example.com"])
    w.github.seed(example_files())
    return w


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    token = tmp_path / "azure-identity-token"
    token.write_text("header.workload-identity-token.signature\n")
    return {
        "AZURE_TENANT_ID": TENANT,
        "AZURE_CLIENT_ID": CLIENT,
        "AZURE_AUTHORITY_HOST": AUTHORITY,
        "AZURE_FEDERATED_TOKEN_FILE": str(token),
        "PERMISSIONS_UI_PUBLIC_ORIGIN": ORIGIN,
        "PERMISSIONS_UI_SESSION_KEY": "k" * 48,
        "PERMISSIONS_UI_GITHUB_REPO": REPO,
        "PERMISSIONS_UI_GITHUB_TOKEN": "gh-token",
        "PERMISSIONS_UI_GITHUB_API": GITHUB_API,
        "PERMISSIONS_UI_CATALOG_PATH": "access",
        "PERMISSIONS_UI_GROUP_PREFIXES": "acl-,k8s-",
        "PERMISSIONS_UI_TEAMS_WEBHOOK": "https://hooks.example.test/workflow",
        "PERMISSIONS_UI_STATIC_DIR": str(tmp_path / "no-frontend"),
    }


def make_client(world: World, env: dict[str, str]) -> TestClient:
    app = create_app(load_config(env), transport=httpx.MockTransport(world.handler), today=lambda: TODAY)
    return TestClient(app, base_url=ORIGIN, follow_redirects=False)


@pytest.fixture
def client(world: World, env: dict[str, str]) -> TestClient:
    return make_client(world, env)


def sign_in(client: TestClient, world: World, upn: str = "alan.turing@example.com", roles: list[str] = ()) -> dict[str, Any]:  # type: ignore[assignment]
    """The whole browser round trip: /auth/login, Microsoft, /auth/callback. Returns /api/me."""
    world.entra.user, world.entra.roles = upn, list(roles)
    client.cookies.clear()
    login = client.get("/auth/login")
    assert login.status_code == 302
    query = parse_qs(urlsplit(login.headers["location"]).query)
    world.entra.challenge = query["code_challenge"][0]
    world.entra.nonce = query["nonce"][0]
    callback = client.get("/auth/callback", params={"code": "good-code", "state": query["state"][0]})
    assert callback.status_code == 303, callback.text
    assert callback.headers["location"] == "/"
    me = client.get("/api/me")
    assert me.status_code == 200, me.text
    return me.json()


def write_headers(me: dict[str, Any], origin: str = ORIGIN) -> dict[str, str]:
    return {"Origin": origin, "X-CSRF-Token": me["csrf"]}
