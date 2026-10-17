"""Everything the server reads from its environment, parsed and checked once at startup.

A value that would make the server unsafe or useless fails startup rather than the first
request: a pod in CrashLoopBackOff with the reason in its log is found in minutes.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_HOST = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
_COLOUR = re.compile(r"^#[0-9a-fA-F]{6}$")
_REPO = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")
_ROLE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}  # DevSkim: ignore DS162092 - plain http is allowed on loopback, for local development


class ConfigError(ValueError):
    """A configuration value the server refuses to start with."""


@dataclass(frozen=True)
class GitHubConfig:
    repo: str
    branch: str = "main"
    root: str = ""
    token: str = ""
    app_id: str = ""
    private_key: str = ""
    api: str = "https://api.github.com"
    bot: str = ""


@dataclass(frozen=True)
class Branding:
    """Colours as #rrggbb; empty keeps the default. The dark ones fall back to the light ones."""

    accent: str = ""
    accent_text: str = ""
    accent_dark: str = ""
    accent_text_dark: str = ""


@dataclass(frozen=True)
class Config:
    tenant_id: str
    client_id: str
    origin: str
    authority: str
    session_key: bytes
    session_seconds: int
    federated_token_file: Path | None = None
    client_secret: str | None = None
    # Exactly one of these: a repository (requests open pull requests) or a local directory
    # (read-only: browse and review, no requests).
    github: GitHubConfig | None = None
    catalog_dir: Path | None = None
    fulfilment: str = "pull-request"
    # How Microsoft Graph is read: "app" as the app itself, "delegated" as the signed-in person.
    directory: str = "app"
    auto_merge: bool = True
    group_prefixes: tuple[str, ...] = ()
    admin_role: str = "Admin"
    auditor_role: str = "Auditor"
    teams_webhook: str = ""
    static_dir: Path | None = None
    port: int = 8080
    version: str = "dev"
    branding: Branding = field(default_factory=Branding)

    @property
    def redirect_uri(self) -> str:
        return f"{self.origin}/auth/callback"

    @property
    def secure_cookies(self) -> bool:
        return self.origin.startswith("https://")

    @property
    def token_url(self) -> str:
        return f"{self.authority}/{self.tenant_id}/oauth2/v2.0/token"


def _guid(env: dict[str, str], name: str) -> str:
    value = env.get(name, "").strip().lower()
    if not _GUID.match(value):
        raise ConfigError(f"{name} must be a GUID")
    return value


def _origin(value: str, name: str) -> str:
    parts = urlsplit(value.strip())
    host = (parts.hostname or "").lower()
    if parts.path not in ("", "/") or parts.query or parts.fragment or parts.username or not host:
        raise ConfigError(f"{name} must be scheme://host[:port], with no path")
    if parts.scheme != "https" and not (parts.scheme == "http" and host in _LOOPBACK):
        raise ConfigError(f"{name} must be https (plain http only on a loopback address)")
    return f"{parts.scheme}://{parts.netloc.lower()}"


def _authority(value: str) -> str:
    parts = urlsplit(value.strip() or "https://login.microsoftonline.com")
    if parts.scheme != "https" or not _HOST.match((parts.hostname or "").lower()) or parts.path not in ("", "/"):
        raise ConfigError("AZURE_AUTHORITY_HOST must be an https host, such as https://login.microsoftonline.com")
    return f"https://{parts.netloc.lower()}"


def _secret(env: dict[str, str], name: str) -> str:
    """NAME, or the contents of the file NAME_FILE points at."""
    value = env.get(name, "")
    if not value and env.get(f"{name}_FILE"):
        try:
            value = Path(env[f"{name}_FILE"]).read_text().strip()
        except OSError as err:
            raise ConfigError(f"cannot read {name}_FILE: {err.__class__.__name__}") from err
    return value


def _github(env: dict[str, str]) -> GitHubConfig:
    repo = env.get("PERMISSIONS_UI_GITHUB_REPO", "").strip()
    if not _REPO.match(repo):
        raise ConfigError("PERMISSIONS_UI_GITHUB_REPO must be owner/name, the repository that holds the catalog")
    token = _secret(env, "PERMISSIONS_UI_GITHUB_TOKEN")
    app_id = env.get("PERMISSIONS_UI_GITHUB_APP_ID", "").strip()
    key = _secret(env, "PERMISSIONS_UI_GITHUB_APP_PRIVATE_KEY")
    if bool(app_id) != bool(key):
        raise ConfigError("a GitHub App needs both PERMISSIONS_UI_GITHUB_APP_ID and PERMISSIONS_UI_GITHUB_APP_PRIVATE_KEY (or _FILE)")
    if not token and not app_id:
        raise ConfigError("set PERMISSIONS_UI_GITHUB_APP_ID and PERMISSIONS_UI_GITHUB_APP_PRIVATE_KEY (preferred) or PERMISSIONS_UI_GITHUB_TOKEN")
    if app_id and not app_id.isdigit():
        raise ConfigError("PERMISSIONS_UI_GITHUB_APP_ID is the App's numeric id")
    api = env.get("PERMISSIONS_UI_GITHUB_API", "https://api.github.com").strip().rstrip("/")
    if not api.startswith("https://"):
        raise ConfigError("PERMISSIONS_UI_GITHUB_API must be https, such as https://api.github.com")
    root = env.get("PERMISSIONS_UI_CATALOG_PATH", "").strip().strip("/")
    if ".." in root.split("/"):
        raise ConfigError("PERMISSIONS_UI_CATALOG_PATH is a folder in the repository, without ..")
    return GitHubConfig(
        repo=repo,
        branch=env.get("PERMISSIONS_UI_GITHUB_BRANCH", "main").strip() or "main",
        root=root,
        token=token,
        app_id=app_id,
        private_key=key,
        api=api,
        bot=env.get("PERMISSIONS_UI_GITHUB_BOT", "").strip(),
    )


