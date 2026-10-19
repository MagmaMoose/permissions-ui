"""The catalog repository on GitHub: read the catalog, and turn every request into a pull request.

A request is a pull request that changes one person's grants file. Approvals are comments the
app writes on it, which only count when this app wrote them; merging is the grant, and the
pipeline that applies the catalog (or the `graph` fulfilment mode) makes it real. So the
repository is the whole state: this server stores nothing, and the pull request history is
the audit trail.

The app authenticates as a GitHub App installed on that one repository (preferred) or with a
token. The App's installation token is asked for that repository alone, with exactly
`PERMISSIONS`, so an App installed more widely, or shared with other tools, lends this app no more.
"""

from __future__ import annotations

import asyncio
import base64
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt

PERMISSIONS = {"contents": "write", "pull_requests": "write", "issues": "write", "metadata": "read"}
_LEVELS = {"read": 1, "write": 2, "admin": 3}
# An API path: segments of the characters repository, branch and label names use, never "..".
_PATH = r"(?:/(?!\.\.(?:/|$))[\w.:@+-]+)+"
# For a few seconds after a force update GitHub still shows a pull request's old head and refuses to merge it (405).
CATCH_UP_SECONDS = 3.0


class GitHubError(Exception):
    def __init__(self, message: str, *, status: int = 502):
        super().__init__(message)
        self.status = status


def _reason(response: httpx.Response) -> str:
    try:
        return str(response.json().get("message") or "")[:200]
    except ValueError:
        return ""


@dataclass(frozen=True)
class Comment:
    id: int
    author: str
    body: str
    created: str


@dataclass(frozen=True)
class PullRequest:
    number: int
    title: str
    body: str
    state: str
    merged: bool
    labels: tuple[str, ...]
    head: str
    head_sha: str
    url: str
    author: str
    created: str
    updated: str
    merged_at: str | None = None
    comments: tuple[Comment, ...] = field(default_factory=tuple)

    @classmethod
    def parse(cls, data: dict[str, Any]) -> PullRequest:
        return cls(
            number=int(data["number"]),
            title=str(data.get("title") or ""),
            body=str(data.get("body") or ""),
            state=str(data.get("state") or ""),
            merged=bool(data.get("merged") or data.get("merged_at") or (data.get("pull_request") or {}).get("merged_at")),
            labels=tuple(str(label.get("name")) for label in data.get("labels") or []),
            head=str((data.get("head") or {}).get("ref") or ""),
            head_sha=str((data.get("head") or {}).get("sha") or ""),
            url=str(data.get("html_url") or ""),
            author=str((data.get("user") or {}).get("login") or ""),
            created=str(data.get("created_at") or ""),
            updated=str(data.get("updated_at") or ""),
            merged_at=data.get("merged_at") or (data.get("pull_request") or {}).get("merged_at"),
        )


