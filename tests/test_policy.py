import asyncio
from datetime import date

from conftest import EXAMPLE

from permissions_ui.catalog import load_catalog
from permissions_ui.policy import conflicts, eligible_needs, latest_expiry, needs, outstanding

CATALOG = load_catalog({str(p.relative_to(EXAMPLE)): p.read_text() for p in EXAMPLE.rglob("*.yaml")})


def test_a_roles_approvers_stand_for_each_of_its_resources():
    found = {n.resource: n for n in needs(CATALOG, [{"role": "payments-developer"}])}
    assert found["payments-keyvault-acc"].approvers == ("team:payments",)
    assert found["vpn"].count == 1


def test_a_resource_falls_back_to_its_owners_leads_and_privileged_needs_two():
    (need,) = needs(CATALOG, [{"resource": "k8s-prd-cluster-admin"}])
    assert need.approvers == ("team:platform",) and need.count == 2


def test_admin_access_needs_two_approvals_whatever_its_tier_says():
    files = {str(p.relative_to(EXAMPLE)): p.read_text() for p in EXAMPLE.rglob("*.yaml")}
    files["resources/grafana.yaml"] = "- {id: grafana-editor, name: Grafana editor, description: Edits dashboards., owner: platform, kind: entra-group, target: Grafana-Admins, approvals: 1}\n"
    files["roles/dashboards.yaml"] = "- {id: dashboards, name: Dashboard ADMIN, description: Grafana for its admins., resources: [grafana-viewer]}\n"
    catalog = load_catalog(files)
    assert [n.count for n in needs(catalog, [{"resource": "grafana-editor"}])] == [2]
    assert [n.count for n in needs(catalog, [{"role": "dashboards"}])] == [2]
    assert [n.count for n in needs(catalog, [{"resource": "grafana-viewer"}])] == [1]


def test_the_same_resource_twice_takes_the_stricter_need():
    found = {n.resource: n for n in needs(CATALOG, [{"role": "payments-developer"}, {"resource": "payments-keyvault-acc"}])}
    assert found["payments-keyvault-acc"].approvers == ("team:payments", "team:platform")


def test_an_approval_counts_for_every_resource_the_approver_may_approve():
    need_list = needs(CATALOG, [{"role": "payments-developer"}])
    mine = asyncio.run(eligible_needs(CATALOG, None, need_list, "katherine.johnson@example.com"))
    assert {n.resource for n in mine} == {n.resource for n in need_list}
    assert asyncio.run(eligible_needs(CATALOG, None, need_list, "grace.hopper@example.com")) == []
    assert outstanding(need_list, {n.resource: {"katherine.johnson@example.com"} for n in need_list}) == []


def test_incompatible_access_is_found_in_either_order():
    assert conflicts(CATALOG, ["prd-release-approver"], ["k8s-prd-cluster-admin"]) == [("k8s-prd-cluster-admin", "prd-release-approver")]
    assert conflicts(CATALOG, ["grafana-viewer"], ["k8s-prd-cluster-admin"]) == []


def test_the_shortest_limit_bounds_the_expiry():
    today = date(2026, 10, 17)
    assert latest_expiry(CATALOG, [{"resource": "k8s-prd-cluster-admin"}], today) == date(2026, 11, 16)
    assert latest_expiry(CATALOG, [{"role": "payments-developer"}], today) == date(2027, 10, 17)
    assert latest_expiry(CATALOG, [{"resource": "grafana-viewer"}], today) is None