def load_config(env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ if env is None else env)

    token_file = env.get("AZURE_FEDERATED_TOKEN_FILE", "").strip()
    secret = _secret(env, "AZURE_CLIENT_SECRET")
    if not token_file and not secret:
        raise ConfigError("set AZURE_FEDERATED_TOKEN_FILE (workload identity), AZURE_CLIENT_SECRET or AZURE_CLIENT_SECRET_FILE")

    session_key = _secret(env, "PERMISSIONS_UI_SESSION_KEY")
    if len(session_key) < 32:
        raise ConfigError("PERMISSIONS_UI_SESSION_KEY (or PERMISSIONS_UI_SESSION_KEY_FILE) must hold at least 32 characters")

    catalog_dir = env.get("PERMISSIONS_UI_CATALOG_DIR", "").strip()
    has_repo = bool(env.get("PERMISSIONS_UI_GITHUB_REPO", "").strip())
    if bool(catalog_dir) == has_repo:
        raise ConfigError("set PERMISSIONS_UI_GITHUB_REPO (requests open pull requests there) or PERMISSIONS_UI_CATALOG_DIR (read-only), not both")
    if catalog_dir and not Path(catalog_dir).is_dir():
        raise ConfigError("PERMISSIONS_UI_CATALOG_DIR is not a directory")

    fulfilment = env.get("PERMISSIONS_UI_FULFILMENT", "pull-request").strip()
    if fulfilment not in ("pull-request", "graph"):
        raise ConfigError("PERMISSIONS_UI_FULFILMENT takes pull-request or graph")
    merge = env.get("PERMISSIONS_UI_MERGE", "auto").strip()
    if merge not in ("auto", "manual"):
        raise ConfigError("PERMISSIONS_UI_MERGE takes auto or manual")
    if fulfilment == "graph" and merge == "manual":
        raise ConfigError("PERMISSIONS_UI_FULFILMENT=graph applies on approval, so it needs PERMISSIONS_UI_MERGE=auto")
    directory = env.get("PERMISSIONS_UI_DIRECTORY", "app").strip()
    if directory not in ("app", "delegated"):
        raise ConfigError("PERMISSIONS_UI_DIRECTORY takes app or delegated")
    if fulfilment == "graph" and directory == "delegated":
        raise ConfigError("PERMISSIONS_UI_FULFILMENT=graph changes groups as the app, so it needs PERMISSIONS_UI_DIRECTORY=app")

    roles = {}
    for key, default in (("admin", "Admin"), ("auditor", "Auditor")):
        name = f"PERMISSIONS_UI_{key.upper()}_ROLE"
        roles[f"{key}_role"] = env.get(name, default).strip()
        if not _ROLE.match(roles[f"{key}_role"]):
            raise ConfigError(f"{name} must be an app role value, such as {default}")

    webhook = _secret(env, "PERMISSIONS_UI_TEAMS_WEBHOOK").strip()
    if webhook and not webhook.startswith("https://"):
        raise ConfigError("PERMISSIONS_UI_TEAMS_WEBHOOK must be an https URL")

    try:
        hours = int(env.get("PERMISSIONS_UI_SESSION_HOURS", "8"))
        port = int(env.get("PORT", "8080"))
    except ValueError as err:
        raise ConfigError("PERMISSIONS_UI_SESSION_HOURS and PORT must be whole numbers") from err
    if not 1 <= hours <= 24:
        raise ConfigError("PERMISSIONS_UI_SESSION_HOURS must be between 1 and 24")

    if "PERMISSIONS_UI_PUBLIC_ORIGIN" not in env:
        raise ConfigError("PERMISSIONS_UI_PUBLIC_ORIGIN is required, such as https://access.example.com")
    static = env.get("PERMISSIONS_UI_STATIC_DIR", "/app/static")

    colours = {}
    for key in ("accent", "accent_text", "accent_dark", "accent_text_dark"):
        name = f"PERMISSIONS_UI_BRAND_{key.upper()}"
        colours[key] = env.get(name, "").strip().lower()
        if colours[key] and not _COLOUR.match(colours[key]):
            raise ConfigError(f"{name} must be a colour as #rrggbb, such as #0f766e")

    return Config(
        tenant_id=_guid(env, "AZURE_TENANT_ID"),
        client_id=_guid(env, "AZURE_CLIENT_ID"),
        authority=_authority(env.get("AZURE_AUTHORITY_HOST", "")),
        federated_token_file=Path(token_file) if token_file else None,
        client_secret=secret or None,
        origin=_origin(env["PERMISSIONS_UI_PUBLIC_ORIGIN"], "PERMISSIONS_UI_PUBLIC_ORIGIN"),
        session_key=session_key.encode(),
        session_seconds=hours * 3600,
        github=_github(env) if has_repo else None,
        catalog_dir=Path(catalog_dir) if catalog_dir else None,
        fulfilment=fulfilment,
        directory=directory,
        auto_merge=merge == "auto",
        group_prefixes=tuple(p.strip().lower() for p in env.get("PERMISSIONS_UI_GROUP_PREFIXES", "").split(",") if p.strip()),
        teams_webhook=webhook,
        static_dir=Path(static) if static and Path(static).is_dir() else None,
        port=port,
        version=env.get("APP_VERSION", "dev"),
        branding=Branding(**colours),
        **roles,
    )
