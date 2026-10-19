"""What people do here, as operations on the catalog repository: ask, approve, decline, withdraw,
remove, mark a manual resource done, and expire what ran out.

Every change starts from the catalog branch as it is at that moment. When a request is fully
approved its branch is rebuilt on the latest catalog before merging, so two requests for the
same person never conflict and an approval always merges what was approved, nothing older.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from . import requests as rq
from .catalog import Catalog, PersonGrants, dump_person, load_catalog
from .directory import Directory
from .github import GitHub, GitHubError, PullRequest
from .notify import Notifier
from .policy import Need, conflicts, eligible_needs, latest_expiry, members_of, needs

log = logging.getLogger("permissions_ui")


class PortalError(Exception):
    def __init__(self, status: int, code: str, message: str, **details: Any):
        super().__init__(message)
        self.status = status
        self.code = code
        self.details = details


@dataclass(frozen=True)
class Actor:
    oid: str
    upn: str
    name: str
    admin: bool = False
    auditor: bool = False


SYSTEM = Actor(oid="", upn="permissions-ui", name="permissions-ui", admin=True)


def _log(event: str, **fields: Any) -> None:
    log.info(json.dumps({"msg": event, **fields}))


class LocalSource:
    """A catalog directory on disk: read-only, for trying it out and for reviews without GitHub."""

    def __init__(self, root: Path):
        self._root = root

    async def __call__(self) -> tuple[str, dict[str, str]]:
        files = {str(p.relative_to(self._root)): p.read_text() for p in sorted(self._root.rglob("*.y*ml")) if p.is_file()}
        digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
        return digest, files


class Portal:
    def __init__(
        self,
        *,
        source: Callable[[], Awaitable[tuple[str, dict[str, str]]]],
        github: GitHub | None,
        directory: Directory,
        notifier: Notifier,
        origin: str,
        records: rq.Records,
        fulfilment: str = "pull-request",
        auto_merge: bool = True,
        today: Callable[[], date] = date.today,
    ):
        self._source = source
        self.github = github
        self.directory = directory
        self._notifier = notifier
        self._origin = origin
        self._records = records
        self._fulfilment = fulfilment
        self._auto_merge = auto_merge
        self.today = today
        self._catalog: tuple[str, Catalog] | None = None

    @property
    def read_only(self) -> bool:
        return self.github is None

    async def catalog(self) -> Catalog:
        version, files = await self._source()
        if self._catalog is None or self._catalog[0] != version:
            self._catalog = (version, load_catalog(files))
        return self._catalog[1]

    def is_lead(self, catalog: Catalog, upn: str) -> bool:
        return any(upn in team.leads for team in catalog.teams.values())

    def sees_everyone(self, catalog: Catalog, actor: Actor) -> bool:
        return actor.admin or actor.auditor or self.is_lead(catalog, actor.upn)

    def may_withdraw(self, catalog: Catalog, req: rq.Request, actor: Actor) -> bool:
        """The requester, an admin, or a lead of a team whose roles the person holds."""
        if actor.admin or actor.upn == req.requested_by:
            return True
        held = {g.role for g in catalog.people[req.person].grants if g.role} if req.person in catalog.people else set()
        return any(actor.upn in team.leads and held & set(team.roles) for team in catalog.teams.values())

    def _repo(self) -> GitHub:
        if self.github is None:
            raise PortalError(400, "read_only", "This catalog is read-only here: requests need a catalog repository.")
        return self.github

    # ── Reading requests ──────────────────────────────────────────────────────────────

    def needs_of(self, catalog: Catalog, kind: str, items: list[dict[str, str]]) -> list[Need] | None:
        if kind == "revoke":
            return []
        try:
            return needs(catalog, items)
        except KeyError:
            # Something the request names has left the catalog since.
            return None

    async def _parse(self, catalog: Catalog, pull: PullRequest) -> rq.Request | None:
        metas = self._records.read(pull.body, "request", pull.number)
        if not metas:
            return None
        meta = metas[-1]
        items = [i for i in meta.get("items") or [] if isinstance(i, dict)]
        return rq.parse(pull, await self._repo().bot(), self._records, self.needs_of(catalog, str(meta.get("kind")), items))

    async def request(self, number: int) -> rq.Request:
        catalog = await self.catalog()
        found = await self._parse(catalog, await self._repo().pull(number))
        if found is None:
            raise PortalError(404, "not_a_request", "That pull request is not an access request.")
        return found

    async def requests(self, *, closed: int = 40) -> list[rq.Request]:
        """Open requests and the `closed` newest closed ones."""
        github = self.github
        if github is None:
            return []
        catalog = await self.catalog()
        listed = await github.pulls(rq.LABEL, state="open")
        if closed:
            listed += await github.pulls(rq.LABEL, state="closed", limit=closed)
        gate = asyncio.Semaphore(6)

        async def one(pull: PullRequest) -> rq.Request | None:
            async with gate:
                return await self._parse(catalog, PullRequest(**{**pull.__dict__, "comments": await github.comments(pull.number)}))

        found = await asyncio.gather(*(one(p) for p in listed))
        return [r for r in found if r is not None]

    async def can_approve(self, catalog: Catalog, req: rq.Request, actor: Actor) -> bool:
        if req.state != "pending" or actor.upn in (req.requested_by, req.person) or any(a.upn == actor.upn for a in req.approvals):
            return False
        return bool(await eligible_needs(catalog, self.directory, req.outstanding, actor.upn))

    # ── Asking ────────────────────────────────────────────────────────────────────────

    def _check_items(self, catalog: Catalog, items: list[dict[str, str]]) -> None:
        if not items:
            raise PortalError(400, "nothing_requested", "Pick at least one role or resource.")
        for item in items:
            if item.get("role") and item["role"] not in catalog.roles:
                raise PortalError(400, "unknown_item", f"There is no role {item['role']}.")
            if item.get("resource") and item["resource"] not in catalog.resources:
                raise PortalError(400, "unknown_item", f"There is no resource {item['resource']}.")
            if bool(item.get("role")) == bool(item.get("resource")):
                raise PortalError(400, "unknown_item", "Each item names a role or a resource.")

    async def ask(self, actor: Actor, *, person: str, items: list[dict[str, str]], reason: str, expires: date | None = None) -> rq.Request:
        github = self._repo()
        catalog = await self.catalog()
        person = person.lower()
        today = self.today()
        self._check_items(catalog, items)
        if person != actor.upn and not (actor.admin or self.is_lead(catalog, actor.upn)):
            raise PortalError(403, "not_allowed", "Only team leads and admins ask for someone else.")
        user = await self.directory.user(person)
        if user is None or not user.enabled:
            raise PortalError(404, "unknown_person", f"{person} is not an enabled account in the directory.")

        held = catalog.holdings(person, today)
        wanted = catalog.resources_of(items)
        if all(rid in held and held[rid].expires is None for rid in wanted):
            raise PortalError(409, "already_held", f"{person} already holds all of that.")
        clashes = [pair for pair in conflicts(catalog, held, wanted) if set(pair) & set(wanted)]
        if clashes:
            raise PortalError(409, "conflict", "That would combine access the catalog keeps apart: " + ", ".join(f"{a} and {b}" for a, b in clashes), pairs=clashes)
        limit = latest_expiry(catalog, items, today)
        if expires is not None and expires < today:
            raise PortalError(400, "expires_in_past", "The expiry date has passed.")
        if limit is not None and (expires is None or expires > limit):
            if expires is not None:
                raise PortalError(400, "expires_too_late", f"This access may run until {limit.isoformat()} at the latest.")
            expires = limit

        need_list = needs(catalog, items)
        meta = {"version": 1, "kind": "grant", "person": person, "name": user.name, "items": items, "reason": reason, "requested_by": actor.upn, "expires": expires.isoformat() if expires else None}
        changed = rq.apply_grants(catalog.people.get(person), person, items, reason=reason, requested_by=actor.upn, expires=expires, today=today, name=user.name)
        title = rq.title(catalog, "grant", person, items, user.name)
        branch = rq.branch_name(person)
        await github.commit(branch, {catalog.file_for(person): dump_person(changed)}, title)
        privileged = any(catalog.resources[rid].tier == "privileged" for rid in wanted)
        pull = await self._open(github, branch, title, rq.body(catalog, meta, need_list, self._origin), meta, [rq.LABEL, "access:pending", *(["tier:privileged"] if privileged else [])])
        _log("request opened", number=pull.number, person=person, by=actor.upn, items=items)
        # A lead asking for someone on their behalf is approving it: an onboarding needs no
        # second click from the same lead. Privileged access still waits for a second person.
        if person != actor.upn:
            mine = [n.resource for n in await eligible_needs(catalog, self.directory, need_list, actor.upn)]
            if mine:
                note = self._records.write("approval", {"upn": actor.upn, "name": actor.name, "oid": actor.oid, "resources": mine, "at": rq.now()}, pull.number)
                await github.comment(pull.number, f"Asked for and approved by {actor.name or actor.upn} (`{actor.upn}`).\n\n{note}")
        req = await self.request(pull.number)
        if req.state == "approved":
            await self._finalise(req, actor)
            return await self.request(pull.number)
        await self._notifier.send(
            f"Access request #{pull.number} needs approval",
            {"For": rq.label(user.name, person), "Asks for": rq.describe(catalog, items), "By": actor.upn, "Why": reason, "Approvers": ", ".join(sorted({s for n in req.outstanding for s in n.approvers}))},
            f"{self._origin}/#/requests/{pull.number}",
            "Review",
        )
        return req

    async def remove(self, actor: Actor, *, person: str, items: list[dict[str, str]] | None, reason: str) -> rq.Request:
        """Take access away: no approval needed, merged at once. `items` None takes everything."""
        github = self._repo()
        catalog = await self.catalog()
        person = person.lower()
        current = catalog.people.get(person)
        if current is None:
            raise PortalError(404, "nothing_held", f"{person} holds nothing in the catalog.")
        if person != actor.upn and not (actor.admin or self.is_lead(catalog, actor.upn)):
            raise PortalError(403, "not_allowed", "Only the person themselves, team leads and admins remove access.")
        held = {g.key for g in current.grants}
        if items is not None:
            self._check_items(catalog, items)
            missing = [i for i in items if (f"role:{i['role']}" if i.get("role") else f"resource:{i['resource']}") not in held]
            if missing:
                raise PortalError(400, "not_held", f"{person} does not hold that.")
        listed = items if items is not None else [{"role": g.role} if g.role else {"resource": str(g.resource)} for g in current.grants]
        meta = {"version": 1, "kind": "revoke", "person": person, "name": current.name, "items": listed, "everything": items is None, "reason": reason, "requested_by": actor.upn, "expires": None}
        title = rq.title(catalog, "revoke", person, listed, current.name)
        branch = rq.branch_name(person)
        left = rq.remove_grants(current, items)
        await github.commit(branch, {catalog.file_for(person): dump_person(left) if left else None}, title)
        pull = await self._open(github, branch, title, rq.body(catalog, meta, [], self._origin), meta, [rq.LABEL, "access:approved", "access:removal"])
        _log("removal opened", number=pull.number, person=person, by=actor.upn, everything=items is None)
        req = await self.request(pull.number)
        await self._finalise(req, actor)
        return await self.request(pull.number)

    async def _open(self, github: GitHub, branch: str, title: str, text: str, meta: dict[str, Any], labels: list[str]) -> PullRequest:
        """Open the pull request and an issue that tracks it, then sign its request record, which
        needs its number. A pull request left without one would still carry the grant, so it is
        closed, with its issue."""
        pull = await github.open_pull(branch, title, text, labels)
        issue = 0
        try:
            issue = await github.open_issue(title, f"{meta['reason']}\n\nRequested by `{meta['requested_by']}` in {pull.url}.")
            record = self._records.write("request", {**meta, "issue": issue}, pull.number)
            await github.set_body(pull.number, f"{text}\n\nCloses #{issue}\n\n{record}")
        except GitHubError:
            await github.close(pull.number)
            if issue:
                await github.close_issue(issue)
            raise
        return pull

    # ── Deciding ──────────────────────────────────────────────────────────────────────

    async def approve(self, actor: Actor, number: int) -> rq.Request:
        github = self._repo()
        catalog = await self.catalog()
        req = await self.request(number)
        if req.state == "approved":
            # Fully approved before, but not merged: try the merge again.
            await self._finalise(req, actor)
            return await self.request(number)
        if req.state != "pending":
            raise PortalError(409, "not_pending", f"Request #{number} is {req.state}.")
        if actor.upn in (req.requested_by, req.person):
            raise PortalError(403, "own_request", "Nobody approves a request they made or one that gives them access.")
        if any(a.upn == actor.upn for a in req.approvals):
            raise PortalError(409, "already_approved", "You approved this already.")
        all_needs = self.needs_of(catalog, req.kind, list(req.items)) or []
        if not await eligible_needs(catalog, self.directory, req.outstanding, actor.upn):
            raise PortalError(403, "not_an_approver", "You are not among the approvers this request still waits for.")
        counts_for = [n.resource for n in await eligible_needs(catalog, self.directory, all_needs, actor.upn)]
        note = self._records.write("approval", {"upn": actor.upn, "name": actor.name, "oid": actor.oid, "resources": counts_for, "at": rq.now()}, number)
        await github.comment(number, f"Approved by {actor.name or actor.upn} (`{actor.upn}`) in permissions-ui.\n\n{note}")
        _log("request approved", number=number, by=actor.upn, resources=counts_for)
        req = await self.request(number)
        if req.state == "approved":
            await self._finalise(req, actor)
            req = await self.request(number)
        return req

    async def decline(self, actor: Actor, number: int, reason: str) -> rq.Request:
        catalog = await self.catalog()
        req = await self.request(number)
        if req.state not in ("pending", "approved", "open"):
            raise PortalError(409, "not_open", f"Request #{number} is {req.state}.")
        withdrawn = actor.upn == req.requested_by
        if not withdrawn and not actor.admin:
            all_needs = self.needs_of(catalog, req.kind, list(req.items)) or []
            if not await eligible_needs(catalog, self.directory, all_needs, actor.upn):
                raise PortalError(403, "not_an_approver", "Only an approver, an admin or the requester closes a request.")
        await self._close(req, actor, reason, withdrawn=withdrawn)
        if not withdrawn:
            await self._notifier.send(f"Access request #{number} was declined", {"For": req.person, "By": actor.upn, "Why": reason}, f"{self._origin}/#/requests/{number}")
        return await self.request(number)

    async def withdraw(self, actor: Actor, number: int, reason: str) -> rq.Request:
        catalog = await self.catalog()
        req = await self.request(number)
        if req.state not in ("pending", "approved", "open"):
            raise PortalError(409, "not_open", f"Request #{number} is {req.state}.")
        if not self.may_withdraw(catalog, req, actor):
            raise PortalError(403, "not_allowed", "Only the requester, the person's team lead or an admin withdraws a request.")
        await self._close(req, actor, reason, withdrawn=True)
        return await self.request(number)

    async def _close(self, req: rq.Request, actor: Actor, reason: str, *, withdrawn: bool) -> None:
        """Close the pull request and its issue, saying why, and delete the request's branch."""
        github = self._repo()
        number = req.number
        note = self._records.write("decline", {"upn": actor.upn, "name": actor.name, "reason": reason, "withdrawn": withdrawn, "at": rq.now()}, number)
        said = f"{'Withdrawn' if withdrawn else 'Declined'} by {actor.name or actor.upn} (`{actor.upn}`): {reason}"
        await github.comment(number, f"{said}\n\n{note}")
        await github.close(number)
        await github.label(number, ["access:declined"], ("access:pending", "access:approved"))
        if req.issue:
            await github.comment(req.issue, said)
            await github.close_issue(req.issue)
        await github.delete_branch((await github.pull(number, with_comments=False)).head)
        _log("request declined", number=number, by=actor.upn, withdrawn=withdrawn)

    async def done(self, actor: Actor, number: int, resource: str) -> rq.Request:
        """A fulfiller set up a manual resource from a granted request."""
        github = self._repo()
        catalog = await self.catalog()
        req = await self.request(number)
        if req.state != "granted" or req.kind != "grant":
            raise PortalError(409, "not_granted", f"Request #{number} has not been granted.")
        found = catalog.resources.get(resource)
        if found is None or found.kind != "manual" or resource not in catalog.resources_of(req.items):
            raise PortalError(400, "not_manual", "That is not a manual resource of this request.")
        if resource in req.done:
            raise PortalError(409, "already_done", "That is marked done already.")
        fulfillers: set[str] = set()
        for spec in found.fulfillers:
            fulfillers |= await members_of(spec, catalog, self.directory)
        if actor.upn not in fulfillers and not actor.admin:
            raise PortalError(403, "not_a_fulfiller", "You are not a fulfiller of that resource.")
        note = self._records.write("done", {"resource": resource, "upn": actor.upn, "name": actor.name, "at": rq.now()}, number)
        await github.comment(number, f"{found.name} set up by {actor.name or actor.upn} (`{actor.upn}`).\n\n{note}")
        manual = {r for r in catalog.resources_of(req.items) if catalog.resources[r].kind == "manual"}
        if manual <= {*req.done, resource}:
            await github.label(number, [], ("access:manual-pending",))
        return await self.request(number)

    async def tasks(self, actor: Actor) -> list[tuple[rq.Request, list[str]]]:
        """Granted requests with manual resources this person sets up and has not marked done."""
        if self.github is None:
            return []
        catalog = await self.catalog()
        out = []
        for pull in await self.github.pulls("access:manual-pending", state="closed", limit=50):
            req = await self._parse(catalog, await self.github.pull(pull.number))
            if req is None or req.state != "granted":
                continue
            todo = []
            for rid in catalog.resources_of(req.items):
                resource = catalog.resources.get(rid)
                if resource is None or resource.kind != "manual" or rid in req.done:
                    continue
                fulfillers: set[str] = set()
                for spec in resource.fulfillers:
                    fulfillers |= await members_of(spec, catalog, self.directory)
                if actor.admin or actor.upn in fulfillers:
                    todo.append(rid)
            if todo:
                out.append((req, todo))
        return out

    # ── Merging ───────────────────────────────────────────────────────────────────────

    async def _finalise(self, req: rq.Request, actor: Actor) -> None:
        """Rebuild the request's branch on the latest catalog, then merge exactly that commit and,
        in the `graph` mode, change the groups."""
        github = self._repo()
        pull = await github.pull(req.number, with_comments=False)
        catalog = await self.catalog()
        today = self.today()
        items = [dict(i) for i in req.items]
        current = catalog.people.get(req.person)
        before = set(catalog.holdings(req.person, today))

        if req.kind == "grant":
            expires = date.fromisoformat(req.expires) if req.expires else None
            changed: PersonGrants | None = rq.apply_grants(current, req.person, items, reason=req.reason, requested_by=req.requested_by, expires=expires, today=today, name=req.person_name)
            changed = rq.finalise(changed, items, [a.upn for a in req.approvals], req.number)  # type: ignore[arg-type]
        else:
            everything = bool(self._records.read(pull.body, "request", req.number)[-1].get("everything"))
            changed = rq.remove_grants(current, None if everything else items) if current else None
        content = dump_person(changed) if changed else None
        built = await github.commit(pull.head, {catalog.file_for(req.person): content}, f"{pull.title} (#{req.number})", replace=True)
        await github.label(req.number, ["access:approved"], ("access:pending",))
        if not self._auto_merge:
            await github.comment(req.number, "Approved. This repository merges access requests by hand.")
            return

        approvers = ", ".join(a.upn for a in req.approvals) or actor.upn
        await github.approve(req.number, built, f"Approved in permissions-ui by {approvers}.")
        if not await github.merge(req.number, built, f"{pull.title} (#{req.number})"):
            await github.comment(req.number, "Approved, but the branch rules still hold this back. It merges once they allow it.")
            return
        manual = [rid for rid in catalog.resources_of(items) if rid in catalog.resources and catalog.resources[rid].kind == "manual"]
        await github.label(req.number, ["access:granted", *(["access:manual-pending"] if manual and req.kind == "grant" else [])], ("access:approved",))
        _log("request merged", number=req.number, kind=req.kind, person=req.person)

        people = dict(catalog.people)
        if changed:
            people[req.person] = changed
        else:
            people.pop(req.person, None)
        after = set(Catalog(catalog.resources, catalog.roles, catalog.teams, people).holdings(req.person, today))
        if self._fulfilment == "graph":
            await self._apply(catalog, req.person, before, after)
        if req.kind == "grant":
            await self._notifier.send(f"Access granted: #{req.number}", {"For": rq.label(changed.name if changed else req.person_name, req.person), "Granted": rq.describe(catalog, items)}, f"{self._origin}/#/people/{req.person}")
            if manual:
                await self._notifier.send(
                    f"Set up access by hand: #{req.number}",
                    {"For": rq.label(changed.name if changed else req.person_name, req.person), "Systems": ", ".join(catalog.resources[r].name for r in manual)},
                    f"{self._origin}/#/tasks",
                    "Open tasks",
                )

    async def _apply(self, catalog: Catalog, person: str, before: set[str], after: set[str]) -> None:
        user = await self.directory.user(person)
        if user is None:
            return
        for rid, add in [*((r, True) for r in sorted(after - before)), *((r, False) for r in sorted(before - after))]:
            resource = catalog.resources.get(rid)
            if resource is None or resource.kind != "entra-group":
                continue
            group = await self.directory.group(resource.target)
            if group is None:
                _log("group not found", resource=rid, target=resource.target)
                continue
            await (self.directory.add_member if add else self.directory.remove_member)(group.id, user.id)
            _log("membership added" if add else "membership removed", resource=rid, person=person)

    # ── Expiry ────────────────────────────────────────────────────────────────────────

    async def expire(self) -> list[int]:
        """Open and merge a removal for every grant that expired, one per person."""
        catalog = await self.catalog()
        today = self.today()
        opened = []
        for person in sorted(catalog.people):
            lapsed = [g for g in catalog.people[person].grants if not g.active(today)]
            if not lapsed:
                continue
            items = [{"role": g.role} if g.role else {"resource": str(g.resource)} for g in lapsed]
            req = await self.remove(SYSTEM, person=person, items=items, reason=f"Expired on or before {today.isoformat()}.")
            opened.append(req.number)
            catalog = await self.catalog()
        return opened
