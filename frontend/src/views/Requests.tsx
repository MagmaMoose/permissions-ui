import { api } from "../api";
import { useSession } from "../App";
import { Loading, StateBadge, useLoad } from "../components/ui";
import { when } from "../format";

export function Requests({ tab }: { tab: string }) {
  const { me } = useSession();
  const { data, error } = useLoad(() => api.requests(), []);
  const all = data?.requests ?? [];
  const tabs = [
    ["approve", "To approve", all.filter((r) => r.can_approve)],
    ["mine", "Mine", all.filter((r) => r.person === me.upn || r.requested_by === me.upn)],
    ...(me.sees_everyone ? [["all", "All", all] as const] : []),
  ] as const;
  const current = tabs.find(([id]) => id === tab) ?? tabs.find(([, , list]) => list.length > 0) ?? tabs[0];

  return (
    <>
      <div className="page-head">
        <h1>Requests</h1>
        <p className="muted">Every request is a pull request in the catalog repository; approving here records it there.</p>
      </div>
      <div className="toolbar">
        <div className="tabs" role="tablist">
          {tabs.map(([id, label, list]) => (
            <a key={id} role="tab" className="tab" aria-selected={current[0] === id} href={`#/requests?tab=${id}`}>
              {label} <span className="count">{list.length}</span>
            </a>
          ))}
        </div>
        <a className="button primary small" href="#/request">
          Ask for access
        </a>
      </div>
      {!data ? (
        <Loading error={error} />
      ) : current[2].length === 0 ? (
        <p className="empty">Nothing here.</p>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>Request</th>
              <th>For</th>
              <th>Asked by</th>
              <th>State</th>
              <th>Opened</th>
            </tr>
          </thead>
          <tbody>
            {current[2].map((r) => (
              <tr key={r.number}>
                <td className="name">
                  <a href={`#/requests/${r.number}`}>
                    #{r.number} {r.kind === "revoke" ? "Remove " : ""}
                    {r.items.map((i) => i.name).join(", ")}
                  </a>
                </td>
                <td title={r.person}>{r.names[r.person] || r.person}</td>
                <td title={r.requested_by}>{r.names[r.requested_by] || r.requested_by}</td>
                <td>
                  <StateBadge req={r} />
                </td>
                <td>{when(Date.parse(r.created) / 1000)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}
