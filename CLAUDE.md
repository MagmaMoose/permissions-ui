# permissions-ui

FastAPI backend (`src/permissions_ui`) serving a React/TypeScript console (`frontend/`), one
image, a Helm chart (`charts/permissions-ui`) and a reference OpenTofu module
(`terraform/entra-grants`). The state lives in the catalog repository; the server stores nothing.

## Things that fail silently

- **Records count only signed with `PERMISSIONS_UI_RECORD_KEY` for their pull request's number,
  and comments only from the app's own GitHub login** (`PERMISSIONS_UI_GITHUB_BOT`, or the App's
  `<slug>[bot]`). The request record is written after the pull request exists. A new key drops
  every request the old one signed from the portal; another login leaves every open request
  "pending" with its approvals ignored. Nothing errors either way. Rotate with the new key on the
  key file's first line and the old one below it.
- **The App's installation token carries only `PERMISSIONS` in `github.py`, for the catalog
  repository alone.** A new call that needs another permission gets a 403 until it is added there
  and to the App.
- **`session` in `app.py` stays an `async def`.** It hands the request's session to the
  delegated directory mode through a ContextVar. As a plain `def` it runs in a worker thread, the
  value never reaches the route, and every delegated directory read answers 401 signed_out.
- **People are their `userPrincipalName` in this directory**, looked up by object id at sign-in,
  never the ID token's `preferred_username`. For a guest that is their home tenant's name for
  them, and every grant, lead and approver check would miss them.
- **No `from __future__ import annotations` in `app.py`.** The routes use `Annotated` aliases
  defined inside `create_app`; postponed annotations turn them into unresolvable strings and
  FastAPI reads `data` as a required query parameter: every route answers 422.
- **A grants file must be `grants/<lowercased upn>.yaml`.** The loader refuses anything else, and
  a refused catalog stops every request until it is fixed.
- **The expiry CronJob's pods must not carry `app.kubernetes.io/component: web`.** That label is
  the Service and PodDisruptionBudget selector; a job pod with it receives traffic.
- **The page may hold no inline script or style.** The CSP blocks it and the page renders blank;
  CI greps `frontend/dist/index.html` for it.
- **The image name follows the repository name** (`ghcr.io/magmamoose/permissions-ui`). The chart's
  default image is that name; a rename means a chart change in the same PR.
- **Finalising rebuilds the request branch on the latest `main` with a force update, and merges
  exactly that commit.** The `chore/access-*` branches belong to the app; a human commit there is
  overwritten, or holds the merge back if it lands between the rebuild and the merge. For a few
  seconds after a force update GitHub still shows the old head and refuses the merge (405):
  `merge` tries once more after `CATCH_UP_SECONDS`, and a slower GitHub leaves the request
  approved, with the branch-rules comment, until someone approves it again.
- **No client or employer names anywhere in this public repository**: code, tests, fixtures,
  docs, commits. Examples use example.com and fictional teams.
