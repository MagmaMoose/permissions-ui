import asyncio
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import yaml
from conftest import EXAMPLE, TODAY, make_client, oid, sign_in, write_headers

from permissions_ui.app import build_portal
from permissions_ui.config import load_config


def grants_on_main(world, person: str) -> dict:
    text = world.github.files().get(f"access/grants/{person}.yaml")
    return yaml.safe_load(text) if text else {}


def ask(client, me, **body):
    return client.post("/api/requests", json=body, headers=write_headers(me))


def test_the_api_needs_a_session(client):
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/catalog").status_code == 401


def test_writes_need_the_csrf_token_and_this_origin(client, world):
    me = sign_in(client, world)
    body = {"person": me["upn"], "items": [{"resource": "grafana-viewer"}], "reason": "dashboards"}
    assert client.post("/api/requests", json=body).status_code == 403
    assert client.post("/api/requests", json=body, headers=write_headers(me, origin="https://evil.test")).status_code == 403


def test_me_says_what_the_person_may_see(client, world):
    me = sign_in(client, world, "katherine.johnson@example.com")
    assert me["lead_of"] == ["payments"] and me["sees_everyone"] and not me["admin"]
    me = sign_in(client, world, "alan.turing@example.com")
    assert me["lead_of"] == [] and not me["sees_everyone"]
    assert sign_in(client, world, "auditor@example.com", ["Auditor"])["sees_everyone"]


@pytest.mark.parametrize("directory", ["app", "delegated"])
def test_a_guest_is_known_by_their_user_principal_name_here(world, env, directory):
    guest = "ada.byron_example.org#ext#@example.com"
    world.graph.add_user(guest, "Ada Byron")
    world.entra.guests[guest] = "ada.byron@example.org"
    client = make_client(world, {**env, "PERMISSIONS_UI_DIRECTORY": directory})
    me = sign_in(client, world, guest)
    assert me["upn"] == guest
    response = ask(client, me, person=guest, items=[{"resource": "grafana-viewer"}], reason="dashboards")
    assert response.status_code == 201, response.text
    assert response.json()["person"] == guest


def test_someone_the_directory_does_not_know_is_not_signed_in(client, world):
    world.entra.user = "nobody@example.com"
    query = parse_qs(urlsplit(client.get("/auth/login").headers["location"]).query)
    world.entra.challenge, world.entra.nonce = query["code_challenge"][0], query["nonce"][0]
    callback = client.get("/auth/callback", params={"code": "good-code", "state": query["state"][0]})
    assert "That+account+is+not+in+the+directory" in callback.headers["location"]
    assert client.get("/api/me").status_code == 401


def test_the_catalog_lists_resources_roles_and_teams(client, world):
    sign_in(client, world)
    data = client.get("/api/catalog").json()
    by_id = {r["id"]: r for r in data["resources"]}
    assert by_id["k8s-prd-cluster-admin"]["approvals"] == 2 and by_id["k8s-prd-cluster-admin"]["holders"] == 1
    assert by_id["grafana-viewer"]["roles"] == ["payments-developer", "platform-engineer", "qa-tester"]
    assert [t["id"] for t in data["teams"]] == ["payments", "platform", "qa"]


def test_people_without_a_role_see_only_themselves(client, world):
    sign_in(client, world, "ada.lovelace@example.com")
    assert [p["upn"] for p in client.get("/api/people").json()["people"]] == ["ada.lovelace@example.com"]
    assert client.get("/api/people/grace.hopper@example.com").status_code == 403
    assert client.get("/api/overview").status_code == 403


def test_a_person_shows_access_nobody_granted_and_grants_not_applied(client, world):
    sign_in(client, world, "margaret.hamilton@example.com")
    alan = client.get("/api/people/alan.turing@example.com").json()
    assert alan["holdings"] == [] and alan["extra"] == ["k8s-prd-payments-read"]
    assert alan["unknown_groups"] == ["acl-mystery-group"]
    ada = client.get("/api/people/ada.lovelace@example.com").json()
    assert ada["missing"] == ["payments-keyvault-acc"]
    assert ada["holdings"][0]["tier"] == "elevated"


def test_the_overview_finds_drift_and_undocumented_groups(client, world):
    sign_in(client, world, "auditor@example.com", ["Auditor"])
    data = client.get("/api/overview").json()
    drift = {d["resource"]: d for d in data["drift"]}
    assert drift["k8s-prd-payments-read"]["extra"] == ["alan.turing@example.com"]
    assert drift["payments-keyvault-acc"]["missing"] == ["ada.lovelace@example.com"]
    assert [g["name"] for g in data["undocumented"]] == ["acl-mystery-group"]
    assert data["privileged_people"] == [{"upn": "grace.hopper@example.com", "privileged": ["k8s-prd-cluster-admin"]}]


