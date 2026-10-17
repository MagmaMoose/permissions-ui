import { useState } from "react";
import { api } from "../api";
import { Loading, Person, Tiers, useLoad } from "../components/ui";

export function People() {
  const [filter, setFilter] = useState("");
  const [query, setQuery] = useState("");
  const { data, error } = useLoad(() => api.people(query), [query]);
  const label = (upn: string) => data?.names[upn] || upn;
  const rows = (data?.people ?? []).filter((p) => `${label(p.upn)} ${p.upn}`.toLowerCase().includes(filter.toLowerCase()));

  return (
    <>
      <div className="page-head">
        <h1>People</h1>
        <p className="muted">Everyone the catalog grants something, most privileged first. Search the directory to open anyone else.</p>
      </div>
      <form
        className="toolbar"
        onSubmit={(e) => {
          e.preventDefault();
          setQuery(filter.trim());
        }}
      >
        <input className="filter" type="search" placeholder="Filter, or search the directory and press Enter" value={filter} onChange={(e) => setFilter(e.target.value)} aria-label="Filter people" />
      </form>
      {!data ? (
        <Loading error={error} />
      ) : (
        <>
          {data.directory.filter((u) => !u.in_catalog).length > 0 && (
            <ul className="list">
              {data.directory
                .filter((u) => !u.in_catalog)
                .map((u) => (
                  <li key={u.upn}>
                    <a href={`#/people/${encodeURIComponent(u.upn)}`}>{u.name}</a> <span className="muted">{u.upn} · holds nothing in the catalog</span>
                  </li>
                ))}
            </ul>
          )}
          <table className="table">
            <thead>
              <tr>
                <th>Person</th>
                <th>Standard · elevated · privileged</th>
                <th>Expiring</th>
                <th>Conflicts</th>
              </tr>
            </thead>
            <tbody>
              {rows
                .sort((a, b) => b.tiers.privileged - a.tiers.privileged || b.tiers.elevated - a.tiers.elevated || label(a.upn).localeCompare(label(b.upn)))
                .map((p) => (
                  <tr key={p.upn}>
                    <td className="name">
                      <Person upn={p.upn} name={data.names[p.upn]} />
                    </td>
                    <td>
                      <Tiers tiers={p.tiers} />
                    </td>
                    <td>{p.expiring || "—"}</td>
                    <td className={p.conflicts ? "danger-text" : undefined}>{p.conflicts || "—"}</td>
                  </tr>
                ))}
            </tbody>
          </table>
        </>
      )}
    </>
  );
}
