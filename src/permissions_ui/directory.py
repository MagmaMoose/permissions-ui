"""Microsoft Graph: who exists, which groups exist, who is in them.

Reads need User.Read.All and GroupMember.Read.All: application permissions in the default
`app` directory mode, where the app reads as itself, or delegated ones in the `delegated` mode,
where it reads as the signed-in person. Writes happen only in the `graph` fulfilment mode,
always as the app, and need GroupMember.ReadWrite.All; the default `pull-request` mode leaves
writing to the pipeline that applies the catalog. Membership is read transitively, so someone
in a nested group counts as a member, the same way a groups claim counts them.

Answers are cached in this replica's memory for a short while: every page of the UI asks
about the same few hundred groups. The cache is kept per reader, because a delegated read
returns only what that person may see (a guest sees less, and a tenant can stop members reading
other users): one person's answers are never served to another.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

GRAPH = "https://graph.microsoft.com/v1.0"
_USER_FIELDS = "id,displayName,givenName,surname,userPrincipalName,mail,accountEnabled,jobTitle,department"
_GROUP_FIELDS = "id,displayName,description"


class DirectoryError(Exception):
    def __init__(self, message: str, *, status: int = 502):
        super().__init__(message)
        self.status = status


def person_name(display: str, given: str, surname: str) -> str:
    """The display name up to the surname: directories often append an organisation
    ("Jane Doe - Contoso"). A display name that does not start with the given name stays whole."""
    if not (given and surname and display.lower().startswith(given.lower())):
        return display
    at = display.lower().find(surname.lower(), len(given))
    return display[: at + len(surname)] if at >= 0 else display


@dataclass(frozen=True)
class User:
    id: str
    name: str
    upn: str
    mail: str = ""
    enabled: bool = True
    job: str = ""
    department: str = ""

    @classmethod
    def parse(cls, data: dict[str, Any]) -> User:
        return cls(
            id=str(data.get("id", "")).lower(),
            name=person_name(str(data.get("displayName") or ""), str(data.get("givenName") or ""), str(data.get("surname") or "")),
            upn=str(data.get("userPrincipalName") or "").lower(),
            mail=str(data.get("mail") or "").lower(),
            enabled=bool(data.get("accountEnabled", True)),
            job=str(data.get("jobTitle") or ""),
            department=str(data.get("department") or ""),
        )


@dataclass(frozen=True)
class Group:
    id: str
    name: str
    description: str = ""

    @classmethod
    def parse(cls, data: dict[str, Any]) -> Group:
        return cls(id=str(data.get("id", "")).lower(), name=str(data.get("displayName") or ""), description=str(data.get("description") or ""))


def _odata(value: str) -> str:
    return value.replace("'", "''")


class AppToken:
    """The app's own Graph token, by client credentials, kept until shortly before it expires."""

    def __init__(self, http: httpx.AsyncClient, *, token_url: str, client_id: str, credential: Callable[[], dict[str, str]]):
        self._http = http
        self._token_url = token_url
        self._client_id = client_id
        self._credential = credential
        self._token: tuple[str, float] = ("", 0.0)
        self._lock = asyncio.Lock()

    async def __call__(self) -> str:
        async with self._lock:
            token, expires = self._token
            if token and expires - 120 > time.time():
                return token
            data = {"grant_type": "client_credentials", "client_id": self._client_id, "scope": "https://graph.microsoft.com/.default", **self._credential()}
            try:
                response = await self._http.post(self._token_url, data=data, timeout=15)
                body = response.json()
            except (httpx.HTTPError, ValueError) as err:
                raise DirectoryError(f"cannot get a Microsoft Graph token: {err.__class__.__name__}") from err
            if response.status_code != 200 or not body.get("access_token"):
                reason = str(body.get("error_description") or body.get("error") or response.status_code).splitlines()[0][:300]
                raise DirectoryError(f"Microsoft refused the app's Graph token: {reason}")
            self._token = (str(body["access_token"]), time.time() + int(body.get("expires_in") or 3600))
            return self._token[0]