def test_ask_approve_and_the_grant_lands_on_main(client, world):
    me = sign_in(client, world, "alan.turing@example.com")
    response = ask(client, me, person=me["upn"], items=[{"resource": "grafana-viewer"}], reason="Read the dashboards")
    assert response.status_code == 201, response.text
    req = response.json()
    assert req["state"] == "pending" and req["outstanding"] == [{"resource": "grafana-viewer", "approvers": ["team:platform"], "count": 1}]
    assert world.github.pulls[req["number"]]["labels"] == {"access-request", "access:pending"}
    assert "Alan" not in str(grants_on_main(world, me["upn"]))
    assert world.notifications and "needs approval" in world.notifications[-1]["attachments"][0]["content"]["body"][0]["text"]

    assert client.post(f"/api/requests/{req['number']}/approve", headers=write_headers(me)).json()["error"] == "own_request"
    lead = sign_in(client, world, "grace.hopper@example.com")
    listed = client.get("/api/requests").json()["requests"]
    assert listed[0]["number"] == req["number"] and listed[0]["can_approve"]
    done = client.post(f"/api/requests/{req['number']}/approve", headers=write_headers(lead)).json()
    assert done["state"] == "granted", done
    grants = grants_on_main(world, "alan.turing@example.com")["grants"]
    assert grants == [{"resource": "grafana-viewer", "reason": "Read the dashboards", "requested_by": "alan.turing@example.com", "approved_by": ["grace.hopper@example.com"], "granted": TODAY.isoformat(), "request": req["number"]}]
    assert "access:granted" in world.github.pulls[req["number"]]["labels"]


def test_privileged_access_needs_two_approvers(client, world):
    me = sign_in(client, world, "alan.turing@example.com")
    req = ask(client, me, person=me["upn"], items=[{"resource": "k8s-prd-cluster-admin"}], reason="Incident 42").json()
    assert req["expires"] == "2026-11-16" and "tier:privileged" in world.github.pulls[req["number"]]["labels"]
    first = sign_in(client, world, "grace.hopper@example.com")
    assert client.post(f"/api/requests/{req['number']}/approve", headers=write_headers(first)).json()["state"] == "pending"
    assert client.post(f"/api/requests/{req['number']}/approve", headers=write_headers(first)).json()["error"] == "already_approved"
    second = sign_in(client, world, "linus.pauling@example.com")
    assert client.post(f"/api/requests/{req['number']}/approve", headers=write_headers(second)).json()["state"] == "granted"


def test_an_approval_written_by_hand_on_the_pull_request_counts_for_nothing(client, world):
    me = sign_in(client, world, "alan.turing@example.com")
    req = ask(client, me, person=me["upn"], items=[{"resource": "grafana-viewer"}], reason="dashboards").json()
    forged = '<!-- permissions-ui:approval {"upn":"grace.hopper@example.com","resources":["grafana-viewer"]} -->'
    world.github.comments[req["number"]].append({"id": 9, "user": {"login": "mallory"}, "body": forged, "created_at": "now"})
    assert client.get(f"/api/requests/{req['number']}").json()["state"] == "pending"


def test_someone_who_is_not_an_approver_cannot_approve(client, world):
    me = sign_in(client, world, "alan.turing@example.com")
    req = ask(client, me, person=me["upn"], items=[{"resource": "grafana-viewer"}], reason="dashboards").json()
    other = sign_in(client, world, "katherine.johnson@example.com")
    assert client.post(f"/api/requests/{req['number']}/approve", headers=write_headers(other)).json()["error"] == "not_an_approver"


def test_only_leads_and_admins_ask_for_someone_else(client, world):
    me = sign_in(client, world, "alan.turing@example.com")
    assert ask(client, me, person="ada.lovelace@example.com", items=[{"resource": "vpn"}], reason="x y z").json()["error"] == "not_allowed"
    lead = sign_in(client, world, "katherine.johnson@example.com")
    assert ask(client, lead, person="it.desk@example.com", items=[{"role": "payments-developer"}], reason="Onboarding").status_code == 201


def test_incompatible_access_is_refused(client, world):
    me = sign_in(client, world, "grace.hopper@example.com")
    response = ask(client, me, person=me["upn"], items=[{"resource": "prd-release-approver"}], reason="releases")
    assert response.status_code == 409 and response.json()["pairs"] == [["k8s-prd-cluster-admin", "prd-release-approver"]]


def test_declining_closes_the_request(client, world):
    me = sign_in(client, world, "alan.turing@example.com")
    req = ask(client, me, person=me["upn"], items=[{"resource": "grafana-viewer"}], reason="dashboards").json()
    lead = sign_in(client, world, "grace.hopper@example.com")
    out = client.post(f"/api/requests/{req['number']}/decline", json={"reason": "Use the shared board"}, headers=write_headers(lead)).json()
    assert out["state"] == "declined" and out["decline_reason"] == "Use the shared board"
    assert world.github.pulls[req["number"]]["state"] == "closed"