class GitHub:
    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        repo: str,
        branch: str = "main",
        root: str = "",
        token: str = "",
        app_id: str = "",
        private_key: str = "",
        api: str = "https://api.github.com",
        bot: str = "",
    ):
        self._http = http
        self.repo = repo
        self.branch = branch
        self.root = root.strip("/")
        self._token = token
        self._app_id = app_id
        self._private_key = private_key
        self._api = api.rstrip("/")
        self._bot = bot
        self._installation: tuple[str, float] = ("", 0.0)
        self._lock = asyncio.Lock()
        self._files: tuple[str, dict[str, str]] = ("", {})

    def path(self, relative: str) -> str:
        return f"{self.root}/{relative}" if self.root else relative

    async def _app_jwt(self) -> str:
        now = int(time.time())
        return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": self._app_id}, self._private_key, algorithm="RS256")

    async def _auth(self) -> str:
        if self._token:
            return f"Bearer {self._token}"
        async with self._lock:
            token, expires = self._installation
            if token and expires - 300 > time.time():
                return f"Bearer {token}"
            headers = {"Authorization": f"Bearer {await self._app_jwt()}", "Accept": "application/vnd.github+json"}
            try:
                found = await self._http.get(f"{self._api}/repos/{self.repo}/installation", headers=headers, timeout=15)
                if found.status_code != 200:
                    raise GitHubError(f"the GitHub App is not installed on {self.repo} (HTTP {found.status_code})")
                installation = found.json()
                held = installation.get("permissions") or {}
                missing = [f"{name}: {level}" for name, level in PERMISSIONS.items() if _LEVELS.get(held.get(name, ""), 0) < _LEVELS[level]]
                if missing:
                    raise GitHubError(f"the GitHub App's installation on {self.repo} lacks {', '.join(missing)}: grant it on the App and accept it on the installation")
                if not self._bot:
                    app = await self._http.get(f"{self._api}/app", headers=headers, timeout=15)
                    self._bot = f"{app.json().get('slug')}[bot]" if app.status_code == 200 else ""
                minted = await self._http.post(
                    f"{self._api}/app/installations/{installation['id']}/access_tokens",
                    headers=headers,
                    json={"repositories": [self.repo.split("/", 1)[1]], "permissions": PERMISSIONS},
                    timeout=15,
                )
            except httpx.HTTPError as err:
                raise GitHubError(f"GitHub did not answer: {err.__class__.__name__}") from err
            if minted.status_code != 201:
                raise GitHubError(f"GitHub refused an installation token for {self.repo} (HTTP {minted.status_code}): {_reason(minted)}")
            body = minted.json()
            self._installation = (str(body["token"]), time.time() + 3600)
            return f"Bearer {body['token']}"

    async def bot(self) -> str:
        """Whose comments count as this app's approval records."""
        if not self._bot:
            await self._auth()
        if not self._bot and self._token:
            self._bot = str((await self._call("GET", "/user")).get("login") or "")
        return self._bot

    async def _call(self, method: str, path: str, *, json: Any = None, params: dict[str, Any] | None = None, ok: tuple[int, ...] = (200, 201)) -> Any:
        if not re.fullmatch(_PATH, path):
            raise GitHubError(f"refused an odd GitHub API path: {path[:80]!r}", status=400)
        headers = {"Authorization": await self._auth(), "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        try:
            response = await self._http.request(method, f"{self._api}{path}", headers=headers, json=json, params=params, timeout=20)
        except httpx.HTTPError as err:
            raise GitHubError(f"GitHub did not answer: {err.__class__.__name__}") from err
        if response.status_code not in ok:
            raise GitHubError(f"GitHub answered HTTP {response.status_code} for {method} {path.split('?')[0]}: {_reason(response)}", status=response.status_code)
        return response.json() if response.content else {}

    # ── Reading the catalog ───────────────────────────────────────────────────────────

    async def head(self) -> str:
        return str((await self._call("GET", f"/repos/{self.repo}/git/ref/heads/{self.branch}"))["object"]["sha"])

    async def catalog_files(self) -> tuple[str, dict[str, str]]:
        """The commit and {path relative to the catalog root: text} of every YAML file in it."""
        sha = await self.head()
        if self._files[0] == sha:
            return self._files
        tree = await self._call("GET", f"/repos/{self.repo}/git/trees/{sha}", params={"recursive": "1"})
        prefix = f"{self.root}/" if self.root else ""
        wanted = [e for e in tree.get("tree", []) if e.get("type") == "blob" and e["path"].startswith(prefix) and e["path"].endswith((".yaml", ".yml"))]

        async def blob(entry: dict[str, Any]) -> tuple[str, str]:
            data = await self._call("GET", f"/repos/{self.repo}/git/blobs/{entry['sha']}")
            return entry["path"][len(prefix) :], base64.b64decode(data["content"]).decode()

        files = dict(await asyncio.gather(*(blob(e) for e in wanted)))
        self._files = (sha, files)
        return self._files

    # ── Writing ───────────────────────────────────────────────────────────────────────

    async def commit(self, branch: str, changes: dict[str, str | None], message: str, *, base: str | None = None, replace: bool = False) -> str:
        """One commit on top of `base` (default: the catalog branch's head) with every change in
        `changes` ({path relative to the root: text, or None to delete}), as `branch`. `replace`
        moves an existing branch there, whatever it held: the branches are this app's own."""
        base = base or await self.head()
        base_tree = (await self._call("GET", f"/repos/{self.repo}/git/commits/{base}"))["tree"]["sha"]
        entries = [
            {"path": self.path(p), "mode": "100644", "type": "blob", **({"content": text} if text is not None else {"sha": None})}
            for p, text in changes.items()
        ]
        tree = await self._call("POST", f"/repos/{self.repo}/git/trees", json={"base_tree": base_tree, "tree": entries})
        commit = await self._call("POST", f"/repos/{self.repo}/git/commits", json={"message": message, "tree": tree["sha"], "parents": [base]})
        if replace:
            await self._call("PATCH", f"/repos/{self.repo}/git/refs/heads/{branch}", json={"sha": commit["sha"], "force": True})
        else:
            await self._call("POST", f"/repos/{self.repo}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": commit["sha"]})
        return str(commit["sha"])

    async def open_pull(self, branch: str, title: str, body: str, labels: list[str]) -> PullRequest:
        data = await self._call("POST", f"/repos/{self.repo}/pulls", json={"title": title, "head": branch, "base": self.branch, "body": body, "maintainer_can_modify": False})
        if labels:
            await self._call("POST", f"/repos/{self.repo}/issues/{data['number']}/labels", json={"labels": labels})
            data["labels"] = [{"name": label} for label in labels]
        return PullRequest.parse(data)

    async def pulls(self, label: str, *, state: str = "open", limit: int = 100) -> list[PullRequest]:
        """Requests by label, newest first, without their comments or head. Issues and pull
        requests share this listing."""
        size = min(limit, 100)
        found: list[PullRequest] = []
        for page in range(1, -(-limit // size) + 1):
            data = await self._call("GET", f"/repos/{self.repo}/issues", params={"labels": label, "state": state, "per_page": size, "page": page, "sort": "created"})
            found += [PullRequest.parse(i) for i in data if "pull_request" in i]
            if len(data) < size:
                break
        return found

    async def pull(self, number: int, *, with_comments: bool = True) -> PullRequest:
        pull = PullRequest.parse(await self._call("GET", f"/repos/{self.repo}/pulls/{number}"))
        return PullRequest(**{**pull.__dict__, "comments": await self.comments(number)}) if with_comments else pull

    async def comments(self, number: int) -> tuple[Comment, ...]:
        data: list[dict[str, Any]] = []
        for page in range(1, 11):  # 1,000 comments at most, so a flood of them cannot make one read endless
            batch = await self._call("GET", f"/repos/{self.repo}/issues/{number}/comments", params={"per_page": 100, "page": page})
            data += batch
            if len(batch) < 100:
                break
        return tuple(Comment(int(c["id"]), str((c.get("user") or {}).get("login") or ""), str(c.get("body") or ""), str(c.get("created_at") or "")) for c in data)

    async def set_body(self, number: int, body: str) -> None:
        await self._call("PATCH", f"/repos/{self.repo}/pulls/{number}", json={"body": body})

    async def comment(self, number: int, body: str) -> None:
        await self._call("POST", f"/repos/{self.repo}/issues/{number}/comments", json={"body": body})

    async def label(self, number: int, add: list[str], remove: tuple[str, ...] = ()) -> None:
        if add:
            await self._call("POST", f"/repos/{self.repo}/issues/{number}/labels", json={"labels": add})
        for name in remove:
            await self._call("DELETE", f"/repos/{self.repo}/issues/{number}/labels/{name}", ok=(200, 404))

    async def approve(self, number: int, sha: str, body: str) -> None:
        """A pull request review of exactly `sha`, for a branch rule that wants one before merging."""
        await self._call("POST", f"/repos/{self.repo}/pulls/{number}/reviews", json={"event": "APPROVE", "body": body, "commit_id": sha}, ok=(200, 422))

    async def merge(self, number: int, sha: str, title: str, *, retry: bool = True) -> bool:
        """Merge exactly `sha` if the branch rules let this app; False when they still wait for something."""
        try:
            await self._call("PUT", f"/repos/{self.repo}/pulls/{number}/merge", json={"sha": sha, "merge_method": "squash", "commit_title": title})
        except GitHubError as err:
            if err.status == 405 and retry:
                await asyncio.sleep(CATCH_UP_SECONDS)
                return await self.merge(number, sha, title, retry=False)
            if err.status in (405, 409, 422):
                return False
            raise
        return True

    async def close(self, number: int) -> None:
        await self._call("PATCH", f"/repos/{self.repo}/pulls/{number}", json={"state": "closed"})

    async def open_issue(self, title: str, body: str) -> int:
        return int((await self._call("POST", f"/repos/{self.repo}/issues", json={"title": title, "body": body}))["number"])

    async def close_issue(self, number: int) -> None:
        await self._call("PATCH", f"/repos/{self.repo}/issues/{number}", json={"state": "closed", "state_reason": "not_planned"})

    async def delete_branch(self, branch: str) -> None:
        await self._call("DELETE", f"/repos/{self.repo}/git/refs/heads/{branch}", ok=(204, 404, 422))
