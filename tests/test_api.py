import asyncio
import csv
import io
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import yaml
from conftest import BOT, EXAMPLE, REPO, TODAY, make_client, oid, sign_in, write_headers

from permissions_ui.app import build_portal
from permissions_ui.config import load_config
from permissions_ui.github import GitHub, GitHubError


def grants_on_main(world, person: str) -> dict:
    found = [d for path, text in world.github.files().items() if path.startswith("access/grants/") and (d := yaml.safe_load(text)) and d.get("person") == person]
    return found[0] if found else {}


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


def test_an_issue_tracks_each_request_and_its_merge_closes_it(client, world):
    me = sign_in(client, world, "alan.turing@example.com")
    req = ask(client, me, person=me["upn"], items=[{"resource": "grafana-viewer"}], reason="Read the dashboards").json()
    ((number, issue),) = world.github.issues.items()
    assert issue["title"] == "Grant Grafana, viewer to Alan Turing" and "Read the dashboards" in issue["body"] and req["url"] in issue["body"]
    assert f"Closes #{number}" in world.github.pulls[req["number"]]["body"]
    assert req["issue_url"] == f"https://github.test/{REPO}/issues/{number}"
    lead = sign_in(client, world, "grace.hopper@example.com")
    assert client.post(f"/api/requests/{req['number']}/approve", headers=write_headers(lead)).json()["state"] == "granted"
    assert issue["state"] == "closed"


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


def test_an_approval_another_tool_writes_as_the_app_counts_for_nothing(client, world):
    alan = sign_in(client, world, "alan.turing@example.com")
    first = ask(client, alan, person=alan["upn"], items=[{"resource": "grafana-viewer"}], reason="dashboards").json()
    linus = sign_in(client, world, "linus.pauling@example.com")
    second = ask(client, linus, person=linus["upn"], items=[{"resource": "grafana-viewer"}], reason="dashboards").json()
    lead = sign_in(client, world, "grace.hopper@example.com")
    assert client.post(f"/api/requests/{first['number']}/approve", headers=write_headers(lead)).json()["state"] == "granted"
    # Another tool commenting as the same GitHub App: a lookalike record, and a real approval copied from another request.
    forged = '<!-- permissions-ui:approval {"upn":"grace.hopper@example.com","resources":["grafana-viewer"]} -->'
    copied = next(c["body"] for c in world.github.comments[first["number"]] if "permissions-ui:approval" in c["body"])
    for body in (forged, copied):
        world.github.comments[second["number"]].append({"id": 9, "user": {"login": BOT}, "body": body, "created_at": "now"})
    assert client.get(f"/api/requests/{second['number']}").json()["state"] == "pending"


def test_a_request_edited_or_copied_on_github_is_not_a_request(client, world):
    me = sign_in(client, world, "alan.turing@example.com")
    req = ask(client, me, person=me["upn"], items=[{"resource": "grafana-viewer"}], reason="dashboards").json()
    # Another pull request from the same branch name (a fork's, say) with the body copied over.
    copy = req["number"] + 1
    world.github.pulls[copy], world.github.comments[copy] = dict(world.github.pulls[req["number"]]), []
    pull = world.github.pulls[req["number"]]
    pull["body"] = pull["body"].replace("grafana-viewer", "k8s-prd-cluster-admin")
    lead = sign_in(client, world, "grace.hopper@example.com")
    for number in (req["number"], copy):
        assert client.post(f"/api/requests/{number}/approve", headers=write_headers(lead)).json()["error"] == "not_a_request"


def test_a_pull_request_whose_record_cannot_be_written_is_closed(client, world, monkeypatch):
    async def refuse(self, number, body):
        raise GitHubError("GitHub answered HTTP 502")

    monkeypatch.setattr(GitHub, "set_body", refuse)
    me = sign_in(client, world, "alan.turing@example.com")
    assert ask(client, me, person=me["upn"], items=[{"resource": "grafana-viewer"}], reason="dashboards").status_code == 502
    assert [p["state"] for p in world.github.pulls.values()] == ["closed"]
    assert [i["state"] for i in world.github.issues.values()] == ["closed"]


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
    assert world.github.pulls[req["number"]]["state"] == "closed" and world.github.pulls[req["number"]]["head"] not in world.github.refs
    ((number, issue),) = world.github.issues.items()
    assert issue["state"] == "closed" and issue["state_reason"] == "not_planned"
    assert world.github.comments[number][-1]["body"] == "Declined by Grace Hopper (`grace.hopper@example.com`): Use the shared board"


