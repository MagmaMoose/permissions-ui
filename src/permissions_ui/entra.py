"""Signing in with Entra ID, as a confidential client (the flow itself is in oidc.py), and the
app's own credential for Microsoft Graph.

The credential is a workload identity token (AZURE_FEDERATED_TOKEN_FILE, which Entra trusts
through a federated credential on the app registration) or a client secret. The sign-in asks
who the person is; their app roles (Admin, Auditor) come in the ID token. In the delegated
directory mode it also asks for offline_access and Microsoft Graph's delegated User.Read.All and
GroupMember.Read.All, so the directory is read as the person.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from .config import Config
from .oidc import OIDC, SignIn, SignInError

_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
GRAPH_SCOPE = "https://graph.microsoft.com/User.Read.All https://graph.microsoft.com/GroupMember.Read.All"


def app_credential(cfg: Config) -> dict[str, str]:
    """The app's credential on Entra ID's token endpoint, read fresh each time."""
    if cfg.federated_token_file is None:
        return {"client_secret": cfg.client_secret or ""}
    try:
        # The kubelet rotates the projected token in place.
        assertion = cfg.federated_token_file.read_text().strip()
    except OSError as err:
        raise SignInError(f"cannot read the workload identity token: {err.__class__.__name__}", status=500) from err
    return {"client_assertion_type": _ASSERTION_TYPE, "client_assertion": assertion}


class Entra(OIDC):
    name = "Microsoft"

    def __init__(self, cfg: Config, http: httpx.AsyncClient):
        base = f"{cfg.authority}/{cfg.tenant_id}"
        super().__init__(
            http,
            client_id=cfg.client_id,
            redirect_uri=cfg.redirect_uri,
            scope="openid profile" if cfg.directory == "app" else f"openid profile offline_access {GRAPH_SCOPE}",
            endpoints={"authorize": f"{base}/oauth2/v2.0/authorize", "token": f"{base}/oauth2/v2.0/token", "jwks": f"{base}/discovery/v2.0/keys"},
            issuers=(f"{base}/v2.0",),
        )
        self._cfg = cfg

    def _credential(self) -> dict[str, str]:
        return app_credential(self._cfg)

    def _check_claims(self, claims: dict[str, Any]) -> None:
        if str(claims.get("tid", "")).lower() != self._cfg.tenant_id or not claims.get("oid"):
            raise SignInError("that account belongs to another tenant", status=401)

    async def _sign_in(self, body: dict[str, Any], claims: dict[str, Any]) -> SignIn:
        if self._cfg.directory == "delegated" and not (body.get("refresh_token") and body.get("access_token")):
            raise SignInError("Microsoft returned no refresh or Graph token; has the app been granted offline_access, User.Read.All and GroupMember.Read.All (delegated)?")
        return SignIn(
            oid=str(claims["oid"]).lower(),
            name=str(claims.get("name") or "")[:120],
            upn=str(claims.get("preferred_username") or "").lower()[:120],
            roles=[r for r in claims.get("roles") or [] if isinstance(r, str)],
            access_token=str(body.get("access_token") or ""),
            expires_at=time.time() + int(body.get("expires_in") or 3600),
            refresh_token=str(body.get("refresh_token") or ""),
        )
