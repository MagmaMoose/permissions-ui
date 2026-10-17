import { useMemo, useState } from "react";
import { api, ApiError, type Catalog, type Item } from "../api";
import { go, useSession } from "../App";
import { approverLabel, PersonPicker, TierBadge } from "../components/ui";

const key = (i: Item) => (i.role ? `role:${i.role}` : `resource:${i.resource}`);

function parsePreset(preset: string): Item[] {
  return preset
    .split(",")
    .filter(Boolean)
    .map((p) => {
      const [kind, id] = p.split(":");
      return kind === "role" ? { role: id } : { resource: id };
    });
}

// Who will be asked, as the server works it out: a role's approvers, else the resource's, else
// its owning team's leads; a privileged resource usually needs two.
export function approvalsNeeded(catalog: Catalog, items: Item[]): { approvers: string; count: number; resources: string[] }[] {
  const needs = new Map<string, { approvers: string; count: number; resources: string[] }>();
  for (const item of items) {
    const role = item.role ? catalog.roles.find((r) => r.id === item.role) : undefined;
    const ids = role ? role.resources : [item.resource ?? ""];
    for (const rid of ids) {
      const resource = catalog.resources.find((r) => r.id === rid);
      if (!resource) continue;
      const specs = role && role.approvers.length ? role.approvers : resource.approvers;
      const label = specs.map((s) => approverLabel(s, catalog)).join(" or ");
      const id = `${label}/${resource.approvals}`;
      const found = needs.get(id) ?? { approvers: label, count: resource.approvals, resources: [] };
      found.resources.push(resource.name);
      needs.set(id, found);
    }
  }
  return [...needs.values()];
}

export function NewRequest({ person: initialPerson, preset }: { person: string; preset: string }) {
  const { catalog, me, notify } = useSession();
  const [person, setPerson] = useState(initialPerson || me.upn);
  const [items, setItems] = useState<Item[]>(() => parsePreset(preset));
  const [filter, setFilter] = useState("");
  const [reason, setReason] = useState("");
  const [expires, setExpires] = useState("");
  const [busy, setBusy] = useState(false);
  const forOthers = me.admin || me.lead_of.length > 0;

  const options = useMemo(() => {
    if (!catalog) return [];
    const needle = filter.toLowerCase();
    const roles = catalog.roles.map((r) => ({ item: { role: r.id } as Item, name: r.name, description: r.description, tier: null, extra: `${r.resources.length} resources` }));
    const resources = catalog.resources.map((r) => ({ item: { resource: r.id } as Item, name: r.name, description: r.description, tier: r.tier, extra: r.kind === "manual" ? "set up by hand" : "" }));
    return [...roles, ...resources].filter((o) => !needle || `${o.name} ${o.description}`.toLowerCase().includes(needle));
  }, [catalog, filter]);

  if (!catalog) return null;
  const chosen = new Set(items.map(key));
  const needs = approvalsNeeded(catalog, items);

  async function submit() {
    setBusy(true);
    try {
      const req = await api.ask({ person, items, reason: reason.trim(), expires: expires || null });
      notify(req.state === "granted" ? `Request #${req.number} is approved and done.` : `Request #${req.number} is open; the approvers have been asked.`);
      go(`/requests/${req.number}`);
    } catch (err) {
      notify(err instanceof ApiError ? err.message : String(err), "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="page-head">
        <h1>Ask for access</h1>
        <p className="muted">Pick a role, which bundles what a job needs, or single resources. The right people are asked to approve.</p>
      </div>
      <form
        className="form request-form"
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <div className="field">
          <label>For</label>
          {forOthers ? <PersonPicker value={person} onPick={setPerson} /> : <input value={person} readOnly aria-label="For" />}
        </div>

        <div className="field">
          <label htmlFor="item-filter">What</label>
          <input id="item-filter" type="search" placeholder="Filter roles and resources" value={filter} onChange={(e) => setFilter(e.target.value)} />
          <ul className="choices">
            {options.map((o) => (
              <li key={key(o.item)}>
                <label className={chosen.has(key(o.item)) ? "choice chosen" : "choice"}>
                  <input
                    type="checkbox"
                    checked={chosen.has(key(o.item))}
                    onChange={(e) => setItems((all) => (e.target.checked ? [...all, o.item] : all.filter((i) => key(i) !== key(o.item))))}
                  />
                  <span>
                    <strong>{o.name}</strong> {o.tier ? <TierBadge tier={o.tier} /> : <span className="badge">role</span>} {o.extra && <span className="muted small">{o.extra}</span>}
                    <span className="muted small block">{o.description}</span>
                  </span>
                </label>
              </li>
            ))}
          </ul>
        </div>

        <div className="field">
          <label htmlFor="reason">Why</label>
          <textarea id="reason" required minLength={3} maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} placeholder="What you need it for" />
        </div>

        <div className="field">
          <label htmlFor="expires">Until (optional)</label>
          <input id="expires" type="date" value={expires} onChange={(e) => setExpires(e.target.value)} />
          <p className="hint">Empty takes the longest the catalog allows for what you picked.</p>
        </div>

        {needs.length > 0 && (
          <div className="notice">
            <p>Approvals needed:</p>
            <ul className="list">
              {needs.map((n) => (
                <li key={`${n.approvers}/${n.count}`}>
                  {n.count > 1 ? `${n.count} of ` : ""}
                  {n.approvers} <span className="muted small">for {n.resources.join(", ")}</span>
                </li>
              ))}
            </ul>
          </div>
        )}

        <footer className="dialog-footer">
          <button type="submit" className="button primary" disabled={busy || items.length === 0 || reason.trim().length < 3 || !person}>
            {busy ? "Opening…" : "Ask"}
          </button>
        </footer>
      </form>
    </>
  );
}
