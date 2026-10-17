// The backend's JSON API. Same origin, the session cookie rides along, and every write carries
// the session's CSRF token. Who may see what is decided by the server; these types only say
// what comes back.

export type Tier = "standard" | "elevated" | "privileged";
export type Kind = "entra-group" | "github-team" | "manual";
export type Item = { role?: string; resource?: string };

export type Me = {
  name: string;
  upn: string;
  csrf: string;
  expires: number;
  version: string;
  release_notes: string | null;
  admin: boolean;
  auditor: boolean;
  lead_of: string[];
  sees_everyone: boolean;
  read_only: boolean;
  fulfilment: "pull-request" | "graph";
  repository: string | null;
  catalog_problems: string[];
};

export type Resource = {
  id: string;
  name: string;
  description: string;
  owner: string;
  kind: Kind;
  target: string;
  tier: Tier;
  approvers: string[];
  approvals: number;
  max_days: number | null;
  incompatible: string[];
  tags: string[];
  instructions: string;
  fulfillers: string[];
  roles: string[];
  holders: number;
  holder_list?: { upn: string; via: string[]; expires: string | null }[];
  drift?: { group_found: boolean; missing: string[]; extra: string[]; members: number } | null;
  names?: Names;
};

// Display names by user principal name, for the people a response mentions.
export type Names = Record<string, string>;

export type Role = { id: string; name: string; description: string; resources: string[]; approvers: string[]; max_days: number | null; teams: string[] };
export type Team = { id: string; name: string; description: string; leads: string[]; roles: string[] };
export type Catalog = { resources: Resource[]; roles: Role[]; teams: Team[] };

export type Overview = {
  people: number;
  resources: Record<Tier, number>;
  roles: number;
  teams: number;
  pending: number;
  privileged_people: { upn: string; privileged: string[] }[];
  expiring: { upn: string; resource: string; expires: string }[];
  conflicts: { upn: string; pairs: string[][] }[];
  drift: { resource: string; name: string; group_found: boolean; missing: string[]; extra: string[] }[];
  undocumented: { id: string; name: string; description: string }[];
  group_prefixes: string[];
  names: Names;
};

export type PersonRow = { upn: string; tiers: Record<Tier, number>; privileged: number; expiring: number; conflicts: number; grants: number };
export type DirectoryUser = { upn: string; name: string; job: string; department: string; in_catalog?: boolean };

export type Person = {
  upn: string;
  name: string;
  user: { name: string; mail: string; job: string; department: string; enabled: boolean } | null;
  teams: string[];
  grants: { role: string | null; resource: string | null; reason: string; granted: string; expires: string | null; approved_by: string[]; request: number | null; active: boolean }[];
  holdings: { resource: string; name: string; tier: Tier; kind: Kind; via: string[]; expires: string | null }[];
  tiers: Record<Tier, number>;
  conflicts: string[][];
  expiring: { resource: string; expires: string }[];
  extra: string[];
  missing: string[];
  unknown_groups: string[];
};

export type AccessRequest = {
  number: number;
  url: string;
  kind: "grant" | "revoke";
  person: string;
  names: Names;
  items: (Item & { name: string })[];
  resources: { id: string; name: string; tier: Tier; kind: Kind }[];
  reason: string;
  requested_by: string;
  expires: string | null;
  created: string;
  state: "pending" | "approved" | "granted" | "declined" | "withdrawn" | "open";
  approvals: { upn: string; name: string; at: string; resources: string[] }[];
  outstanding: { resource: string; approvers: string[]; count: number }[];
  declined_by: string;
  decline_reason: string;
  done: string[];
  can_approve: boolean;
  can_decline: boolean;
};

export type Task = { request: AccessRequest; resources: Resource[] };

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly data: Record<string, unknown> = {},
  ) {
    super(message);
  }
}

const MESSAGES: Record<string, string> = {
  signed_out: "Your session has ended. Sign in again.",
  csrf: "This page is out of date. Reload it and try again.",
  not_allowed: "You don't have access to this.",
  invalid_request: "Some fields are not valid.",
};

let csrf = "";
let onSignedOut: () => void = () => {};

export function configure(token: string, signedOut: () => void) {
  csrf = token;
  onSignedOut = signedOut;
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (method !== "GET") headers["X-CSRF-Token"] = csrf;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let response: Response;
  try {
    response = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), credentials: "same-origin", cache: "no-store" });
  } catch {
    throw new ApiError(0, "network", "The server could not be reached.");
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const code = String(data.error ?? response.status);
    if (response.status === 401) onSignedOut();
    throw new ApiError(response.status, code, data.message ?? MESSAGES[code] ?? `Request failed (${response.status}).`, data);
  }
  return data as T;
}

export const api = {
  me: () => request<Me>("GET", "/api/me"),
  logout: () => request<unknown>("POST", "/auth/logout"),
  catalog: () => request<Catalog>("GET", "/api/catalog"),
  overview: () => request<Overview>("GET", "/api/overview"),
  people: (q = "") => request<{ people: PersonRow[]; directory: DirectoryUser[]; names: Names }>("GET", `/api/people${q ? `?q=${encodeURIComponent(q)}` : ""}`),
  person: (upn: string) => request<Person>("GET", `/api/people/${encodeURIComponent(upn)}`),
  resource: (id: string) => request<Resource>("GET", `/api/resources/${encodeURIComponent(id)}`),
  searchUsers: (q: string) => request<{ users: DirectoryUser[] }>("GET", `/api/directory/users?q=${encodeURIComponent(q)}`).then((r) => r.users),
  requests: () => request<{ requests: AccessRequest[]; read_only: boolean }>("GET", "/api/requests"),
  request: (n: number) => request<AccessRequest>("GET", `/api/requests/${n}`),
  ask: (body: { person: string; items: Item[]; reason: string; expires?: string | null }) => request<AccessRequest>("POST", "/api/requests", body),
  approve: (n: number) => request<AccessRequest>("POST", `/api/requests/${n}/approve`),
  decline: (n: number, reason: string) => request<AccessRequest>("POST", `/api/requests/${n}/decline`, { reason }),
  done: (n: number, resource: string) => request<AccessRequest>("POST", `/api/requests/${n}/done`, { resource }),
  remove: (body: { person: string; items: Item[] | null; reason: string }) => request<AccessRequest>("POST", "/api/removals", body),
  tasks: () => request<{ tasks: Task[] }>("GET", "/api/tasks").then((r) => r.tasks),
};
