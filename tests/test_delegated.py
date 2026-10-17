import asyncio
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from conftest import TODAY, make_client, sign_in

from permissions_ui.app import build_portal
from permissions_ui.config import load_config
from permissions_ui.directory import DirectoryError
from permissions_ui.session import Sealer, TokenCache

GRAPH = "https://graph.microsoft.com/User.Read.All https://graph.microsoft.com/GroupMember.Read.All"


@pytest.fixture
def delegated(env):
    return {**env, "PERMISSIONS_UI_DIRECTORY": "delegated"}


def grants(world) -> list[str]:
    return [form["grant_type"][0] for form in world.entra.token_requests]


def test_the_sign_in_asks_for_the_persons_graph_access(world, delegated):
    login = make_client(world, delegated).get("/auth/login")
    assert parse_qs(urlsplit(login.headers["location"]).query)["scope"] == [f"openid profile offline_access {GRAPH}"]


def test_the_directory_is_read_as_the_signed_in_person(world, delegated):
    client = make_client(world, delegated)
    sign_in(client, world, "margaret.hamilton@example.com")
    alan = client.get("/api/people/alan.turing@example.com").json()
    assert alan["extra"] == ["k8s-prd-payments-read"] and alan["unknown_groups"] == ["acl-mystery-group"]
    assert {who for who, _ in world.graph.calls} == {"margaret.hamilton@example.com"}
    # The sign-in's own Graph token serves the first reads; the app never asks for one of its own.
    assert grants(world) == ["authorization_code"]


def test_the_session_cookie_holds_the_refresh_token_and_never_an_access_token(world, delegated):
    client = make_client(world, delegated)
    sign_in(client, world)
    sealed = "".join(value for name, value in sorted(client.cookies.items()) if name.startswith("__Host-permissions-ui."))
    data = Sealer(delegated["PERMISSIONS_UI_SESSION_KEY"].encode()).open("session", sealed)
    assert data["rt"] == "rt:alan.turing@example.com" and data["sid"]
    assert "graph:" not in json.dumps(data)


def test_another_replica_redeems_the_refresh_token_with_the_apps_credential(world, delegated):
    first = make_client(world, delegated)
    sign_in(first, world, "margaret.hamilton@example.com")
    second = make_client(world, delegated)
    second.cookies = first.cookies
    assert second.get("/api/people/alan.turing@example.com").status_code == 200
    assert second.get("/api/people/ada.lovelace@example.com").status_code == 200
    refresh = [form for form in world.entra.token_requests if form["grant_type"] == ["refresh_token"]]
    assert len(refresh) == 1 and refresh[0]["scope"] == [f"offline_access {GRAPH}"]
    assert refresh[0]["refresh_token"] == ["rt:margaret.hamilton@example.com"]
    assert refresh[0]["client_assertion"] == ["header.workload-identity-token.signature"]


@pytest.mark.parametrize(("refusal", "status", "error"), [((400, "invalid_grant"), 401, "signed_out"), ((503, "temporarily_unavailable"), 502, "directory_unavailable")])
def test_a_refused_refresh(world, delegated, refusal, status, error):
    first = make_client(world, delegated)
    sign_in(first, world, "margaret.hamilton@example.com")
    world.entra.refresh_error = refusal
    second = make_client(world, delegated)
    second.cookies = first.cookies
    response = second.get("/api/people/alan.turing@example.com")
    assert response.status_code == status and response.json()["error"] == error


def test_a_session_from_the_app_mode_signs_in_again(world, env, delegated):
    before = make_client(world, env)
    sign_in(before, world)
    after = make_client(world, delegated)
    after.cookies = before.cookies
    assert after.get("/api/me").json()["error"] == "signed_out"


@pytest.mark.parametrize(("mode", "reads"), [("app", ["app"]), ("delegated", ["margaret.hamilton@example.com", "katherine.johnson@example.com"])])
def test_one_persons_directory_answers_are_never_served_to_another(world, env, mode, reads):
    client = make_client(world, {**env, "PERMISSIONS_UI_DIRECTORY": mode})
    for lead in ("margaret.hamilton@example.com", "katherine.johnson@example.com"):
        sign_in(client, world, lead)
        assert client.get("/api/people/alan.turing@example.com").status_code == 200
    assert [who for who, path in world.graph.calls if path == "/users/alan.turing@example.com"] == reads


def test_expire_needs_no_person_and_no_graph(world, delegated):
    cfg = load_config(delegated)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(world.handler)) as http:
            portal = build_portal(cfg, http, today=lambda: TODAY.replace(month=12))
            opened = await portal.expire()
            with pytest.raises(DirectoryError, match="nobody is signed in"):
                await portal.directory.user("alan.turing@example.com")
            return opened

    assert len(asyncio.run(run())) == 1
    assert world.graph.calls == [] and world.entra.token_requests == []


def test_the_token_cache_drops_tokens_near_expiry_and_beyond_its_size():
    cache = TokenCache(size=2)
    cache.put("a", "token-a", time.time() + 3600)
    cache.put("b", "token-b", time.time() + 60)
    assert cache.get("a") == "token-a" and cache.get("b") is None
    cache.put("c", "token-c", time.time() + 3600)
    cache.put("d", "token-d", time.time() + 3600)
    assert cache.get("a") is None and cache.get("d") == "token-d"
