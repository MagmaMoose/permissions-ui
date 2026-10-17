"""The web app: sign-in routes, the JSON API the frontend calls, and the built frontend itself.

Writes need the session's CSRF token in X-CSRF-Token and this origin in Origin, on top of the
SameSite=Strict session cookie. Who may see everyone's access: admins, auditors and team leads;
everyone else sees the catalog, their own access and the requests that involve them.
"""

import asyncio
import hmac
import json
import logging
import re
import secrets
from collections.abc import Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import date
from typing import Annotated, Any, Self
from urllib.parse import urlencode

import httpx
from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from . import insights
from .catalog import Catalog, CatalogError
from .config import Branding, Config
from .directory import AppToken, Directory, DirectoryError
from .entra import GRAPH_SCOPE, Entra, app_credential
from .github import GitHub, GitHubError
from .notify import Notifier
from .oidc import SignInError
from .requests import Records
from .requests import Request as AccessRequest
from .service import Actor, LocalSource, Portal, PortalError
from .session import Cookies, Sealer, TokenCache

log = logging.getLogger("permissions_ui")

LOGIN_SECONDS = 600
SOURCE = "https://github.com/MagmaMoose/permissions-ui"
_UPN = r"^[^@\s/\\]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$"
# The session of the request being served: in the delegated directory mode its refresh token
# reads the directory, and its person keys the directory cache.
_session: ContextVar[dict[str, Any] | None] = ContextVar("permissions_ui_session", default=None)

