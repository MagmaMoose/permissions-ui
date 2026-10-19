"""The catalog: what can be granted, in which bundles, to whom, and who approves. YAML in Git.

    resources/*.yaml   one thing access is granted to: an Entra group, a GitHub team, or a
                       system someone sets up by hand
    roles/*.yaml       a named bundle of resources ("Backend developer")
    teams/*.yaml       a team's leads and the roles a new member gets
    grants/<name>.yaml one file per person, named for them: what they hold, why, who approved
                       it, until when

The Git history of these files is the record of who had what and when. Everything here is
plain data with no I/O: the loader takes {path: text}, so the same code reads a local
directory and a repository fetched over the GitHub API.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

ID = r"^[a-z0-9][a-z0-9-]{0,62}$"
_UPN = re.compile(r"^[^@\s/\\]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_APPROVER = re.compile(r"^(team|group|user):\S+$")
TIERS = ("standard", "elevated", "privileged")
DEFAULT_APPROVALS = {"standard": 1, "elevated": 1, "privileged": 2}


def admin_named(*names: str) -> bool:
    """Admin access, by its name, needs as many approvals as privileged access whatever its tier."""
    return any("admin" in name.lower() for name in names)


class CatalogError(ValueError):
    """The catalog does not hold together; `problems` lists every reason, with its file."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _approvers(values: list[str]) -> list[str]:
    for value in values:
        if not _APPROVER.match(value):
            raise ValueError(f"{value!r} is not team:<id>, group:<entra group> or user:<upn>")
    return values


class Resource(_Model):
    id: str = Field(pattern=ID)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=2000)
    owner: str = Field(pattern=ID)
    kind: Literal["entra-group", "github-team", "manual"]
    # entra-group: the group's display name or object id; github-team: org/team-slug;
    # manual: the system, as people call it.
    target: str = Field(min_length=1, max_length=256)
    tier: Literal["standard", "elevated", "privileged"] = "standard"
    approvers: list[str] = Field(default_factory=list)
    approvals: int | None = Field(default=None, ge=1, le=5)
    max_days: int | None = Field(default=None, ge=1, le=3650)
    incompatible: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    # manual only: what the fulfiller does, and who they are.
    instructions: str = Field(default="", max_length=4000)
    fulfillers: list[str] = Field(default_factory=list)

    @field_validator("approvers", "fulfillers")
    @classmethod
    def _check_approvers(cls, values: list[str]) -> list[str]:
        return _approvers(values)

    @model_validator(mode="after")
    def _manual(self) -> Resource:
        if self.kind == "manual" and not self.fulfillers:
            raise ValueError("a manual resource needs fulfillers: who sets it up")
        if self.kind != "manual" and (self.instructions or self.fulfillers):
            raise ValueError("instructions and fulfillers are for manual resources only")
        if self.kind == "github-team" and not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+", self.target):
            raise ValueError("a github-team target is org/team-slug")
        return self

    @property
    def required_approvals(self) -> int:
        count = self.approvals or DEFAULT_APPROVALS[self.tier]
        return max(count, DEFAULT_APPROVALS["privileged"]) if admin_named(self.id, self.name, self.target) else count


class Role(_Model):
    id: str = Field(pattern=ID)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=2000)
    resources: list[str] = Field(min_length=1)
    approvers: list[str] = Field(default_factory=list)
    max_days: int | None = Field(default=None, ge=1, le=3650)

    @field_validator("approvers")
    @classmethod
    def _check_approvers(cls, values: list[str]) -> list[str]:
        return _approvers(values)


class Team(_Model):
    id: str = Field(pattern=ID)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    leads: list[str] = Field(min_length=1)
    roles: list[str] = Field(default_factory=list)

    @field_validator("leads")
    @classmethod
    def _upns(cls, values: list[str]) -> list[str]:
        return [_upn(v) for v in values]


