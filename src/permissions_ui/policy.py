"""Who has to approve a request, who may, and what a request may not do.

Each resource in a request needs its own approvals: its role's approvers when it comes in a
role that names some, else the resource's, else the leads of the team that owns it. A
privileged resource needs two approvals unless the catalog says otherwise, and so does any
resource or role named for admin access, whatever the catalog says. Nobody approves a
request they made or one that gives them access, and an approval by someone who could approve
several of the resources counts for each of them.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta

from .catalog import DEFAULT_APPROVALS, Catalog, Resource, admin_named
from .directory import Directory


@dataclass(frozen=True)
class Need:
    """One resource's approval requirement: `count` approvals from people matching `approvers`."""

    resource: str
    approvers: tuple[str, ...]
    count: int


def needs(catalog: Catalog, items: Iterable[Mapping[str, str]]) -> list[Need]:
    found: dict[str, Need] = {}
    for item in items:
        role = catalog.roles.get(item.get("role", "")) if item.get("role") else None
        ids = role.resources if role else [item["resource"]]
        for rid in ids:
            resource = catalog.resources[rid]
            specs = tuple(role.approvers) if role and role.approvers else tuple(resource.approvers) or (f"team:{resource.owner}",)
            count = resource.required_approvals
            if role and admin_named(role.id, role.name):
                count = max(count, DEFAULT_APPROVALS["privileged"])
            need = Need(rid, specs, count)
            # The same resource through a role and directly: the stricter count, both lists.
            if rid in found:
                old = found[rid]
                need = Need(rid, tuple(dict.fromkeys(old.approvers + need.approvers)), max(old.count, need.count))
            found[rid] = need
    return list(found.values())


async def members_of(spec: str, catalog: Catalog, directory: Directory | None) -> set[str]:
    """The user principal names an approver spec stands for, lowercased."""
    kind, _, value = spec.partition(":")
    if kind == "user":
        return {value.lower()}
    if kind == "team":
        team = catalog.teams.get(value)
        return set(team.leads) if team else set()
    if kind == "group" and directory is not None:
        group = await directory.group(value)
        return {u.upn for u in await directory.members(group.id)} if group else set()
    return set()


async def eligible_needs(catalog: Catalog, directory: Directory | None, need_list: Iterable[Need], upn: str) -> list[Need]:
    """The needs this person's approval would count for."""
    upn = upn.lower()
    out = []
    cache: dict[str, set[str]] = {}
    for need in need_list:
        for spec in need.approvers:
            if spec not in cache:
                cache[spec] = await members_of(spec, catalog, directory)
            if upn in cache[spec]:
                out.append(need)
                break
    return out


def outstanding(need_list: Iterable[Need], approvals: Mapping[str, set[str]]) -> list[Need]:
    """Needs not yet met. `approvals` is {resource id: the approvers counted for it}."""
    return [n for n in need_list if len(approvals.get(n.resource, set())) < n.count]


def conflicts(catalog: Catalog, held: Iterable[str], wanted: Iterable[str]) -> list[tuple[str, str]]:
    """Pairs of resources one person may not hold together, among what they hold and want."""
    total = set(held) | set(wanted)
    pairs: set[tuple[str, str]] = set()
    for rid in total:
        for other in catalog.resources[rid].incompatible:
            if other in total:
                pairs.add(tuple(sorted((rid, other))))  # type: ignore[arg-type]
    return sorted(pairs)


def latest_expiry(catalog: Catalog, items: Iterable[Mapping[str, str]], today: date) -> date | None:
    """The furthest a grant of these items may run: the shortest max_days among them."""
    limits: list[int] = []
    for item in items:
        if item.get("role"):
            role = catalog.roles[item["role"]]
            limits += [d for d in [role.max_days] if d]
            limits += [d for d in (catalog.resources[r].max_days for r in role.resources) if d]
        else:
            limits += [d for d in [catalog.resources[item["resource"]].max_days] if d]
    return today + timedelta(days=min(limits)) if limits else None


def tier_rank(resource: Resource) -> int:
    return ("standard", "elevated", "privileged").index(resource.tier)
