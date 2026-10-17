"""A request as a pull request: what it asks for, who approved it, and where it stands.

The pull request body starts with a summary for people and ends with the request as JSON in an
HTML comment, which GitHub does not show. Approvals, declines and "done" notes for manual
resources are comments that carry the same kind of hidden record. Every record carries an HMAC
under the server's record key over its pull request's number, and only comments written by this
app's login count. So neither a lookalike comment nor another tool commenting as the same GitHub
App approves anything, a record copied to another pull request counts for nothing, and a request
edited on GitHub is no longer a request.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Literal

from .catalog import Catalog, Grant, PersonGrants, person_file
from .github import PullRequest
from .policy import Need, outstanding

LABEL = "access-request"
STATE_LABELS = ("access:pending", "access:approved", "access:declined", "access:granted")
_RECORD = re.compile(r"<!--\s*permissions-ui:(request|approval|decline|done)\s+(\{.*?\})\s+([0-9a-f]{64})\s*-->", re.S)

State = Literal["pending", "approved", "granted", "declined", "withdrawn", "open"]


class Records:
    """Writes records signed with the first key and reads those that any key signed for this pull
    request number, so a key rotated out keeps what it signed while it stays listed."""

    def __init__(self, keys: tuple[bytes, ...]):
        self._keys = keys

    @staticmethod
    def _mac(key: bytes, kind: str, number: int, raw: str) -> str:
        return hmac.new(key, f"{kind}\n{number}\n{raw}".encode(), hashlib.sha256).hexdigest()

    def write(self, kind: str, data: dict[str, Any], number: int) -> str:
        # `--` would end the HTML comment early, so it never appears in the JSON.
        raw = json.dumps(data, separators=(",", ":"), sort_keys=True).replace("--", "-\\u002d")
        return f"<!-- permissions-ui:{kind} {raw} {self._mac(self._keys[0], kind, number, raw)} -->"

    def read(self, text: str, kind: str, number: int) -> list[dict[str, Any]]:
        out = []
        for found_kind, raw, mac in _RECORD.findall(text):
            if found_kind != kind or not any(hmac.compare_digest(mac, self._mac(key, kind, number, raw)) for key in self._keys):
                continue
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            if isinstance(data, dict):
                out.append(data)
        return out


@dataclass(frozen=True)
class Approval:
    upn: str
    name: str
    at: str
    resources: tuple[str, ...]


@dataclass(frozen=True)
class Request:
    number: int
    url: str
    kind: Literal["grant", "revoke"]
    person: str
    items: tuple[dict[str, str], ...]
    reason: str
    requested_by: str
    expires: str | None
    created: str
    state: State
    approvals: tuple[Approval, ...] = ()
    outstanding: tuple[Need, ...] = ()
    declined_by: str = ""
    decline_reason: str = ""
    done: tuple[str, ...] = ()
    labels: tuple[str, ...] = field(default_factory=tuple)


def branch_name(person: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", person.split("@")[0].lower()).strip("-")[:40] or "person"
    return f"access/{slug}-{secrets.token_hex(3)}"


def describe(catalog: Catalog, items: list[dict[str, str]]) -> str:
    names = []
    for item in items:
        if item.get("role"):
            names.append(catalog.roles[item["role"]].name)
        else:
            names.append(catalog.resources[item["resource"]].name)
    return ", ".join(names) if len(names) <= 3 else f"{', '.join(names[:3])} and {len(names) - 3} more"


def title(catalog: Catalog, kind: str, person: str, items: list[dict[str, str]]) -> str:
    verb = "Grant" if kind == "grant" else "Remove"
    return f"{verb} {describe(catalog, items)} {'to' if kind == 'grant' else 'from'} {person}"[:250]


def body(catalog: Catalog, meta: dict[str, Any], need_list: list[Need], origin: str) -> str:
    """The pull request body for people. The request record follows once the number is known."""
    lines = [
        f"**{'Access request' if meta['kind'] == 'grant' else 'Removal'}** for `{meta['person']}`, by `{meta['requested_by']}`.",
        "",
        f"> {meta['reason']}",
        "",
        "| Resource | Tier | Via | Approvals needed from |",
        "|---|---|---|---|",
    ]
    by_resource = {n.resource: n for n in need_list}
    for item in meta["items"]:
        ids = catalog.roles[item["role"]].resources if item.get("role") else [item["resource"]]
        for rid in ids:
            resource = catalog.resources[rid]
            need = by_resource.get(rid)
            approvers = f"{need.count} of {', '.join(need.approvers)}" if need else "none"
            via = f"role {item['role']}" if item.get("role") else "direct"
            lines.append(f"| {resource.name} (`{rid}`) | {resource.tier} | {via} | {approvers} |")
    if meta.get("expires"):
        lines += ["", f"Expires {meta['expires']}."]
    lines += ["", f"Approve or decline in [permissions-ui]({origin}/#/requests). Approvals left here by hand do not count."]
    return "\n".join(lines)


def apply_grants(current: PersonGrants | None, person: str, items: list[dict[str, str]], *, reason: str, requested_by: str, expires: date | None, today: date) -> PersonGrants:
    """The person's grants with these items added; an item they hold already is renewed."""
    keys = {f"role:{i['role']}" if i.get("role") else f"resource:{i['resource']}" for i in items}
    kept = [g for g in (current.grants if current else []) if g.key not in keys]
    added = [
        Grant(role=i.get("role") or None, resource=i.get("resource") or None, reason=reason, requested_by=requested_by, granted=today, expires=expires)
        for i in items
    ]
    return PersonGrants(person=person, grants=kept + added)


