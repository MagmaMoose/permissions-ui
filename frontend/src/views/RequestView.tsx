import { useState } from "react";
import { api, ApiError, type AccessRequest } from "../api";
import { Section, useSession } from "../App";
import { Dialog } from "../components/Dialog";
import { approverLabel, Loading, Person, StateBadge, TierBadge, useLoad } from "../components/ui";
import { when } from "../format";

export function RequestView({ number }: { number: number }) {
  const { catalog, notify } = useSession();
  const { data, error, reload } = useLoad(() => api.request(number), [number]);
  const [closing, setClosing] = useState<"decline" | "withdraw" | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);

  async function act(run: () => Promise<AccessRequest>, done: (r: AccessRequest) => string) {
    setBusy(true);
    try {
      const req = await run();
      notify(done(req));
      setClosing(null);
      reload();
    } catch (err) {
      notify(err instanceof ApiError ? err.message : String(err), "error");
    } finally {
      setBusy(false);
    }
  }

  if (!data) return <Loading error={error} />;
  const withdraw = closing === "withdraw";
  return (
    <>
      <div className="page-head">
        <h1>
          #{data.number} {data.kind === "revoke" ? "Remove " : ""}
          {data.items.map((i) => i.name).join(", ")}
        </h1>
        <p className="muted">
          For <Person upn={data.person} name={data.names[data.person]} />, asked by <Person upn={data.requested_by} name={data.names[data.requested_by]} /> on {when(Date.parse(data.created) / 1000, true)}
          {data.expires && ` · until ${data.expires}`}
        </p>
        <div className="actions">
          <StateBadge req={data} />
          {data.can_approve && (
            <button
              type="button"
              className="button primary small"
              disabled={busy}
              onClick={() => void act(() => api.approve(number), (r) => (r.state === "granted" ? "Approved and granted." : r.state === "approved" ? "Approved; merging." : "Approved; it still waits for another approver."))}
            >
              Approve
            </button>
          )}
          {data.can_decline && (
            <button type="button" className="button small" disabled={busy} onClick={() => setClosing("decline")}>
              Decline
            </button>
          )}
          {data.can_withdraw && (
            <button type="button" className="button small" disabled={busy} onClick={() => setClosing("withdraw")}>
              Withdraw
            </button>
          )}
          <a className="button small" href={data.url} target="_blank" rel="noreferrer">
            Pull request
          </a>
          {data.issue_url && (
            <a className="button small" href={data.issue_url} target="_blank" rel="noreferrer">
              Issue
            </a>
          )}
        </div>
      </div>

      <blockquote className="reason">{data.reason}</blockquote>
      {(data.state === "declined" || data.state === "withdrawn") && data.declined_by && (
        <p className="notice error">
          {data.state === "declined" ? "Declined" : "Withdrawn"} by {data.declined_by}: {data.decline_reason}
        </p>
      )}

      <Section title="What it gives">
        <table className="table">
          <tbody>
            {data.resources.map((r) => (
              <tr key={r.id}>
                <td className="name">
                  <a href={`#/resources/${r.id}`}>{r.name}</a>
                </td>
                <td>
                  <TierBadge tier={r.tier} />
                </td>
                <td>{r.kind === "manual" ? (data.done.includes(r.id) ? "set up" : data.state === "granted" ? "to be set up by hand" : "set up by hand") : ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>

      {data.kind === "grant" && (
        <Section title="Approvals">
          {data.approvals.length === 0 ? (
            <p className="muted">None yet.</p>
          ) : (
            <ul className="list">
              {data.approvals.map((a) => (
                <li key={a.upn}>
                  {a.name || a.upn} <span className="muted small">{a.at.replace("T", " ").replace("Z", " UTC")}</span>
                </li>
              ))}
            </ul>
          )}
          {data.outstanding.length > 0 && data.state === "pending" && (
            <>
              <p className="muted">Still waiting for:</p>
              <ul className="list">
                {data.outstanding.map((n) => (
                  <li key={n.resource}>
                    {catalog?.resources.find((r) => r.id === n.resource)?.name ?? n.resource}: {n.count > 1 ? `${n.count} approvals from ` : ""}
                    {n.approvers.map((a) => approverLabel(a, catalog)).join(" or ")}
                  </li>
                ))}
              </ul>
            </>
          )}
        </Section>
      )}

      {closing && (
        <Dialog title={withdraw ? "Withdraw the request" : "Decline the request"} onClose={() => setClosing(null)}>
          <form
            className="form"
            onSubmit={(e) => {
              e.preventDefault();
              void act(() => (withdraw ? api.withdraw : api.decline)(number, reason.trim()), () => (withdraw ? "Withdrawn." : "Declined."));
            }}
          >
            <div className="field">
              <label htmlFor="decline-reason">Why</label>
              <textarea id="decline-reason" data-autofocus required minLength={3} value={reason} onChange={(e) => setReason(e.target.value)} />
            </div>
            <footer className="dialog-footer">
              <button type="button" className="button" onClick={() => setClosing(null)}>
                Cancel
              </button>
              <button type="submit" className="button danger" disabled={busy || reason.trim().length < 3}>
                {withdraw ? "Withdraw" : "Decline"}
              </button>
            </footer>
          </form>
        </Dialog>
      )}
    </>
  );
}
