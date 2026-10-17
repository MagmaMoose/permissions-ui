from datetime import date

import pytest
from conftest import EXAMPLE

from permissions_ui.catalog import CatalogError, PersonGrants, dump_person, load_catalog, person_file


def files(**extra: str) -> dict[str, str]:
    found = {str(p.relative_to(EXAMPLE)): p.read_text() for p in EXAMPLE.rglob("*.yaml")}
    return {**found, **extra}


def test_the_example_catalog_holds_together():
    catalog = load_catalog(files())
    assert set(catalog.teams) == {"platform", "payments", "qa"}
    assert catalog.resources["k8s-prd-cluster-admin"].required_approvals == 2
    assert catalog.resources["grafana-viewer"].required_approvals == 1


def test_a_role_grant_reaches_every_resource_of_the_role():
    catalog = load_catalog(files())
    held = catalog.holdings("ada.lovelace@example.com", date(2026, 10, 17))
    assert set(held) == set(catalog.roles["payments-developer"].resources)
    assert held["vpn"].expires == date(2027, 8, 31)


def test_an_expired_grant_gives_nothing():
    catalog = load_catalog(files())
    held = catalog.holdings("grace.hopper@example.com", date(2026, 11, 5))
    assert "k8s-prd-cluster-admin" not in held and "grafana-viewer" in held
    assert set(catalog.holders("k8s-prd-cluster-admin", date(2026, 10, 17))) == {"grace.hopper@example.com"}


def test_every_problem_is_reported_at_once():
    broken = files(**{
        "roles/broken.yaml": "- id: ghost\n  name: Ghost\n  description: x\n  resources: [nope]\n",
        "teams/dup.yaml": "- id: qa\n  name: QA again\n  leads: [x@example.com]\n",
        "grants/someone@example.com.yaml": "person: other@example.com\ngrants: []\n",
        "resources/manual.yaml": "- id: thing\n  name: Thing\n  description: x\n  owner: qa\n  kind: manual\n  target: Thing\n",
    })
    with pytest.raises(CatalogError) as err:
        load_catalog(broken)
    text = " | ".join(err.value.problems)
    assert "role ghost: resource nope is not a resource" in text
    assert "team qa is defined twice" in text
    assert "belong in grants/other@example.com.yaml" in text
    assert "a manual resource needs fulfillers" in text


def test_a_grant_names_a_role_or_a_resource_not_both():
    bad = "person: ada.lovelace@example.com\ngrants:\n- role: qa-tester\n  resource: vpn\n  reason: x\n  granted: '2026-01-01'\n"
    with pytest.raises(CatalogError) as err:
        load_catalog(files(**{"grants/ada.lovelace@example.com.yaml": bad}))
    assert "either a resource or a role" in str(err.value)


def test_person_files_are_one_per_person_and_stable():
    assert person_file("Ada.Lovelace@Example.com") == "grants/ada.lovelace@example.com.yaml"
    catalog = load_catalog(files())
    text = dump_person(catalog.people["grace.hopper@example.com"])
    assert text.index("resource: k8s-prd-cluster-admin") < text.index("role: platform-engineer")
    reloaded = load_catalog(files(**{"grants/grace.hopper@example.com.yaml": text})).people["grace.hopper@example.com"]
    assert sorted(reloaded.grants, key=lambda g: g.key) == sorted(catalog.people["grace.hopper@example.com"].grants, key=lambda g: g.key)
    assert dump_person(PersonGrants(person="x@example.com")) == "person: x@example.com\ngrants: []\n"
