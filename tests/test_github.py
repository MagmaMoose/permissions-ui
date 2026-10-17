import asyncio
import json

import httpx
import pytest
from conftest import GITHUB_API, KEY, REPO
from cryptography.hazmat.primitives import serialization

from permissions_ui.github import PERMISSIONS, GitHub, GitHubError

PEM = KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()


def installed(held: dict[str, str]) -> tuple[GitHub, list[httpx.Request]]:
    """A GitHub App installed on the catalog repository with these permissions."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        if path == f"/repos/{REPO}/installation":
            return httpx.Response(200, json={"id": 42, "permissions": held})
        if path == "/app":
            return httpx.Response(200, json={"slug": "access-portal"})
        if path == "/app/installations/42/access_tokens":
            return httpx.Response(201, json={"token": "ghs_scoped", "expires_at": "2026-10-17T11:00:00Z"})
        return httpx.Response(404, json={"message": "Not Found"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return GitHub(http, repo=REPO, app_id="123", private_key=PEM, api=GITHUB_API), calls


def test_the_installation_token_is_held_to_the_catalog_repository():
    github, calls = installed({**PERMISSIONS, "actions": "write", "administration": "write"})
    assert asyncio.run(github._auth()) == "Bearer ghs_scoped"
    assert asyncio.run(github.bot()) == "access-portal[bot]"
    minted = next(c for c in calls if c.url.path.endswith("/access_tokens"))
    assert json.loads(minted.content) == {
        "repositories": ["access"],
        "permissions": {"contents": "write", "pull_requests": "write", "issues": "write", "metadata": "read"},
    }


@pytest.mark.parametrize(
    ("held", "missing"),
    [
        ({**PERMISSIONS, "pull_requests": "read"}, "pull_requests: write"),
        ({k: v for k, v in PERMISSIONS.items() if k != "issues"}, "issues: write"),
    ],
)
def test_an_installation_without_a_permission_it_needs_is_refused(held, missing):
    github, calls = installed(held)
    with pytest.raises(GitHubError, match=f"installation on {REPO} lacks {missing}"):
        asyncio.run(github._auth())
    assert not any(c.url.path.endswith("/access_tokens") for c in calls)
