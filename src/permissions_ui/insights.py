"""What the catalog says against what the directory holds, and what stands out.

Drift has two directions. Missing: the catalog grants it, the group lacks the person (the
pipeline has not applied it yet, or failed). Extra: the person is in the group without a grant
(added by hand, or left behind) — access nobody approved, which is the part worth a look.
Undocumented groups match the operator's prefixes but are in no resource: nobody can say what
they are for.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta

from .catalog import Catalog
from .directory import Directory, Group
from .policy import conflicts


@dataclass(frozen=True)
class Drift:
    resource: str
    group_found: bool
    missing: tuple[str, ...] = ()
    extra: tuple[str, ...] = ()
    members: int = 0


@dataclass(frozen=True)
class PersonView:
    upn: str
    tiers: dict[str, int]
    privileged: tuple[str, ...]
    expiring: tuple[tuple[str, str], ...]
    conflicts: tuple[tuple[str, str], ...]
    extra_groups: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    unknown_groups: tuple[str, ...] = field(default_factory=tuple)


async def resource_drift(catalog: Catalog, directory: Directory, resource_id: str, today: date) -> Drift | None:
    resource = catalog.resources[resource_id]
    if resource.kind != "entra-group":
        return None
    group = await directory.group(resource.target)
    if group is None:
        return Drift(resource_id, group_found=False, missing=tuple(sorted(catalog.holders(resource_id, today))))
    actual = {u.upn for u in await directory.members(group.id)}
    granted = set(catalog.holders(resource_id, today))
    return Drift(resource_id, True, tuple(sorted(granted - actual)), tuple(sorted(actual - granted)), len(actual))


async def all_drift(catalog: Catalog, directory: Directory, today: date) -> list[Drift]:
    found = await asyncio.gather(*(resource_drift(catalog, directory, rid, today) for rid in catalog.resources))
    return [d for d in found if d is not None]


async def undocumented(catalog: Catalog, directory: Directory, prefixes: tuple[str, ...]) -> list[Group]:
    if not prefixes:
        return []
    known_names = {r.target.lower() for r in catalog.resources.values() if r.kind == "entra-group"}
    groups: dict[str, Group] = {}
    for prefix in prefixes:
        for group in await directory.groups(prefix):
            groups[group.id] = group
    return sorted((g for g in groups.values() if g.name.lower() not in known_names and g.id not in known_names), key=lambda g: g.name.lower())


def person_view(catalog: Catalog, upn: str, today: date, soon: int = 30) -> PersonView:
    holdings = catalog.holdings(upn, today)
    tiers = Counter(h.resource.tier for h in holdings.values())
    expiring = tuple(
        sorted((rid, h.expires.isoformat()) for rid, h in holdings.items() if h.expires is not None and h.expires <= today + timedelta(days=soon))
    )
    return PersonView(
        upn=upn,
        tiers={t: tiers.get(t, 0) for t in ("standard", "elevated", "privileged")},
        privileged=tuple(sorted(rid for rid, h in holdings.items() if h.resource.tier == "privileged")),
        expiring=expiring,
        conflicts=tuple(conflicts(catalog, holdings, [])),
    )


async def person_actual(catalog: Catalog, directory: Directory, view: PersonView, user_id: str, today: date, prefixes: tuple[str, ...]) -> PersonView:
    """The person's view with what the directory says: groups held without a grant, grants not applied."""
    groups = await directory.member_of(user_id)
    names = {g.name.lower() for g in groups} | {g.id for g in groups}
    by_target = catalog.by_target("entra-group")
    holdings = catalog.holdings(view.upn, today)
    extra = sorted(r.id for target, r in by_target.items() if target in names and r.id not in holdings)
    missing = sorted(rid for rid, h in holdings.items() if h.resource.kind == "entra-group" and h.resource.target.lower() not in names)
    unknown = sorted(g.name for g in groups if prefixes and g.name.lower().startswith(prefixes) and g.name.lower() not in by_target and g.id not in by_target)
    return PersonView(**{**view.__dict__, "extra_groups": tuple(extra), "missing": tuple(missing), "unknown_groups": tuple(unknown)})
