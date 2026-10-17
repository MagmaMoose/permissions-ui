import { useState } from "react";
import { api, ApiError, type Item } from "../api";
import { go, Section, useSession } from "../App";
import { Confirm } from "../components/Dialog";
import { Loading, TierBadge, Tiers, useLoad } from "../components/ui";

export function PersonView({ upn }: { upn: string }) {
  const { me, notify } = useSession();
  const { data, error, reload } = useLoad(() => api.person(upn), [upn]);
  const [removing, setRemoving] = useState<{ items: Item[] | null; label: string } | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const manages = me.admin || me.lead_of.length > 0;
  const self = me.upn === upn.toLowerCase();

  async function remove() {
    if (!removing) return;
    setBusy(true);
    try {
      const req = await api.remove({ person: upn, items: removing.items, reason: reason.trim() || "No longer needed" });
      notify(`Removal #${req.number} ${req.state === "granted" ? "is done" : "is open"}.`);
      setRemoving(null);
      reload();
    } catch (err) {
      notify(err instanceof ApiError ? err.message : String(err), "error");
    } finally {
      setBusy(false);
    }
  }

  if (!data) return <Loading error={error} />;
  return (
    <>
      <div className="page-head">
        <h1>{data.user?.name || upn}</h1>
        <p className="muted">
          {upn}
          {data.user && (data.user.job || data.user.department) && ` · ${[data.user.job, data.user.department].filter(Boolean).join(", ")}`}
          {data.user && !data.user.enabled && " · account disabled"}
          {!data.user && " · not in the directory"}
          {data.teams.length > 0 && ` · leads ${data.teams.join(", ")}`}
        </p>
        <div className="actions">
          <Tiers tiers={data.tiers} />
          {!me.read_only && (manages || self) && (
            <a className="button small" href={`#/request?person=${encodeURIComponent(upn)}`}>
              Ask for access{self ? "" : " for them"}
            </a>
          )}
          {!me.read_only && (manages || self) && data.grants.length > 0 && (
            <button type="button" className="button small danger" onClick={() => setRemoving({ items: null, label: "everything they hold" })}>
              Remove everything
            </button>
          )}
        </div>
      </div>

      {(data.extra.length > 0 || data.missing.length > 0 || data.conflicts.length > 0 || data.unknown_groups.length > 0) && (
        <div className="flags">
          {data.extra.length > 0 && <p className="notice warn">In these groups without a grant: {data.extra.join(", ")}.</p>}
          {data.missing.length > 0 && <p className="notice">Granted, but not in the group yet: {data.missing.join(", ")}.</p>}
          {data.conflicts.length > 0 && <p className="notice error">Holds access the catalog keeps apart: {data.conflicts.map((p) => p.join(" and ")).join("; ")}.</p>}
          {data.unknown_groups.length > 0 && <p className="notice warn">In groups no resource describes: {data.unknown_groups.join(", ")}.</p>}
        </div>
      )}

      <Section title="Holds">
        {data.holdings.length === 0 ? (
          <p className="muted">Nothing through the catalog.</p>
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
              {data.holdings.map((h) => (
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
      </Section>

      <Section title="Grants">
        {data.grants.length === 0 ? (
          <p className="muted">None.</p>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Grant</th>
                <th>Why</th>
                <th>Approved by</th>
                <th>Granted</th>
                <th>Until</th>
                <th aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {data.grants.map((g) => {
                const item: Item = g.role ? { role: g.role } : { resource: g.resource ?? "" };
                const label = g.role ? `role ${g.role}` : (g.resource ?? "");
                return (
                  <tr key={label} className={g.active ? undefined : "muted"}>
                    <td className="name">{g.role ? <span>role {g.role}</span> : <a href={`#/resources/${g.resource}`}>{g.resource}</a>}</td>
                    <td>{g.reason}</td>
                    <td>{g.approved_by.join(", ") || "—"}</td>
                    <td>{g.request ? <a href={`#/requests/${g.request}`}>{g.granted}</a> : g.granted}</td>
                    <td>{g.expires ?? "—"}{!g.active && " (expired)"}</td>
                    <td className="actions-cell">
                      {!me.read_only && (manages || self) && (
                        <button type="button" className="button small" onClick={() => setRemoving({ items: [item], label })}>
                          Remove
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Section>

      {removing && (
        <Confirm title={`Remove ${removing.label}`} action="Remove" danger busy={busy} onConfirm={() => void remove()} onClose={() => setRemoving(null)}>
          <p>
            This removes {removing.label} from {upn} straight away; removing access needs no approval.
          </p>
          <div className="field">
            <label htmlFor="removal-reason">Why</label>
            <input id="removal-reason" data-autofocus value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Left the team" />
          </div>
        </Confirm>
      )}
      {self && data.grants.length === 0 && !me.read_only && (
        <button type="button" className="button primary" onClick={() => go("/request")}>
          Ask for access
        </button>
      )}
    </>
  );
}