CSP = "; ".join(
    [
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "manifest-src 'self'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)


def release_notes(version: str) -> str | None:
    """Where a version's changes are written up: its GitHub release, or the pull request it was built from."""
    if re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?", version):
        return f"{SOURCE}/releases/tag/{version}"
    if found := re.fullmatch(r"pr-([0-9]+)", version):
        return f"{SOURCE}/pull/{found.group(1)}"
    return None


def theme_css(brand: Branding) -> str:
    """The operator's colours over styles.css, whose `:root` loses to `html:root` in any order."""

    def rules(accent: str, text: str, soft: int) -> str:
        found = [f"--accent:{accent}", f"--accent-soft:color-mix(in srgb,{accent} {soft}%,var(--surface))"] if accent else []
        return ";".join(found + ([f"--accent-text:{text}"] if text else []))

    light = rules(brand.accent, brand.accent_text, 10)
    dark = rules(brand.accent_dark or brand.accent, brand.accent_text_dark or brand.accent_text, 14)
    return (f"html:root{{{light}}}\n" if light else "") + (f"@media (prefers-color-scheme: dark){{html:root{{{dark}}}}}\n" if dark else "")


def favicon_svg(brand: Branding) -> str:
    """The tab icon in the operator's accent. Unbranded, it is frontend/public/favicon.svg."""
    tile, glyph = brand.accent or "#0f766e", brand.accent_text or "#fff"
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="7" fill="{tile}"/>'
        f'<circle cx="16" cy="11" r="4.5" fill="none" stroke="{glyph}" stroke-width="2.5"/>'
        f'<path d="M8 25c1-5 4.5-7.5 8-7.5s7 2.5 8 7.5" fill="none" stroke="{glyph}" stroke-width="2.5" stroke-linecap="round"/></svg>'
    )


class Item(BaseModel):
    role: str | None = Field(default=None, max_length=64)
    resource: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _one(self) -> Self:
        if bool(self.role) == bool(self.resource):
            raise ValueError("an item names a role or a resource")
        return self

    def plain(self) -> dict[str, str]:
        return {"role": str(self.role)} if self.role else {"resource": str(self.resource)}


class NewRequest(BaseModel):
    person: str = Field(pattern=_UPN, max_length=120)
    items: list[Item] = Field(min_length=1, max_length=40)
    reason: str = Field(min_length=3, max_length=500)
    expires: date | None = None


class Removal(BaseModel):
    person: str = Field(pattern=_UPN, max_length=120)
    items: list[Item] | None = Field(default=None, max_length=200)
    reason: str = Field(min_length=3, max_length=500)


class Reason(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class Done(BaseModel):
    resource: str = Field(min_length=1, max_length=64)


def _log(event: str, **fields: Any) -> None:
    log.info(json.dumps({"msg": event, **fields}))


def resource_json(catalog: Catalog, rid: str, today: date) -> dict[str, Any]:
    r = catalog.resources[rid]
    return {
        "id": r.id,
        "name": r.name,
        "description": r.description,
        "owner": r.owner,
        "kind": r.kind,
        "target": r.target,
        "tier": r.tier,
        "approvers": r.approvers or [f"team:{r.owner}"],
        "approvals": r.required_approvals,
        "max_days": r.max_days,
        "incompatible": r.incompatible,
        "tags": r.tags,
        "instructions": r.instructions,
        "fulfillers": r.fulfillers,
        "roles": sorted(role.id for role in catalog.roles.values() if rid in role.resources),
        "holders": len(catalog.holders(rid, today)),
    }


def names_of(catalog: Catalog, upns: Iterable[str]) -> dict[str, str]:
    """Display names, from the grants files, of the people a response mentions."""
    return {upn: p.name for upn in upns if (p := catalog.people.get(upn)) is not None and p.name}


def request_json(catalog: Catalog, req: AccessRequest, *, can_approve: bool = False, can_decline: bool = False) -> dict[str, Any]:
    names = {}
    for item in req.items:
        if item.get("role"):
            role = catalog.roles.get(item["role"])
            names[f"role:{item['role']}"] = role.name if role else item["role"]
        else:
            resource = catalog.resources.get(item.get("resource", ""))
            names[f"resource:{item.get('resource')}"] = resource.name if resource else item.get("resource", "")
    resources = []
    try:
        resources = catalog.resources_of(req.items)
    except KeyError:
        pass
    return {
        "number": req.number,
        "url": req.url,
        "kind": req.kind,
        "person": req.person,
        # The request record has the name of someone the catalog does not hold yet.
        "names": {**({req.person: req.person_name} if req.person_name else {}), **names_of(catalog, (req.person, req.requested_by))},
        "items": [{**i, "name": names.get(f"role:{i['role']}" if i.get("role") else f"resource:{i.get('resource')}", "")} for i in req.items],
        "resources": [{"id": rid, "name": catalog.resources[rid].name, "tier": catalog.resources[rid].tier, "kind": catalog.resources[rid].kind} for rid in resources if rid in catalog.resources],
        "reason": req.reason,
        "requested_by": req.requested_by,
        "expires": req.expires,
        "created": req.created,
        "state": req.state,
        "approvals": [{"upn": a.upn, "name": a.name, "at": a.at, "resources": list(a.resources)} for a in req.approvals],
        "outstanding": [{"resource": n.resource, "approvers": list(n.approvers), "count": n.count} for n in req.outstanding],
        "declined_by": req.declined_by,
        "decline_reason": req.decline_reason,
        "done": list(req.done),
        "can_approve": can_approve,
        "can_decline": can_decline,
    }


def _reader() -> str:
    return str((_session.get() or {}).get("oid", ""))


async def _nobody() -> str:
    raise DirectoryError("the delegated directory mode reads Microsoft Graph as a signed-in person, and nobody is signed in here")


def build_portal(cfg: Config, http: httpx.AsyncClient, *, today=date.today, person_token: Callable[[], Awaitable[str]] | None = None) -> Portal:
    """`person_token` reads the directory as the signed-in person in the delegated mode. Without
    one, as in the expire job, nothing may read it; pull-request fulfilment never does."""
    if cfg.directory == "app":
        directory = Directory(http, token=AppToken(http, token_url=cfg.token_url, client_id=cfg.client_id, credential=lambda: app_credential(cfg)))
    else:
        directory = Directory(http, token=person_token or _nobody, reader=_reader)
    github = None
    if cfg.github is not None:
        gh = cfg.github
        github = GitHub(http, repo=gh.repo, branch=gh.branch, root=gh.root, token=gh.token, app_id=gh.app_id, private_key=gh.private_key, api=gh.api, bot=gh.bot)
    source = github.catalog_files if github is not None else LocalSource(cfg.catalog_dir)  # type: ignore[arg-type]
    return Portal(
        source=source,
        github=github,
        directory=directory,
        notifier=Notifier(http, cfg.teams_webhook),
        origin=cfg.origin,
        records=Records(cfg.record_keys),
        fulfilment=cfg.fulfilment,
        auto_merge=cfg.auto_merge,
        today=today,
    )


def create_app(cfg: Config, transport: httpx.AsyncBaseTransport | None = None, *, today=date.today) -> FastAPI:
    http = httpx.AsyncClient(transport=transport, follow_redirects=False)
    idp = Entra(cfg, http)
    cookies = Cookies(Sealer(cfg.session_key), secure=cfg.secure_cookies)
    delegated = cfg.directory == "delegated"
    tokens = TokenCache()
    refreshing = asyncio.Lock()

    async def person_token() -> str:
        """The signed-in person's Graph token, redeemed from their session's refresh token."""
        data = _session.get() or {}
        key = str(data.get("sid", ""))
        if (token := tokens.get(key)) is not None:
            return token
        async with refreshing:
            if (token := tokens.get(key)) is not None:
                return token
            try:
                token, expires_at = await idp.refresh(str(data.get("rt", "")), scope=GRAPH_SCOPE)
            except SignInError as err:
                _log("graph token failed", user=data.get("oid"), status=err.status, reason=str(err))
                if err.status == 401:
                    raise HTTPException(401, "signed_out") from err
                raise DirectoryError(f"cannot get your Microsoft Graph token: {err}") from err
            tokens.put(key, token, expires_at)
            return token

    portal = build_portal(cfg, http, today=today, person_token=person_token if delegated else None)
    directory = portal.directory

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        await http.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.portal = portal

    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        if cfg.secure_cookies:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        if path.startswith("/assets/"):
            # Vite names every asset by its content hash.
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif path.startswith(("/api/", "/auth/")):
            response.headers["Cache-Control"] = "no-store"
        else:
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid(_: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [{"field": ".".join(str(p) for p in e.get("loc", ())[1:]), "message": e.get("msg", "")} for e in exc.errors()]
        return JSONResponse({"error": "invalid_request", "message": "The request is not valid.", "fields": fields}, 422)

    @app.exception_handler(PortalError)
    async def portal_error(_: Request, exc: PortalError) -> JSONResponse:
        return JSONResponse({"error": exc.code, "message": str(exc), **exc.details}, exc.status)

    @app.exception_handler(CatalogError)
    async def catalog_error(_: Request, exc: CatalogError) -> JSONResponse:
        _log("catalog invalid", problems=exc.problems[:20])
        return JSONResponse({"error": "catalog_invalid", "message": "The catalog does not hold together.", "problems": exc.problems[:50]}, 503)

    @app.exception_handler(DirectoryError)
    async def directory_error(_: Request, exc: DirectoryError) -> JSONResponse:
        _log("directory failed", reason=str(exc))
        return JSONResponse({"error": "directory_unavailable", "message": str(exc)}, 502 if exc.status != 403 else 503)

    @app.exception_handler(GitHubError)
    async def github_error(_: Request, exc: GitHubError) -> JSONResponse:
        _log("github failed", status=exc.status, reason=str(exc))
        return JSONResponse({"error": "repository_unavailable", "message": str(exc)}, 502)

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"error": exc.detail}, exc.status_code)

    # ── Sessions ──────────────────────────────────────────────────────────────────────

    async def session(request: Request) -> dict[str, Any]:
        # async, so it runs in the route's own task and the ContextVar reaches the directory.
        data = cookies.read_session(request)
        if data is None or (delegated and not data.get("rt")):
            raise HTTPException(401, "signed_out")
        _session.set(data)
        return data

    Session = Annotated[dict[str, Any], Depends(session)]

    def writable(request: Request, data: Session) -> dict[str, Any]:
        origin = request.headers.get("origin", "").encode()
        token = request.headers.get("x-csrf-token", "").encode()
        if not hmac.compare_digest(origin, cfg.origin.encode()) or not hmac.compare_digest(token, str(data.get("csrf", "")).encode()):
            raise HTTPException(403, "csrf")
        return data

    Writable = Annotated[dict[str, Any], Depends(writable)]
    Number = Annotated[int, Path(ge=1, le=10_000_000)]

    def actor(data: dict[str, Any]) -> Actor:
        roles = data.get("roles") or []
        return Actor(oid=str(data["oid"]), upn=str(data["upn"]), name=str(data.get("name", "")), admin=cfg.admin_role in roles, auditor=cfg.auditor_role in roles)

    async def everyone(data: dict[str, Any]) -> tuple[Catalog, Actor]:
        catalog = await portal.catalog()
        who = actor(data)
        if not portal.sees_everyone(catalog, who):
            raise HTTPException(403, "not_allowed")
        return catalog, who

    # ── Sign-in ───────────────────────────────────────────────────────────────────────

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    theme = theme_css(cfg.branding)
    favicon = favicon_svg(cfg.branding)

    @app.get("/theme.css")
    async def theme_stylesheet() -> Response:
        return Response(theme, media_type="text/css")

    @app.get("/favicon.svg")
    async def favicon_image() -> Response:
        return Response(favicon, media_type="image/svg+xml")

    @app.get("/auth/login")
    async def login() -> Response:
        state, nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(64)
        response = RedirectResponse(idp.authorize_url(state=state, nonce=nonce, verifier=verifier), 302)
        cookies.write_login(response, {"state": state, "nonce": nonce, "verifier": verifier}, LOGIN_SECONDS)
        return response

    def _back(**query: str) -> Response:
        response = RedirectResponse("/" + (f"?{urlencode(query)}" if query else ""), 303)
        cookies.clear(response, cookies.login)
        return response

    @app.get("/auth/callback")
    async def callback(request: Request) -> Response:
        params = request.query_params
        if params.get("error"):
            reason = (params.get("error_description") or params["error"]).splitlines()[0][:300]
            _log("sign-in refused", reason=reason)
            return _back(auth_error=reason)
        flow = cookies.read_login(request)
        if flow is None or not hmac.compare_digest(params.get("state", "").encode(), str(flow.get("state", "")).encode()):
            return _back(auth_error="This sign-in expired or was not started here. Sign in again.")
        try:
            signed = await idp.redeem_code(code=params.get("code", ""), verifier=str(flow["verifier"]), nonce=str(flow["nonce"]))
        except SignInError as err:
            _log("sign-in failed", status=err.status, reason=str(err))
            return _back(auth_error=str(err))
        data = {"oid": signed.oid, "name": signed.name, "roles": signed.roles, "csrf": secrets.token_urlsafe(32)}
        if delegated:
            data |= {"sid": secrets.token_urlsafe(16), "rt": signed.refresh_token}
            tokens.put(data["sid"], signed.access_token, signed.expires_at)
        # The catalog and Graph name people by their userPrincipalName here. A guest's ID token
        # names them by their home tenant's name instead, which matches nothing.
        _session.set(data)
        try:
            user = await directory.user(signed.oid)
        except DirectoryError as err:
            _log("sign-in lookup failed", user=signed.oid, reason=str(err))
            return _back(auth_error=f"Cannot find you in the directory: {err}")
        if user is None or not user.upn:
            return _back(auth_error="That account is not in the directory.")
        data["upn"] = user.upn
        response = _back()
        cookies.write_session(request, response, data, cfg.session_seconds)
        _log("signed in", user=signed.oid, roles=signed.roles)
        return response

    @app.post("/auth/logout")
    async def logout(request: Request, data: Writable) -> Response:
        tokens.drop(str(data.get("sid", "")))
        response = JSONResponse({"signed_out": True})
        cookies.clear_session(request, response)
        return response

    # ── API ───────────────────────────────────────────────────────────────────────────

    @app.get("/api/me")
    async def me(data: Session) -> dict[str, Any]:
        who = actor(data)
        problems: list[str] = []
        lead_of: list[str] = []
        sees = who.admin or who.auditor
        try:
            catalog = await portal.catalog()
            lead_of = sorted(t.id for t in catalog.teams.values() if who.upn in t.leads)
            sees = portal.sees_everyone(catalog, who)
        except CatalogError as err:
            problems = err.problems[:50]
        return {
            "name": who.name,
            "upn": who.upn,
            "csrf": data.get("csrf", ""),
            "expires": data.get("exp"),
            "version": cfg.version,
            "release_notes": release_notes(cfg.version),
            "admin": who.admin,
            "auditor": who.auditor,
            "lead_of": lead_of,
            "sees_everyone": sees,
            "read_only": portal.read_only,
            "fulfilment": cfg.fulfilment,
            "repository": f"https://github.com/{cfg.github.repo}" if cfg.github and cfg.github.api == "https://api.github.com" else None,
            "catalog_problems": problems,
        }

    @app.get("/api/catalog")
    async def catalog_view(data: Session) -> dict[str, Any]:
        catalog = await portal.catalog()
        today = portal.today()
        return {
            "resources": [resource_json(catalog, rid, today) for rid in sorted(catalog.resources)],
            "roles": [
                {"id": r.id, "name": r.name, "description": r.description, "resources": r.resources, "approvers": r.approvers, "max_days": r.max_days,
                 "teams": sorted(t.id for t in catalog.teams.values() if r.id in t.roles)}
                for r in sorted(catalog.roles.values(), key=lambda r: r.name.lower())
            ],
            "teams": [
                {"id": t.id, "name": t.name, "description": t.description, "leads": t.leads, "roles": t.roles}
                for t in sorted(catalog.teams.values(), key=lambda t: t.name.lower())
            ],
        }

    @app.get("/api/overview")
    async def overview(data: Session) -> dict[str, Any]:
        catalog, _ = await everyone(data)
        today = portal.today()
        people = sorted(catalog.people)
        views = [insights.person_view(catalog, upn, today) for upn in people]
        drift = await insights.all_drift(catalog, directory, today)
        undocumented = await insights.undocumented(catalog, directory, cfg.group_prefixes)
        open_requests = [r for r in await portal.requests(include_closed=False) if r.state in ("pending", "approved", "open")]
        return {
            "people": len(people),
            "resources": {t: sum(1 for r in catalog.resources.values() if r.tier == t) for t in ("standard", "elevated", "privileged")},
            "roles": len(catalog.roles),
            "teams": len(catalog.teams),
            "pending": len(open_requests),
            "privileged_people": sorted(({"upn": v.upn, "privileged": list(v.privileged)} for v in views if v.privileged), key=lambda x: -len(x["privileged"]))[:20],
            "expiring": sorted(({"upn": v.upn, "resource": rid, "expires": exp} for v in views for rid, exp in v.expiring), key=lambda x: x["expires"])[:50],
            "conflicts": [{"upn": v.upn, "pairs": [list(p) for p in v.conflicts]} for v in views if v.conflicts],
            "drift": [
                {"resource": d.resource, "name": catalog.resources[d.resource].name, "group_found": d.group_found, "missing": list(d.missing), "extra": list(d.extra)}
                for d in drift
                if d.missing or d.extra or not d.group_found
            ],
            "undocumented": [{"id": g.id, "name": g.name, "description": g.description} for g in undocumented],
            "group_prefixes": list(cfg.group_prefixes),
            "names": {**{upn: name for d in drift for upn, name in d.names.items()}, **names_of(catalog, people)},
        }

    @app.get("/api/people")
    async def people(data: Session, q: Annotated[str, Query(max_length=80)] = "") -> dict[str, Any]:
        catalog = await portal.catalog()
        who = actor(data)
        today = portal.today()
        sees = portal.sees_everyone(catalog, who)
        upns = sorted(catalog.people) if sees else [who.upn]
        rows = []
        for upn in upns:
            view = insights.person_view(catalog, upn, today)
            rows.append({"upn": upn, "tiers": view.tiers, "privileged": len(view.privileged), "expiring": len(view.expiring), "conflicts": len(view.conflicts),
                         "grants": len(catalog.people[upn].grants) if upn in catalog.people else 0})
        found = []
        if q and sees:
            found = [{"upn": u.upn, "name": u.name, "job": u.job, "department": u.department, "in_catalog": u.upn in catalog.people} for u in await directory.search_users(q)]
        return {"people": rows, "directory": found, "names": {**({who.upn: who.name} if who.name else {}), **names_of(catalog, upns)}}

    @app.get("/api/people/{upn}")
    async def person(upn: Annotated[str, Path(pattern=_UPN, max_length=120)], data: Session) -> dict[str, Any]:
        catalog = await portal.catalog()
        who = actor(data)
        upn = upn.lower()
        if upn != who.upn and not portal.sees_everyone(catalog, who):
            raise HTTPException(403, "not_allowed")
        today = portal.today()
        user = await directory.user(upn)
        view = insights.person_view(catalog, upn, today)
        if user is not None:
            view = await insights.person_actual(catalog, directory, view, user.id, today, cfg.group_prefixes)
        holdings = catalog.holdings(upn, today)
        grants = catalog.people[upn].grants if upn in catalog.people else []
        return {
            "upn": upn,
            "name": catalog.people[upn].name if upn in catalog.people else "",
            "user": None if user is None else {"name": user.name, "mail": user.mail, "job": user.job, "department": user.department, "enabled": user.enabled},
            "teams": sorted(t.id for t in catalog.teams.values() if upn in t.leads),
            "grants": [
                {"role": g.role, "resource": g.resource, "reason": g.reason, "granted": g.granted.isoformat(), "expires": g.expires.isoformat() if g.expires else None,
                 "approved_by": g.approved_by, "request": g.request, "active": g.active(today)}
                for g in grants
            ],
            "holdings": [
                {"resource": rid, "name": h.resource.name, "tier": h.resource.tier, "kind": h.resource.kind,
                 "via": [g.role or "direct" for g in h.via], "expires": h.expires.isoformat() if h.expires else None}
                for rid, h in sorted(holdings.items(), key=lambda x: (-("standard", "elevated", "privileged").index(x[1].resource.tier), x[0]))
            ],
            "tiers": view.tiers,
            "conflicts": [list(p) for p in view.conflicts],
            "expiring": [{"resource": r, "expires": e} for r, e in view.expiring],
            "extra": list(view.extra_groups),
            "missing": list(view.missing),
            "unknown_groups": list(view.unknown_groups),
        }

    @app.get("/api/resources/{rid}")
    async def resource(rid: Annotated[str, Path(max_length=64)], data: Session) -> dict[str, Any]:
        catalog = await portal.catalog()
        if rid not in catalog.resources:
            raise HTTPException(404, "unknown_resource")
        today = portal.today()
        out = resource_json(catalog, rid, today)
        if portal.sees_everyone(catalog, actor(data)):
            out["holder_list"] = [{"upn": upn, "via": [g.role or "direct" for g in h.via], "expires": h.expires.isoformat() if h.expires else None}
                                  for upn, h in sorted(catalog.holders(rid, today).items())]
            drift = await insights.resource_drift(catalog, directory, rid, today)
            out["drift"] = None if drift is None else {"group_found": drift.group_found, "missing": list(drift.missing), "extra": list(drift.extra), "members": drift.members}
            out["names"] = {**(drift.names if drift else {}), **names_of(catalog, (h["upn"] for h in out["holder_list"]))}
        return out

    @app.get("/api/directory/users")
    async def search_users(data: Session, q: Annotated[str, Query(min_length=2, max_length=80)]) -> dict[str, Any]:
        await everyone(data)
        return {"users": [{"upn": u.upn, "name": u.name, "job": u.job, "department": u.department} for u in await directory.search_users(q)]}

    async def visible(catalog: Catalog, who: Actor, req: AccessRequest) -> dict[str, Any] | None:
        approve = await portal.can_approve(catalog, req, who)
        involved = who.upn in (req.person, req.requested_by)
        if not (approve or involved or portal.sees_everyone(catalog, who)):
            return None
        decline = req.state in ("pending", "approved", "open") and (who.admin or who.upn == req.requested_by or approve)
        return request_json(catalog, req, can_approve=approve, can_decline=decline)

    @app.get("/api/requests")
    async def list_requests(data: Session) -> dict[str, Any]:
        catalog = await portal.catalog()
        who = actor(data)
        out = [v for r in await portal.requests() if (v := await visible(catalog, who, r)) is not None]
        return {"requests": out, "read_only": portal.read_only}

    @app.get("/api/requests/{number}")
    async def get_request(number: Number, data: Session) -> dict[str, Any]:
        catalog = await portal.catalog()
        found = await visible(catalog, actor(data), await portal.request(number))
        if found is None:
            raise HTTPException(403, "not_allowed")
        return found

    @app.post("/api/requests", status_code=201)
    async def create_request(body: NewRequest, data: Writable) -> dict[str, Any]:
        req = await portal.ask(actor(data), person=body.person, items=[i.plain() for i in body.items], reason=body.reason, expires=body.expires)
        return request_json(await portal.catalog(), req)

    @app.post("/api/requests/{number}/approve")
    async def approve(number: Number, data: Writable) -> dict[str, Any]:
        req = await portal.approve(actor(data), number)
        return request_json(await portal.catalog(), req)

    @app.post("/api/requests/{number}/decline")
    async def decline(number: Number, body: Reason, data: Writable) -> dict[str, Any]:
        req = await portal.decline(actor(data), number, body.reason)
        return request_json(await portal.catalog(), req)

    @app.post("/api/requests/{number}/done")
    async def done(number: Number, body: Done, data: Writable) -> dict[str, Any]:
        req = await portal.done(actor(data), number, body.resource)
        return request_json(await portal.catalog(), req)

    @app.post("/api/removals", status_code=201)
    async def removal(body: Removal, data: Writable) -> dict[str, Any]:
        items = [i.plain() for i in body.items] if body.items is not None else None
        req = await portal.remove(actor(data), person=body.person, items=items, reason=body.reason)
        return request_json(await portal.catalog(), req)

    @app.get("/api/tasks")
    async def tasks(data: Session) -> dict[str, Any]:
        catalog = await portal.catalog()
        found = await portal.tasks(actor(data))
        return {"tasks": [{"request": request_json(catalog, req), "resources": [resource_json(catalog, rid, portal.today()) for rid in todo]} for req, todo in found]}

    if cfg.static_dir is not None:
        app.mount("/", StaticFiles(directory=cfg.static_dir, html=True), name="frontend")

    return app
