import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { api, ApiError, configure, type Catalog, type Me } from "./api";
import { CatalogView } from "./views/CatalogView";
import { Home } from "./views/Home";
import { NewRequest } from "./views/NewRequest";
import { Onboard } from "./views/Onboard";
import { OverviewView } from "./views/OverviewView";
import { People } from "./views/People";
import { PersonView } from "./views/PersonView";
import { RequestView } from "./views/RequestView";
import { Requests } from "./views/Requests";
import { ResourceView } from "./views/ResourceView";
import { Tasks } from "./views/Tasks";

type Toast = { id: number; message: string; kind: "info" | "error" };

type Session = {
  me: Me;
  catalog: Catalog | null;
  reloadCatalog: () => void;
  notify: (message: string, kind?: "info" | "error") => void;
};

const SessionContext = createContext<Session | null>(null);

export function useSession(): Session {
  const session = useContext(SessionContext);
  if (!session) throw new Error("useSession outside the signed-in shell");
  return session;
}

// The page in view lives in the hash: #/people/ada@example.com, #/requests/12, #/request?person=…
export type Route = { path: string[]; query: URLSearchParams };

function routeFromHash(): Route {
  const raw = window.location.hash.replace(/^#\/?/, "");
  const [path, query = ""] = raw.split("?");
  return { path: path.split("/").filter(Boolean).map(decodeURIComponent), query: new URLSearchParams(query) };
}

export function go(path: string) {
  window.location.hash = path.startsWith("/") ? path : `/${path}`;
}

// The callback sends a sign-in error back as ?auth_error=; show it once and drop it from the URL.
function takeAuthError(): string {
  const params = new URLSearchParams(window.location.search);
  const error = params.get("auth_error") ?? "";
  if (params.has("auth_error")) window.history.replaceState(null, "", window.location.pathname + window.location.hash);
  return error;
}

export function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [state, setState] = useState<"loading" | "signed-out" | "ready" | "ended">("loading");
  const [authError] = useState(takeAuthError);
  const [route, setRoute] = useState(routeFromHash);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const nextToast = useRef(0);

  const notify = useCallback((message: string, kind: "info" | "error" = "info") => {
    const id = ++nextToast.current;
    setToasts((t) => [...t, { id, message, kind }]);
    if (kind === "info") window.setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 5000);
  }, []);

  const reloadCatalog = useCallback(() => {
    api.catalog().then(setCatalog, (err) => notify(err instanceof ApiError ? err.message : String(err), "error"));
  }, [notify]);

  useEffect(() => {
    api
      .me()
      .then((data) => {
        configure(data.csrf, () => setState("ended"));
        setMe(data);
        setState("ready");
      })
      .catch((err) => setState(err instanceof ApiError && err.status === 401 ? "signed-out" : "ended"));
  }, []);

  useEffect(() => {
    if (state === "ready" && me && me.catalog_problems.length === 0) reloadCatalog();
  }, [state, me, reloadCatalog]);

  useEffect(() => {
    const onHash = () => setRoute(routeFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  async function signOut() {
    try {
      await api.logout();
    } finally {
      setMe(null);
      setState("signed-out");
    }
  }

  if (state === "loading") return <main className="center muted">Loading…</main>;

  if (state === "signed-out" || state === "ended" || !me) {
    return (
      <main className="center">
        <div className="card sign-in">
          <Logo />
          <h1>Permissions UI</h1>
          <p className="muted">{state === "ended" ? "Your session has ended." : "Sign in with your work account to see your access, ask for more and approve requests."}</p>
          {authError && (
            <p className="notice error" role="alert">
              {authError}
            </p>
          )}
          <a className="button primary wide" href="/auth/login">
            Sign in with Microsoft
          </a>
        </div>
      </main>
    );
  }

  const page = route.path[0] ?? "";
  const leads = me.lead_of.length > 0 || me.admin;
  const nav: { href: string; label: string; show: boolean; match: string }[] = [
    { href: "#/", label: "My access", show: true, match: "" },
    { href: "#/request", label: "Ask for access", show: !me.read_only, match: "request" },
    { href: "#/requests", label: "Requests", show: !me.read_only, match: "requests" },
    { href: "#/tasks", label: "Tasks", show: !me.read_only, match: "tasks" },
    { href: "#/onboard", label: "Onboard someone", show: leads && !me.read_only, match: "onboard" },
    { href: "#/overview", label: "Overview", show: me.sees_everyone, match: "overview" },
    { href: "#/people", label: "People", show: me.sees_everyone, match: "people" },
    { href: "#/catalog", label: "Catalog", show: true, match: "catalog" },
  ];

  return (
    <SessionContext.Provider value={{ me, catalog, reloadCatalog, notify }}>
      <div className="shell">
        <header className="topbar">
          <a className="brand" href="#/">
            <Logo />
            <span>Permissions UI</span>
          </a>
          <div className="who">
            <span title={me.upn}>{me.name || me.upn}</span>
            <button type="button" className="button small" onClick={() => void signOut()}>
              Sign out
            </button>
          </div>
        </header>
        <div className="layout">
          <nav className="vaults" aria-label="Pages">
            <select className="vault-select" aria-label="Page" value={`#/${page}`} onChange={(e) => (window.location.hash = e.target.value)}>
              {nav.filter((n) => n.show).map((n) => (
                <option key={n.href} value={n.href}>
                  {n.label}
                </option>
              ))}
            </select>
            <div className="nav-group">
              <ul>
                {nav
                  .filter((n) => n.show)
                  .map((n) => (
                    <li key={n.href}>
                      <a href={n.href} aria-current={page === n.match || (n.match === "requests" && page === "requests") ? "page" : undefined}>
                        {n.label}
                      </a>
                    </li>
                  ))}
              </ul>
            </div>
            <p className="muted small version">
              {me.release_notes ? (
                <a href={me.release_notes} target="_blank" rel="noreferrer" title="Release notes">
                  {me.version}
                </a>
              ) : (
                me.version
              )}
              {me.read_only && " · read-only"}
            </p>
          </nav>
          <main className="content">
            {me.catalog_problems.length > 0 ? <CatalogProblems problems={me.catalog_problems} /> : !catalog ? <p className="muted">Loading the catalog…</p> : <Page route={route} />}
          </main>
        </div>
        <div className="toasts" role="status" aria-live="polite">
          {toasts.map((t) => (
            <div key={t.id} className={`toast ${t.kind}`}>
              <span>{t.message}</span>
              <button type="button" className="icon-button" aria-label="Dismiss" onClick={() => setToasts((all) => all.filter((x) => x.id !== t.id))}>
                ×
              </button>
            </div>
          ))}
        </div>
      </div>
    </SessionContext.Provider>
  );
}

function Page({ route }: { route: Route }) {
  const [page, id] = route.path;
  switch (page) {
    case undefined:
      return <Home />;
    case "request":
      return <NewRequest person={route.query.get("person") ?? ""} preset={route.query.get("items") ?? ""} />;
    case "requests":
      return id ? <RequestView number={Number(id)} /> : <Requests tab={route.query.get("tab") ?? ""} />;
    case "tasks":
      return <Tasks />;
    case "onboard":
      return <Onboard team={route.query.get("team") ?? ""} />;
    case "overview":
      return <OverviewView />;
    case "people":
      return id ? <PersonView upn={id} /> : <People />;
    case "catalog":
      return <CatalogView tab={id ?? "resources"} />;
    case "resources":
      return id ? <ResourceView id={id} /> : <CatalogView tab="resources" />;
    default:
      return <NotFound />;
  }
}

function CatalogProblems({ problems }: { problems: string[] }) {
  return (
    <div className="card">
      <h1>The catalog does not hold together</h1>
      <p className="muted">Fix these in the catalog repository; nothing can be asked for until then.</p>
      <ul className="problems">
        {problems.map((p) => (
          <li key={p} className="mono small">
            {p}
          </li>
        ))}
      </ul>
    </div>
  );
}

function NotFound() {
  return (
    <div className="card">
      <h1>Nothing here</h1>
      <p className="muted">
        That page does not exist. <a href="#/">Back to your access</a>.
      </p>
    </div>
  );
}

export function Section({ title, children, actions }: { title: string; children: ReactNode; actions?: ReactNode }) {
  return (
    <section className="section">
      <div className="section-head">
        <h2>{title}</h2>
        {actions}
      </div>
      {children}
    </section>
  );
}

function Logo() {
  return (
    <svg className="logo" viewBox="0 0 32 32" aria-hidden="true">
      <rect width="32" height="32" rx="7" />
      <circle cx="16" cy="11" r="4.5" />
      <path d="M8 25c1-5 4.5-7.5 8-7.5s7 2.5 8 7.5" />
    </svg>
  );
}
