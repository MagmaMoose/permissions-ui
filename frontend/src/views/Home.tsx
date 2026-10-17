import { api } from "../api";
import { Section, useSession } from "../App";
import { Loading, StateBadge, TierBadge, useLoad } from "../components/ui";
import { when } from "../format";

export function Home() {
  const { me } = useSession();
  const person = useLoad(() => api.person(me.upn), [me.upn]);
  const requests = useLoad(() => (me.read_only ? Promise.resolve({ requests: [], read_only: true }) : api.requests()), [me.read_only]);
  const mine = (requests.data?.requests ?? []).filter((r) => r.person === me.upn || r.requested_by === me.upn).slice(0, 8);
  const waiting = (requests.data?.requests ?? []).filter((r) => r.can_approve);

  return (
    <>
      <div className="page-head">
        <h1>Your access</h1>
        <p className="muted">What you hold today, through which role, and until when.</p>
      </div>
      {waiting.length > 0 && (
        <p className="notice">
          <a href="#/requests?tab=approve">
            {waiting.length} request{waiting.length === 1 ? "" : "s"} wait for your approval.
          </a>
        </p>
      )}
      {!person.data ? (
        <Loading error={person.error} />
      ) : person.data.holdings.length === 0 ? (
        <div className="empty">
          <p>You hold nothing through the catalog yet.</p>
          {!me.read_only && (
            <a className="button primary" href="#/request">
              Ask for access
            </a>
          )}
        </div>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>Resource</th>
              <th>Tier</th>
              <th>Through</th>
              <th>Until</th>
            </tr>
          </thead>
          <tbody>
            {person.data.holdings.map((h) => (
              <tr key={h.resource}>
                <td className="name">
                  <a href={`#/resources/${h.resource}`}>{h.name}</a>
                </td>
                <td>
                  <TierBadge tier={h.tier} />
                </td>
                <td>{h.via.map((v) => (v === "direct" ? "direct" : `role ${v}`)).join(", ")}</td>
                <td>{h.expires ?? "no end date"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {person.data && person.data.extra.length > 0 && (
        <p className="notice warn">
          You are also in {person.data.extra.length} group{person.data.extra.length === 1 ? "" : "s"} the catalog never granted you: {person.data.extra.join(", ")}.
        </p>
      )}
      {!me.read_only && (
        <Section title="Your requests" actions={<a href="#/requests?tab=mine">All of them</a>}>
          {!requests.data ? (
            <Loading error={requests.error} />
          ) : mine.length === 0 ? (
            <p className="muted">None yet.</p>
          ) : (
            <ul className="list">
              {mine.map((r) => (
                <li key={r.number}>
                  <a href={`#/requests/${r.number}`}>
                    #{r.number} {r.kind === "revoke" ? "Remove" : ""} {r.items.map((i) => i.name).join(", ")}
                    {r.person !== me.upn && ` for ${r.names[r.person] || r.person}`}
                  </a>{" "}
                  <StateBadge req={r} /> <span className="muted small">{when(Date.parse(r.created) / 1000)}</span>
                </li>
              ))}
            </ul>
          )}
        </Section>
      )}
    </>
  );
}