def remove_grants(current: PersonGrants, items: list[dict[str, str]] | None) -> PersonGrants | None:
    """The person's grants without these items (all of them when `items` is None); None when empty."""
    if items is None:
        return None
    keys = {f"role:{i['role']}" if i.get("role") else f"resource:{i['resource']}" for i in items}
    left = [g for g in current.grants if g.key not in keys]
    return PersonGrants(person=current.person, grants=left) if left else None


def finalise(current: PersonGrants, items: list[dict[str, str]], approvers: list[str], number: int) -> PersonGrants:
    """Write who approved, and which request it was, into the grants this request added."""
    keys = {f"role:{i['role']}" if i.get("role") else f"resource:{i['resource']}" for i in items}
    grants = [g.model_copy(update={"approved_by": sorted(approvers), "request": number}) if g.key in keys else g for g in current.grants]
    return PersonGrants(person=current.person, grants=grants)


def parse(pull: PullRequest, bot: str, records: Records, need_list: list[Need] | None = None) -> Request | None:
    """A request from its pull request, or None when its body holds no request record this app signed for it."""
    metas = records.read(pull.body, "request", pull.number)
    if not metas:
        return None
    meta = metas[-1]
    if meta.get("kind") not in ("grant", "revoke") or not isinstance(meta.get("items"), list):
        return None
    trusted = [c for c in pull.comments if bot and c.author.lower() == bot.lower()]
    approvals: list[Approval] = []
    seen: set[str] = set()
    for comment in trusted:
        for data in records.read(comment.body, "approval", pull.number):
            upn = str(data.get("upn", "")).lower()
            if upn and upn not in seen:
                seen.add(upn)
                approvals.append(Approval(upn, str(data.get("name", "")), str(data.get("at", comment.created)), tuple(data.get("resources") or [])))
    declines = [d for c in trusted for d in records.read(c.body, "decline", pull.number)]
    done = tuple(dict.fromkeys(str(d.get("resource")) for c in trusted for d in records.read(c.body, "done", pull.number)))

    counted: dict[str, set[str]] = {}
    for approval in approvals:
        for rid in approval.resources:
            counted.setdefault(rid, set()).add(approval.upn)
    left = outstanding(need_list, counted) if need_list is not None else []

    state: State
    if pull.merged:
        state = "granted"
    elif pull.state == "closed":
        state = "declined" if declines and not declines[-1].get("withdrawn") else "withdrawn"
    elif need_list is None:
        state = "open"
    else:
        state = "approved" if not left else "pending"
    last_decline = declines[-1] if declines else {}
    return Request(
        number=pull.number,
        url=pull.url,
        kind=meta["kind"],
        person=str(meta.get("person", "")),
        items=tuple({k: str(v) for k, v in i.items() if k in ("role", "resource")} for i in meta["items"] if isinstance(i, dict)),
        reason=str(meta.get("reason", "")),
        requested_by=str(meta.get("requested_by", "")),
        expires=meta.get("expires"),
        created=pull.created,
        state=state,
        approvals=tuple(approvals),
        outstanding=tuple(left),
        declined_by=str(last_decline.get("upn", "")),
        decline_reason=str(last_decline.get("reason", "")),
        done=done,
        labels=pull.labels,
    )


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def person_path(person: str) -> str:
    return person_file(person)