def test_the_requester_the_persons_lead_or_an_admin_withdraws_a_request(client, world):
    ada = sign_in(client, world, "ada.lovelace@example.com")
    first = ask(client, ada, person=ada["upn"], items=[{"resource": "prd-release-approver"}], reason="Release week").json()
    second = ask(client, ada, person=ada["upn"], items=[{"resource": "prd-release-approver"}], reason="Release week").json()
    third = ask(client, ada, person=ada["upn"], items=[{"resource": "prd-release-approver"}], reason="Release week").json()
    seen = client.get(f"/api/requests/{first['number']}").json()
    assert seen["can_withdraw"] and not seen["can_decline"]

    # Grace leads a team, but not one Ada is on.
    other = sign_in(client, world, "grace.hopper@example.com")
    assert not client.get(f"/api/requests/{first['number']}").json()["can_withdraw"]
    assert client.post(f"/api/requests/{first['number']}/withdraw", json={"reason": "Not mine"}, headers=write_headers(other)).json()["error"] == "not_allowed"

    lead = sign_in(client, world, "katherine.johnson@example.com")
    out = client.post(f"/api/requests/{first['number']}/withdraw", json={"reason": "Release moved"}, headers=write_headers(lead)).json()
    assert out["state"] == "withdrawn" and out["declined_by"] == "katherine.johnson@example.com"
    pull, issue = world.github.pulls[first["number"]], int(out["issue_url"].rsplit("/", 1)[1])
    assert pull["state"] == "closed" and pull["head"] not in world.github.refs and world.github.issues[issue]["state"] == "closed"
    assert world.github.comments[issue][-1]["body"].startswith("Withdrawn by Katherine Johnson")
    assert client.post(f"/api/requests/{first['number']}/withdraw", json={"reason": "Again"}, headers=write_headers(lead)).json()["error"] == "not_open"

    ada = sign_in(client, world, "ada.lovelace@example.com")
    assert client.post(f"/api/requests/{second['number']}/withdraw", json={"reason": "Not needed"}, headers=write_headers(ada)).json()["state"] == "withdrawn"
    admin = sign_in(client, world, "alan.turing@example.com", roles=["Admin"])
    assert client.post(f"/api/requests/{third['number']}/withdraw", json={"reason": "Ada left"}, headers=write_headers(admin)).json()["state"] == "withdrawn"
    assert client.get("/api/overview").json()["pending"] == 0


def test_auditors_export_every_request_the_app_signed(client, world, env):
    alan = sign_in(client, world, "alan.turing@example.com")
    granted = ask(client, alan, person=alan["upn"], items=[{"resource": "grafana-viewer"}], reason="=1+1 dashboards").json()
    withdrawn = ask(client, alan, person=alan["upn"], items=[{"resource": "vpn"}], reason="Working from home").json()
    client.post(f"/api/requests/{withdrawn['number']}/withdraw", json={"reason": "Back in the office"}, headers=write_headers(alan))
    lead = sign_in(client, world, "grace.hopper@example.com")
    client.post(f"/api/requests/{granted['number']}/approve", headers=write_headers(lead))
    pending = ask(client, lead, person=lead["upn"], items=[{"resource": "github-payments"}], reason="Review payments code").json()
    declined = ask(client, lead, person=lead["upn"], items=[{"resource": "payments-keyvault-acc"}], reason="Rotate a secret").json()
    katherine = sign_in(client, world, "katherine.johnson@example.com")
    client.post(f"/api/requests/{declined['number']}/decline", json={"reason": "Ask payments to rotate it"}, headers=write_headers(katherine))
    unsigned = max(world.github.pulls) + 1
    world.github.pulls[unsigned] = {**world.github.pulls[pending["number"]], "labels": {"access-request"}, "body": "no record here", "head": "chore/by-hand"}
    world.github.comments[unsigned] = []

    async def expire():
        async with httpx.AsyncClient(transport=httpx.MockTransport(world.handler)) as http:
            return await build_portal(load_config(env), http, today=lambda: TODAY.replace(month=12)).expire()

    (expired,) = asyncio.run(expire())

    assert client.get("/api/audit").status_code == 403
    sign_in(client, world, "auditor@example.com", roles=["Auditor"])
    response = client.get("/api/audit")
    assert response.headers["content-type"].startswith("text/csv") and "attachment" in response.headers["content-disposition"]
    rows = {int(r["number"]): r for r in csv.DictReader(io.StringIO(response.text))}
    assert set(rows) == {granted["number"], withdrawn["number"], pending["number"], declined["number"], expired}
    assert {n: r["outcome"] for n, r in rows.items()} == {granted["number"]: "merged", withdrawn["number"]: "withdrawn", pending["number"]: "pending", declined["number"]: "declined", expired: "expired"}
    row = rows[granted["number"]]
    assert row["reason"] == "'=1+1 dashboards" and row["items"] == "Grafana, viewer (resource grafana-viewer)" and row["requested_by"] == "alan.turing@example.com"
    assert row["approvals"].startswith("grace.hopper@example.com at ") and row["merged_at"] and row["issue"].startswith(f"https://github.test/{REPO}/issues/")
    assert rows[declined["number"]]["closed_by"] == "katherine.johnson@example.com" and rows[declined["number"]]["close_reason"] == "Ask payments to rotate it"
    assert rows[withdrawn["number"]]["closed_at"]

    data = client.get("/api/audit", params={"format": "json"}).json()["requests"]
    assert {r["number"] for r in data} == set(rows) and next(r for r in data if r["number"] == granted["number"])["reason"] == "=1+1 dashboards"