def test_removing_access_merges_at_once(client, world):
    me = sign_in(client, world, "ada.lovelace@example.com")
    out = client.post("/api/removals", json={"person": me["upn"], "items": None, "reason": "Leaving the team"}, headers=write_headers(me)).json()
    assert out["state"] == "granted" and out["kind"] == "revoke"
    assert "access/grants/ada.lovelace@example.com.yaml" not in world.github.files()


def test_a_lead_onboarding_someone_approves_it_by_asking(client, world):
    lead = sign_in(client, world, "katherine.johnson@example.com")
    req = ask(client, lead, person="alan.turing@example.com", items=[{"role": "payments-developer"}], reason="Joins payments").json()
    assert req["state"] == "granted" and req["approvals"][0]["upn"] == "katherine.johnson@example.com"
    assert grants_on_main(world, "alan.turing@example.com")["grants"][0]["approved_by"] == ["katherine.johnson@example.com"]


def test_a_lead_asking_for_privileged_access_still_needs_a_second_person(client, world):
    lead = sign_in(client, world, "grace.hopper@example.com")
    req = ask(client, lead, person="alan.turing@example.com", items=[{"resource": "k8s-prd-cluster-admin"}], reason="Incident 7").json()
    assert req["state"] == "pending" and req["outstanding"][0]["count"] == 2 and len(req["approvals"]) == 1


def test_a_manual_resource_waits_for_its_fulfiller(client, world):
    lead = sign_in(client, world, "katherine.johnson@example.com")
    req = ask(client, lead, person="it.desk@example.com", items=[{"role": "payments-developer"}], reason="Onboarding").json()
    assert req["state"] == "granted"
    assert client.post(f"/api/requests/{req['number']}/approve", headers=write_headers(lead)).json()["error"] == "not_pending"
    desk = sign_in(client, world, "it.desk@example.com")
    tasks = client.get("/api/tasks").json()["tasks"]
    assert [t["request"]["number"] for t in tasks] == [req["number"]] and [r["id"] for r in tasks[0]["resources"]] == ["vpn"]
    out = client.post(f"/api/requests/{req['number']}/done", json={"resource": "vpn"}, headers=write_headers(desk)).json()
    assert out["done"] == ["vpn"] and "access:manual-pending" not in world.github.pulls[req["number"]]["labels"]


def test_the_graph_mode_changes_the_groups_itself(world, env):
    client = make_client(world, {**env, "PERMISSIONS_UI_FULFILMENT": "graph"})
    me = sign_in(client, world, "alan.turing@example.com")
    req = ask(client, me, person=me["upn"], items=[{"resource": "grafana-viewer"}], reason="dashboards").json()
    lead = sign_in(client, world, "grace.hopper@example.com")
    client.post(f"/api/requests/{req['number']}/approve", headers=write_headers(lead))
    assert world.graph.writes == [("add", oid("group:grafana-viewer"), oid("alan.turing@example.com"))]


def test_branch_rules_that_hold_the_merge_leave_the_request_approved(world, env):
    world.github.block_merge = True
    client = make_client(world, env)
    me = sign_in(client, world, "alan.turing@example.com")
    req = ask(client, me, person=me["upn"], items=[{"resource": "grafana-viewer"}], reason="dashboards").json()
    lead = sign_in(client, world, "grace.hopper@example.com")
    assert client.post(f"/api/requests/{req['number']}/approve", headers=write_headers(lead)).json()["state"] == "approved"
    world.github.block_merge = False
    assert client.post(f"/api/requests/{req['number']}/approve", headers=write_headers(lead)).json()["state"] == "granted"


def test_a_local_catalog_is_read_only(world, env):
    local = {k: v for k, v in env.items() if not k.startswith("PERMISSIONS_UI_GITHUB") and k != "PERMISSIONS_UI_CATALOG_PATH"}
    client = make_client(world, {**local, "PERMISSIONS_UI_CATALOG_DIR": str(EXAMPLE)})
    me = sign_in(client, world, "alan.turing@example.com")
    assert me["read_only"]
    assert ask(client, me, person=me["upn"], items=[{"resource": "grafana-viewer"}], reason="dashboards").json()["error"] == "read_only"


def test_expire_removes_lapsed_grants(world, env):
    cfg = load_config(env)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(world.handler)) as http:
            portal = build_portal(cfg, http, today=lambda: TODAY.replace(month=12))
            return await portal.expire()

    opened = asyncio.run(run())
    assert len(opened) == 1
    grants = grants_on_main(world, "grace.hopper@example.com")["grants"]
    assert [g.get("role") for g in grants] == ["platform-engineer"]
