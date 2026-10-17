import { useState } from "react";
import { useSession } from "../App";
import { approverLabel, TierBadge } from "../components/ui";

const TABS = [
  ["resources", "Resources"],
  ["roles", "Roles"],
  ["teams", "Teams"],
] as const;

export function CatalogView({ tab }: { tab: string }) {
  const { catalog, me } = useSession();
  const [filter, setFilter] = useState("");
  if (!catalog) return null;
  const needle = filter.toLowerCase();
  const match = (...texts: string[]) => texts.some((t) => t.toLowerCase().includes(needle));

  return (
    <>
      <div className="page-head">
        <h1>Catalog</h1>
        <p className="muted">Everything that can be granted, what it is for, who owns it and who approves it.</p>
      </div>
      <div className="toolbar">
        <div className="tabs" role="tablist">
          {TABS.map(([id, label]) => (
            <a key={id} role="tab" className="tab" aria-selected={tab === id} href={`#/catalog/${id}`}>
              {label} <span className="count">{catalog[id].length}</span>
            </a>
          ))}
        </div>
        <input className="filter" type="search" placeholder="Filter" value={filter} onChange={(e) => setFilter(e.target.value)} aria-label="Filter the catalog" />
      </div>

      {tab === "resources" && (
        <table className="table">
          <thead>
            <tr>
              <th>Resource</th>
              <th>Tier</th>
              <th>Owner</th>
              <th>Approved by</th>
              <th>Holders</th>
            </tr>
          </thead>
          <tbody>
            {catalog.resources
              .filter((r) => match(r.id, r.name, r.description, r.target, r.owner, ...r.tags))
              .map((r) => (
                <tr key={r.id}>
                  <td className="name">
                    <a href={`#/resources/${r.id}`}>{r.name}</a>
                    <div className="muted small">{r.description}</div>
                  </td>
                  <td>
                    <TierBadge tier={r.tier} />
                  </td>
                  <td>{catalog.teams.find((t) => t.id === r.owner)?.name ?? r.owner}</td>
                  <td>
                    {r.approvals > 1 ? `${r.approvals} of ` : ""}
                    {r.approvers.map((a) => approverLabel(a, catalog)).join(", ")}
                  </td>
                  <td>{r.holders}</td>
                </tr>
              ))}
          </tbody>
        </table>
      )}

      {tab === "roles" && (
        <div className="cards">
          {catalog.roles
            .filter((r) => match(r.id, r.name, r.description, ...r.resources))
            .map((role) => (
              <div key={role.id} className="card">
                <h2>{role.name}</h2>
                <p className="muted">{role.description}</p>
                <ul className="list">
                  {role.resources.map((rid) => {
                    const r = catalog.resources.find((x) => x.id === rid);
                    return (
                      <li key={rid}>
                        <a href={`#/resources/${rid}`}>{r?.name ?? rid}</a> {r && <TierBadge tier={r.tier} />}
                      </li>
                    );
                  })}
                </ul>
                <p className="muted small">
                  Approved by {(role.approvers.length ? role.approvers : ["each resource's approvers"]).map((a) => (a.includes(":") ? approverLabel(a, catalog) : a)).join(", ")}
                  {role.max_days ? ` · at most ${role.max_days} days` : ""}
                  {role.teams.length > 0 && ` · given to new members of ${role.teams.join(", ")}`}
                </p>
                {!me.read_only && (
                  <a className="button small" href={`#/request?items=role:${role.id}`}>
                    Ask for this role
                  </a>
                )}
              </div>
            ))}
        </div>
      )}

      {tab === "teams" && (
        <div className="cards">
          {catalog.teams
            .filter((t) => match(t.id, t.name, t.description, ...t.leads))
            .map((team) => (
              <div key={team.id} className="card">
                <h2>{team.name}</h2>
                {team.description && <p className="muted">{team.description}</p>}
                <p>
                  <span className="muted">Leads:</span> {team.leads.join(", ")}
                </p>
                <p>
                  <span className="muted">New members get:</span> {team.roles.map((r) => catalog.roles.find((x) => x.id === r)?.name ?? r).join(", ") || "nothing by default"}
                </p>
                {!me.read_only && (me.admin || me.lead_of.includes(team.id)) && (
                  <a className="button small" href={`#/onboard?team=${team.id}`}>
                    Onboard someone
                  </a>
                )}
              </div>
            ))}
        </div>
      )}
    </>
  );
}