class Grant(_Model):
    resource: str | None = Field(default=None, pattern=ID)
    role: str | None = Field(default=None, pattern=ID)
    reason: str = Field(min_length=1, max_length=500)
    requested_by: str = ""
    approved_by: list[str] = Field(default_factory=list)
    granted: date
    expires: date | None = None
    request: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _one(self) -> Grant:
        if (self.resource is None) == (self.role is None):
            raise ValueError("a grant names either a resource or a role")
        if self.expires is not None and self.expires < self.granted:
            raise ValueError("a grant cannot expire before it was granted")
        return self

    @property
    def key(self) -> str:
        return f"role:{self.role}" if self.role else f"resource:{self.resource}"

    def active(self, today: date) -> bool:
        return self.expires is None or self.expires >= today


class PersonGrants(_Model):
    person: str
    name: str = ""
    grants: list[Grant] = Field(default_factory=list)

    @field_validator("person")
    @classmethod
    def _check_person(cls, value: str) -> str:
        return _upn(value)


def _upn(value: str) -> str:
    value = str(value).strip().lower()
    if not _UPN.match(value):
        raise ValueError(f"{value!r} is not a user principal name such as jane.doe@example.com")
    return value


def short_name(upn: str) -> str:
    """The person part of a UPN: jane.doe for jane.doe@example.com, and for a guest
    (jane.doe_home.example#ext#@tenant.example) the name their home tenant gives them."""
    local = str(upn).strip().lower().split("@")[0]
    if "#ext#" in local:
        local = local.split("#ext#")[0].rsplit("_", 1)[0]
    return re.sub(r"[^a-z0-9._-]", "_", local) or "person"


def person_files(upn: str) -> tuple[str, str]:
    """Where a person's grants may live, one file each: their short name, or their whole UPN
    when someone else has the short name."""
    return f"grants/{short_name(upn)}.yaml", f"grants/{re.sub(r'[^a-z0-9._@-]', '_', _upn(upn))}.yaml"


@dataclass(frozen=True)
class Holding:
    """One resource a person holds, and every grant that gives it to them."""

    resource: Resource
    via: tuple[Grant, ...]

    @property
    def expires(self) -> date | None:
        dates = [g.expires for g in self.via]
        return None if any(d is None for d in dates) else max(d for d in dates if d is not None)


@dataclass(frozen=True)
class Catalog:
    resources: dict[str, Resource]
    roles: dict[str, Role]
    teams: dict[str, Team]
    people: dict[str, PersonGrants]
    files: dict[str, str] = field(default_factory=dict)

    def file_for(self, upn: str) -> str:
        """The person's grants file: the one they have, else their short name unless it is taken."""
        upn = _upn(upn)
        if upn in self.files:
            return self.files[upn]
        short, full = person_files(upn)
        return full if short in {p.replace(".yml", ".yaml") for p in self.files.values()} else short

    def holdings(self, upn: str, today: date) -> dict[str, Holding]:
        """What a person holds today, by resource id, through roles and direct grants alike."""
        found: dict[str, list[Grant]] = {}
        person = self.people.get(_upn(upn))
        for grant in person.grants if person else []:
            if not grant.active(today):
                continue
            ids = self.roles[grant.role].resources if grant.role else [str(grant.resource)]
            for rid in ids:
                found.setdefault(rid, []).append(grant)
        return {rid: Holding(self.resources[rid], tuple(grants)) for rid, grants in found.items()}

    def holders(self, resource_id: str, today: date) -> dict[str, Holding]:
        """Everyone holding a resource today, by person."""
        return {upn: h[resource_id] for upn in self.people if resource_id in (h := self.holdings(upn, today))}

    def resources_of(self, items: Iterable[Grant | Mapping[str, str]]) -> list[str]:
        """The resource ids a set of grants or {role|resource: id} items reach, in order, once each."""
        out: list[str] = []
        for item in items:
            role = item.role if isinstance(item, Grant) else item.get("role")
            resource = item.resource if isinstance(item, Grant) else item.get("resource")
            for rid in self.roles[role].resources if role else [str(resource)]:
                if rid not in out:
                    out.append(rid)
        return out

    def by_target(self, kind: str) -> dict[str, Resource]:
        """Resources of one kind by their target, lowercased, to match what the directory reports."""
        return {r.target.lower(): r for r in self.resources.values() if r.kind == kind}


