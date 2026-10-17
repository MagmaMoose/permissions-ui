import { api } from "../api";
import { Section } from "../App";
import { Loading, Person, useLoad } from "../components/ui";

export function OverviewView() {
  const { data, error } = useLoad(() => api.overview(), []);
  if (!data) return <Loading error={error} />;
  const extra = data.drift.filter((d) => d.extra.length > 0);
  const missing = data.drift.filter((d) => d.missing.length > 0 || !d.group_found);

  return (
    <>
      <div className="page-head">
        <h1>Overview</h1>
        <p className="muted">Who holds what, and what needs a look.</p>
      </div>
      <div className="stats">
        <Stat label="People with access" value={data.people} href="#/people" />
        <Stat label="Privileged resources" value={data.resources.privileged} href="#/catalog/resources" />
        <Stat label="Requests open" value={data.pending} href="#/requests?tab=all" />
        <Stat label="Access nobody granted" value={extra.reduce((n, d) => n + d.extra.length, 0)} warn />
        <Stat label="Undocumented groups" value={data.undocumented.length} warn={data.undocumented.length > 0} />
      </div>

      <Section title="Access nobody granted">
        <p className="muted small">In the group, but not in the catalog: added by hand, or left behind. Grant it through a request, or remove it.</p>
        {extra.length === 0 ? (
          <p className="muted">None.</p>
        ) : (
          <table className="table">
            <tbody>
              {extra.map((d) => (
                <tr key={d.resource}>
                  <td className="name">
                    <a href={`#/resources/${d.resource}`}>{d.name}</a>
                  </td>
                  <td>
                    {d.extra.map((upn, i) => (
                      <span key={upn}>
                        {i > 0 && ", "}
                        <Person upn={upn} />
                      </span>
                    ))}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>

      <Section title="Granted but not in place">
        <p className="muted small">The catalog grants it, the group lacks the person: the change has not been applied yet, or applying it failed.</p>
        {missing.length === 0 ? (
          <p className="muted">None.</p>
        ) : (
          <ul className="list">
            {missing.map((d) => (
              <li key={d.resource}>
                <a href={`#/resources/${d.resource}`}>{d.name}</a>: {d.group_found ? d.missing.join(", ") : "its group does not exist"}
              </li>
            ))}
          </ul>
        )}
      </Section>

      <Section title="Undocumented groups">
        <p className="muted small">
          Groups starting with {data.group_prefixes.map((p) => `“${p}”`).join(" or ") || "the configured prefixes"} that no resource describes. Nobody can say what they are for until someone adds them to the catalog.
        </p>
        {data.undocumented.length === 0 ? (
          <p className="muted">None.</p>
        ) : (
          <ul className="list">
            {data.undocumented.map((g) => (
              <li key={g.id}>
                <span className="mono">{g.name}</span> {g.description && <span className="muted">— {g.description}</span>}
              </li>
            ))}
          </ul>
        )}
      </Section>

      <Section title="Most privileged people">
        {data.privileged_people.length === 0 ? (
          <p className="muted">Nobody holds privileged access.</p>
        ) : (
          <ul className="list">
            {data.privileged_people.map((p) => (
              <li key={p.upn}>
                <Person upn={p.upn} />: {p.privileged.join(", ")}
              </li>
            ))}
          </ul>
        )}
      </Section>

      {data.conflicts.length > 0 && (
        <Section title="Access kept apart, held together">
          <ul className="list">
            {data.conflicts.map((c) => (
              <li key={c.upn}>
                <Person upn={c.upn} />: {c.pairs.map((p) => p.join(" and ")).join("; ")}
              </li>
            ))}
          </ul>
        </Section>
      )}

      <Section title="Expiring within 30 days">
        {data.expiring.length === 0 ? (
          <p className="muted">Nothing.</p>
        ) : (
          <table className="table">
            <tbody>
              {data.expiring.map((e) => (
                <tr key={`${e.upn}-${e.resource}`}>
                  <td>
                    <Person upn={e.upn} />
                  </td>
                  <td>
                    <a href={`#/resources/${e.resource}`}>{e.resource}</a>
                  </td>
                  <td>{e.expires}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>
    </>
  );
}

function Stat({ label, value, href, warn }: { label: string; value: number; href?: string; warn?: boolean }) {
  const body = (
    <>
      <span className={warn && value > 0 ? "stat-value warn-text" : "stat-value"}>{value}</span>
      <span className="stat-label">{label}</span>
    </>
  );
  return href ? (
    <a className="stat" href={href}>
      {body}
    </a>
  ) : (
    <div className="stat">{body}</div>
  );
}
