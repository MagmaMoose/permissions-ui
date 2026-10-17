import { api } from "../api";
import { Section, useSession } from "../App";
import { approverLabel, Loading, Person, TierBadge, useLoad } from "../components/ui";

const KINDS = { "entra-group": "Entra ID group", "github-team": "GitHub team", manual: "Set up by hand" } as const;

export function ResourceView({ id }: { id: string }) {
  const { catalog, me } = useSession();
  const { data, error } = useLoad(() => api.resource(id), [id]);
  if (!data) return <Loading error={error} />;
  const owner = catalog?.teams.find((t) => t.id === data.owner);

  return (
    <>
      <div className="page-head">
        <h1>{data.name}</h1>
        <p className="muted">{data.description}</p>
        <div className="actions">
          <TierBadge tier={data.tier} />
          {!me.read_only && (
            <a className="button small" href={`#/request?items=resource:${data.id}`}>
              Ask for this
            </a>
          )}
        </div>
      </div>
      <dl className="facts">
        <dt>Kind</dt>
        <dd>
          {KINDS[data.kind]}: <span className="mono">{data.target}</span>
        </dd>
        <dt>Owner</dt>
        <dd>{owner?.name ?? data.owner}</dd>
        <dt>Approved by</dt>
        <dd>
          {data.approvals > 1 ? `${data.approvals} of ` : ""}
          {data.approvers.map((a) => approverLabel(a, catalog)).join(", ")}
        </dd>
        <dt>Longest grant</dt>
        <dd>{data.max_days ? `${data.max_days} days` : "no limit"}</dd>
        {data.roles.length > 0 && (
          <>
            <dt>In roles</dt>
            <dd>{data.roles.map((r) => catalog?.roles.find((x) => x.id === r)?.name ?? r).join(", ")}</dd>
          </>
        )}
        {data.incompatible.length > 0 && (
          <>
            <dt>Kept apart from</dt>
            <dd>{data.incompatible.join(", ")}</dd>
          </>
        )}
        {data.kind === "manual" && (
          <>
            <dt>Set up by</dt>
            <dd>{data.fulfillers.map((f) => approverLabel(f, catalog)).join(", ")}</dd>
            <dt>How</dt>
            <dd>{data.instructions}</dd>
          </>
        )}
      </dl>

      {data.holder_list && (
        <Section title={`Holders (${data.holder_list.length})`}>
          {data.drift && !data.drift.group_found && <p className="notice error">The group {data.target} does not exist in the directory.</p>}
          {data.drift && data.drift.extra.length > 0 && (
            <p className="notice warn">
              In the group without a grant:{" "}
              {data.drift.extra.map((upn, i) => (
                <span key={upn}>
                  {i > 0 && ", "}
                  <Person upn={upn} />
                </span>
              ))}
            </p>
          )}
          {data.drift && data.drift.missing.length > 0 && <p className="notice">Granted, not in the group yet: {data.drift.missing.join(", ")}.</p>}
          {data.holder_list.length === 0 ? (
            <p className="muted">Nobody.</p>
          ) : (
            <table className="table">
              <tbody>
                {data.holder_list.map((h) => (
                  <tr key={h.upn}>
                    <td className="name">
                      <Person upn={h.upn} />
                    </td>
                    <td>{h.via.map((v) => (v === "direct" ? "direct" : `role ${v}`)).join(", ")}</td>
                    <td>{h.expires ?? "no end date"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Section>
      )}
    </>
  );
}