def test_removing_access_merges_at_once(client, world):
    me = sign_in(client, world, "ada.lovelace@example.com")
    out = client.post("/api/removals", json={"person": me["upn"], "items": None, "reason": "Leaving the team"}, headers=write_headers(me)).json()
    assert out["state"] == "granted" and out["kind"] == "revoke"
    assert "access/grants/ada.lovelace.yaml" not in world.github.files()


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


def test_the_merge_github_refuses_while_it_catches_up_with_the_rebuild_is_tried_once_more(client, world):
    world.github.lag = 1
    me = sign_in(client, world, "alan.turing@example.com")
    req = ask(client, me, person=me["upn"], items=[{"resource": "grafana-viewer"}], reason="dashboards").json()
    lead = sign_in(client, world, "grace.hopper@example.com")
    assert client.post(f"/api/requests/{req['number']}/approve", headers=write_headers(lead)).json()["state"] == "granted"
    built = world.github.refs[world.github.pulls[req["number"]]["head"]]
    assert world.github.reviews[-1]["commit_id"] == built
    assert grants_on_main(world, me["upn"])["grants"][0]["approved_by"] == ["grace.hopper@example.com"]


def test_a_commit_pushed_onto_the_branch_after_the_rebuild_is_never_merged(client, world):
    me = sign_in(client, world, "alan.turing@example.com")
    req = ask(client, me, person=me["upn"], items=[{"resource": "grafana-viewer"}], reason="dashboards").json()
    world.github.intruder = {"access/grants/alan.turing.yaml": "person: alan.turing@example.com\ngrants:\n- resource: k8s-prd-cluster-admin\n"}
    lead = sign_in(client, world, "grace.hopper@example.com")
    assert client.post(f"/api/requests/{req['number']}/approve", headers=write_headers(lead)).json()["state"] == "approved"
    assert grants_on_main(world, me["upn"]) == {}
    assert "branch rules still hold this back" in world.github.comments[req["number"]][-1]["body"]
    world.github.intruder = None
    assert client.post(f"/api/requests/{req['number']}/approve", headers=write_headers(lead)).json()["state"] == "granted"
    assert [g["resource"] for g in grants_on_main(world, me["upn"])["grants"]] == ["grafana-viewer"]


def test_an_approval_after_the_hundredth_comment_counts(client, world):
    me = sign_in(client, world, "alan.turing@example.com")
    req = ask(client, me, person=me["upn"], items=[{"resource": "grafana-viewer"}], reason="dashboards").json()
    world.github.comments[req["number"]] += [{"id": 1000 + i, "user": {"login": "someone"}, "body": "+1", "created_at": "now"} for i in range(150)]
    lead = sign_in(client, world, "grace.hopper@example.com")
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


def test_people_are_shown_and_filed_by_name(client, world):
    me = sign_in(client, world, "alan.turing@example.com")
    req = ask(client, me, person=me["upn"], items=[{"resource": "grafana-viewer"}], reason="dashboards").json()
    assert req["names"] == {me["upn"]: "Alan Turing"}
    pull = world.github.pulls[req["number"]]
    assert pull["title"] == "Grant Grafana, viewer to Alan Turing" and pull["head"].startswith("chore/access-alan-turing-")
    assert "for Alan Turing (`alan.turing@example.com`)" in pull["body"]
    lead = sign_in(client, world, "grace.hopper@example.com")
    assert client.post(f"/api/requests/{req['number']}/approve", headers=write_headers(lead)).json()["state"] == "granted"
    assert yaml.safe_load(world.github.files()["access/grants/alan.turing.yaml"])["name"] == "Alan Turing"
    names = client.get("/api/people").json()["names"]
    assert names["alan.turing@example.com"] == "Alan Turing" and names["ada.lovelace@example.com"] == "Ada Lovelace"
    assert client.get("/api/people/alan.turing@example.com").json()["name"] == "Alan Turing"