class Directory:
    def __init__(self, http: httpx.AsyncClient, *, token: Callable[[], Awaitable[str]], reader: Callable[[], str] = lambda: "", ttl: float = 120.0):
        """`token` gives the bearer token for the call at hand: the app's own, or the signed-in
        person's. `reader` says whose reads they are ("" for the app) and keys the cache."""
        self._http = http
        self._token = token
        self._reader = reader
        self._cache: dict[tuple[str, str], tuple[float, Any]] = {}
        self._swept = float("-inf")
        self._ttl = ttl
        self._calls = asyncio.Semaphore(8)

    async def _request(self, method: str, url: str, *, json: Any = None, search: bool = False) -> httpx.Response:
        headers = {"Authorization": f"Bearer {await self._token()}", "Accept": "application/json"}
        if search:
            # $search and $count on directory objects need eventual consistency.
            headers["ConsistencyLevel"] = "eventual"
        async with self._calls:
            try:
                response = await self._http.request(method, url, headers=headers, json=json, timeout=20)
            except httpx.HTTPError as err:
                raise DirectoryError(f"Microsoft Graph did not answer: {err.__class__.__name__}") from err
        if response.status_code == 403:
            raise DirectoryError("Microsoft Graph refused this for want of a permission; see the README", status=403)
        return response

    async def _pages(self, url: str, *, search: bool = False, limit: int = 5000) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        next_url: str | None = url
        while next_url and len(items) < limit:
            response = await self._request("GET", next_url, search=search)
            if response.status_code == 404:
                return []
            if response.status_code != 200:
                raise DirectoryError(f"Microsoft Graph answered HTTP {response.status_code}")
            body = response.json()
            items += body.get("value", [])
            next_url = body.get("@odata.nextLink")
        return items

    async def _cached(self, key: str, load):
        slot = (self._reader(), key)
        now = time.monotonic()
        hit = self._cache.get(slot)
        if hit and hit[0] > now:
            return hit[1]
        value = await load()
        if now - self._swept > self._ttl:
            # Kept per reader, expired answers would pile up for everyone who ever signed in.
            self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
            self._swept = now
        self._cache[slot] = (time.monotonic() + self._ttl, value)
        return value

    def forget(self, *prefixes: str) -> None:
        for slot in [s for s in self._cache if s[1].startswith(prefixes)]:
            del self._cache[slot]

    async def user(self, key: str) -> User | None:
        """A user by object id or user principal name."""

        async def load() -> User | None:
            response = await self._request("GET", f"{GRAPH}/users/{quote(key.lower())}?$select={_USER_FIELDS}")
            if response.status_code == 404:
                return None
            if response.status_code != 200:
                raise DirectoryError(f"Microsoft Graph answered HTTP {response.status_code} for a user")
            return User.parse(response.json())

        return await self._cached(f"user:{key.lower()}", load)

    async def search_users(self, text: str) -> list[User]:
        text = text.replace('"', "").strip()
        if len(text) < 2:
            return []
        query = quote(f'"displayName:{text}" OR "userPrincipalName:{text}" OR "mail:{text}"')
        items = await self._pages(f"{GRAPH}/users?$search={query}&$select={_USER_FIELDS}&$top=20", search=True, limit=20)
        return [User.parse(i) for i in items]

    async def group(self, key: str) -> Group | None:
        """A group by object id or by its exact display name."""

        async def load() -> Group | None:
            if len(key) == 36 and key.count("-") == 4:
                response = await self._request("GET", f"{GRAPH}/groups/{key.lower()}?$select={_GROUP_FIELDS}")
                if response.status_code == 200:
                    return Group.parse(response.json())
                if response.status_code != 404:
                    raise DirectoryError(f"Microsoft Graph answered HTTP {response.status_code} for a group")
            items = await self._pages(f"{GRAPH}/groups?$filter={quote(f"displayName eq '{_odata(key)}'")}&$select={_GROUP_FIELDS}", limit=2)
            return Group.parse(items[0]) if len(items) == 1 else None

        return await self._cached(f"group:{key.lower()}", load)

    async def groups(self, prefix: str) -> list[Group]:
        """Every group whose display name starts with `prefix`."""

        async def load() -> list[Group]:
            filter_ = quote(f"startswith(displayName,'{_odata(prefix)}')")
            return [Group.parse(i) for i in await self._pages(f"{GRAPH}/groups?$filter={filter_}&$select={_GROUP_FIELDS}&$top=999")]

        return await self._cached(f"groups:{prefix.lower()}", load)

    async def members(self, group_id: str) -> list[User]:
        """Everyone in a group, through nested groups too."""

        async def load() -> list[User]:
            url = f"{GRAPH}/groups/{group_id}/transitiveMembers/microsoft.graph.user?$select={_USER_FIELDS}&$top=999"
            return [User.parse(i) for i in await self._pages(url)]

        return await self._cached(f"members:{group_id}", load)

    async def member_of(self, user_id: str) -> list[Group]:
        """Every group a user is in, through nested groups too."""

        async def load() -> list[Group]:
            url = f"{GRAPH}/users/{user_id}/transitiveMemberOf/microsoft.graph.group?$select={_GROUP_FIELDS}&$top=999"
            return [Group.parse(i) for i in await self._pages(url)]

        return await self._cached(f"memberof:{user_id}", load)

    async def add_member(self, group_id: str, user_id: str) -> None:
        body = {"@odata.id": f"{GRAPH}/directoryObjects/{user_id}"}
        response = await self._request("POST", f"{GRAPH}/groups/{group_id}/members/$ref", json=body)
        # 400 with "already exist" means it is done already.
        if response.status_code not in (204, 400) or (response.status_code == 400 and "already exist" not in response.text):
            raise DirectoryError(f"adding a member answered HTTP {response.status_code}")
        self.forget(f"members:{group_id}", f"memberof:{user_id}")

    async def remove_member(self, group_id: str, user_id: str) -> None:
        response = await self._request("DELETE", f"{GRAPH}/groups/{group_id}/members/{user_id}/$ref")
        if response.status_code not in (204, 404):
            raise DirectoryError(f"removing a member answered HTTP {response.status_code}")
        self.forget(f"members:{group_id}", f"memberof:{user_id}")
