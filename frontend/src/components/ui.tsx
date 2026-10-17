import { useCallback, useEffect, useState, type ReactNode } from "react";
import { api, ApiError, type AccessRequest, type Catalog, type DirectoryUser, type Tier } from "../api";

// A loader for one view's data: the value, its error, and a reload.
export function useLoad<T>(load: () => Promise<T>, deps: unknown[]): { data: T | null; error: string; reload: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [tick, setTick] = useState(0);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const run = useCallback(load, deps);
  useEffect(() => {
    let live = true;
    setError("");
    run().then(
      (value) => live && setData(value),
      (err) => live && setError(err instanceof ApiError ? err.message : String(err)),
    );
    return () => {
      live = false;
    };
  }, [run, tick]);
  return { data, error, reload: () => setTick((t) => t + 1) };
}

export function Loading({ error, children }: { error: string; children?: ReactNode }) {
  if (error)
    return (
      <p className="notice error" role="alert">
        {error}
      </p>
    );
  return <p className="muted">{children ?? "Loading…"}</p>;
}

export function TierBadge({ tier }: { tier: Tier }) {
  return <span className={`badge tier-${tier}`}>{tier}</span>;
}

const STATE_LABELS: Record<AccessRequest["state"], string> = {
  pending: "Waiting for approval",
  approved: "Approved, merging",
  granted: "Done",
  declined: "Declined",
  withdrawn: "Withdrawn",
  open: "Needs attention",
};

export function StateBadge({ req }: { req: AccessRequest }) {
  const label = req.kind === "revoke" && req.state === "granted" ? "Removed" : STATE_LABELS[req.state];
  return <span className={`badge state-${req.state}`}>{label}</span>;
}

export function Tiers({ tiers }: { tiers: Record<Tier, number> }) {
  return (
    <span className="tiers" aria-label={`${tiers.standard} standard, ${tiers.elevated} elevated, ${tiers.privileged} privileged`}>
      <span className="tier-dot tier-standard" title="standard">
        {tiers.standard}
      </span>
      <span className="tier-dot tier-elevated" title="elevated">
        {tiers.elevated}
      </span>
      <span className="tier-dot tier-privileged" title="privileged">
        {tiers.privileged}
      </span>
    </span>
  );
}

// "team:payments" reads as "Payments leads", "group:x" as "members of x", "user:a@b" as "a@b".
export function approverLabel(spec: string, catalog: Catalog | null): string {
  const [kind, ...rest] = spec.split(":");
  const value = rest.join(":");
  if (kind === "team") return `${catalog?.teams.find((t) => t.id === value)?.name ?? value} leads`;
  if (kind === "group") return `members of ${value}`;
  return value;
}

export function upnName(upn: string) {
  return upn.split("@")[0].replace(/[._]/g, " ");
}

export function Person({ upn }: { upn: string }) {
  return (
    <a href={`#/people/${encodeURIComponent(upn)}`} title={upn}>
      {upn}
    </a>
  );
}

// Search the directory for a person; picks the user principal name.
export function PersonPicker({ value, onPick, autoFocus }: { value: string; onPick: (upn: string) => void; autoFocus?: boolean }) {
  const [text, setText] = useState(value);
  const [found, setFound] = useState<DirectoryUser[]>([]);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (text.length < 2 || text === value) {
      setFound([]);
      return;
    }
    const timer = window.setTimeout(() => {
      api.searchUsers(text).then(setFound, () => setFound([]));
    }, 250);
    return () => window.clearTimeout(timer);
  }, [text, value]);

  return (
    <div className="picker">
      <input
        type="search"
        value={text}
        placeholder="Name or email"
        autoFocus={autoFocus}
        data-autofocus={autoFocus ? "" : undefined}
        onChange={(e) => {
          setText(e.target.value);
          setOpen(true);
        }}
        aria-label="Person"
      />
      {open && found.length > 0 && (
        <ul className="picker-list" role="listbox">
          {found.map((u) => (
            <li key={u.upn}>
              <button
                type="button"
                className="picker-item"
                onClick={() => {
                  setText(u.upn);
                  setOpen(false);
                  onPick(u.upn);
                }}
              >
                <strong>{u.name}</strong> <span className="muted">{u.upn}</span>
                {(u.job || u.department) && <span className="muted small"> · {[u.job, u.department].filter(Boolean).join(", ")}</span>}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