def _documents(path: str, text: str) -> list[dict]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as err:
        raise CatalogError([f"{path}: not valid YAML ({err.__class__.__name__})"]) from err
    if data is None:
        return []
    if path.startswith("grants/"):
        if not isinstance(data, dict):
            raise CatalogError([f"{path}: a grants file is one mapping with person and grants"])
        return [data]
    if not isinstance(data, list) or not all(isinstance(d, dict) for d in data):
        raise CatalogError([f"{path}: a list of entries is expected"])
    return data


def _error(path: str, err: ValidationError) -> list[str]:
    return [f"{path}: {'.'.join(str(p) for p in e['loc']) or 'entry'}: {e['msg']}" for e in err.errors()]


def load_catalog(files: Mapping[str, str]) -> Catalog:
    """A catalog from {relative path: YAML}, or CatalogError naming every problem at once."""
    problems: list[str] = []
    resources: dict[str, Resource] = {}
    roles: dict[str, Role] = {}
    teams: dict[str, Team] = {}
    people: dict[str, PersonGrants] = {}
    paths: dict[str, str] = {}
    kinds = {"resources/": (Resource, resources), "roles/": (Role, roles), "teams/": (Team, teams)}

    for path in sorted(files):
        if not path.endswith((".yaml", ".yml")):
            continue
        folder = next((prefix for prefix in (*kinds, "grants/") if path.startswith(prefix)), None)
        if folder is None:
            continue
        try:
            documents = _documents(path, files[path])
        except CatalogError as err:
            problems += err.problems
            continue
        for index, doc in enumerate(documents):
            where = f"{path}[{index}]" if folder != "grants/" else path
            try:
                if folder == "grants/":
                    person = PersonGrants.model_validate(doc)
                    allowed = person_files(person.person)
                    if path.replace(".yml", ".yaml") not in allowed:
                        problems.append(f"{path}: grants for {person.person} belong in {allowed[0]}")
                    elif person.person in people:
                        problems.append(f"{path}: {person.person} has a second grants file")
                    else:
                        people[person.person] = person
                        paths[person.person] = path
                    continue
                model, into = kinds[folder]
                entry = model.model_validate(doc)
                if entry.id in into:
                    problems.append(f"{where}: {folder.rstrip('/')[:-1]} {entry.id} is defined twice")
                into[entry.id] = entry
            except ValidationError as err:
                problems += _error(where, err)

    for resource in resources.values():
        if resource.owner not in teams:
            problems.append(f"resource {resource.id}: owner {resource.owner} is not a team")
        problems += [f"resource {resource.id}: incompatible {i} is not a resource" for i in resource.incompatible if i not in resources]
        problems += _approver_refs(f"resource {resource.id}", [*resource.approvers, *resource.fulfillers], teams)
    for role in roles.values():
        problems += [f"role {role.id}: resource {r} is not a resource" for r in role.resources if r not in resources]
        problems += _approver_refs(f"role {role.id}", role.approvers, teams)
    for team in teams.values():
        problems += [f"team {team.id}: role {r} is not a role" for r in team.roles if r not in roles]
    for person in people.values():
        seen: set[str] = set()
        for grant in person.grants:
            if grant.role and grant.role not in roles:
                problems.append(f"{paths[person.person]}: role {grant.role} is not a role")
            if grant.resource and grant.resource not in resources:
                problems.append(f"{paths[person.person]}: resource {grant.resource} is not a resource")
            if grant.key in seen:
                problems.append(f"{paths[person.person]}: {grant.key} is granted twice")
            seen.add(grant.key)

    if problems:
        raise CatalogError(problems)
    return Catalog(resources=resources, roles=roles, teams=teams, people=people, files=paths)


def _approver_refs(where: str, specs: list[str], teams: Mapping[str, Team]) -> list[str]:
    return [f"{where}: {s} names no team" for s in specs if s.startswith("team:") and s[5:] not in teams]


def dump_person(person: PersonGrants) -> str:
    """A grants file, in a stable order so a change shows as a small diff."""
    grants = []
    for grant in sorted(person.grants, key=lambda g: g.key):
        data = grant.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
        data.setdefault("granted", grant.granted.isoformat())
        grants.append(data)
    head = {"person": person.person, **({"name": person.name} if person.name else {})}
    return yaml.safe_dump({**head, "grants": grants}, sort_keys=False, allow_unicode=True, width=100)
