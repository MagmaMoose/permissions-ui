"""`python -m permissions_ui [serve | check <dir> | expire]`.

serve   load the config, build the app, serve it with uvicorn (the default)
check   validate a catalog directory and print every problem: for the catalog repository's CI
expire  open and merge a removal for every grant that ran out: for a CronJob

TLS ends at the ingress in front of it; this listens on plain http inside the pod.
"""

import asyncio
import json
import logging
import sys
from pathlib import Path

import httpx
import uvicorn

from .app import build_portal, create_app
from .catalog import CatalogError, load_catalog
from .config import ConfigError, load_config


def _config():
    try:
        return load_config()
    except ConfigError as err:
        print(json.dumps({"msg": "configuration refused", "reason": str(err)}), file=sys.stderr)
        sys.exit(1)


def check(root: Path) -> int:
    files = {str(p.relative_to(root)): p.read_text() for p in sorted(root.rglob("*.y*ml")) if p.is_file()}
    try:
        catalog = load_catalog(files)
    except CatalogError as err:
        for problem in err.problems:
            print(problem, file=sys.stderr)
        return 1
    print(f"{len(catalog.resources)} resources, {len(catalog.roles)} roles, {len(catalog.teams)} teams, {len(catalog.people)} people: the catalog holds together.")
    return 0


async def expire() -> int:
    cfg = _config()
    async with httpx.AsyncClient(follow_redirects=False) as http:
        portal = build_portal(cfg, http)
        if portal.read_only:
            print(json.dumps({"msg": "expire needs PERMISSIONS_UI_GITHUB_REPO"}), file=sys.stderr)
            return 1
        opened = await portal.expire()
    print(json.dumps({"msg": "expired", "requests": opened}))
    return 0


def serve() -> None:
    cfg = _config()
    logging.getLogger("permissions_ui").info(
        json.dumps(
            {
                "msg": "starting",
                "version": cfg.version,
                "origin": cfg.origin,
                "tenant": cfg.tenant_id,
                "client": cfg.client_id,
                "credential": "workload identity" if cfg.federated_token_file else "client secret",
                "catalog": f"{cfg.github.repo}:{cfg.github.branch}/{cfg.github.root}" if cfg.github else str(cfg.catalog_dir),
                "github_auth": ("app" if cfg.github.app_id else "token") if cfg.github else None,
                "fulfilment": cfg.fulfilment,
                "auto_merge": cfg.auto_merge,
                "group_prefixes": list(cfg.group_prefixes) or None,
                "teams": bool(cfg.teams_webhook),
            }
        )
    )
    uvicorn.run(
        create_app(cfg),
        # All interfaces: a container port, reached only through the ingress in front of it.
        host="0.0.0.0",  # noqa: S104 # nosec B104
        port=cfg.port,
        # Longer than common ingress idle timeouts, so this side never closes a keep-alive
        # connection the proxy is about to reuse (a 502).
        timeout_keep_alive=95,
        access_log=False,
        server_header=False,
        log_level="info",
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    # httpx logs every request at INFO, and its URLs name people and groups.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    command = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if command == "check":
        sys.exit(check(Path(sys.argv[2] if len(sys.argv) > 2 else ".")))
    if command == "expire":
        sys.exit(asyncio.run(expire()))
    if command != "serve":
        print("usage: python -m permissions_ui [serve | check <dir> | expire]", file=sys.stderr)
        sys.exit(2)
    serve()


if __name__ == "__main__":
    main()
