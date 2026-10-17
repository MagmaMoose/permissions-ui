import { useState } from "react";
import { api, ApiError } from "../api";
import { go, useSession } from "../App";
import { PersonPicker, TierBadge } from "../components/ui";
import { approvalsNeeded } from "./NewRequest";

export function Onboard({ team: initialTeam }: { team: string }) {
  const { catalog, me, notify } = useSession();
  const teams = (catalog?.teams ?? []).filter((t) => me.admin || me.lead_of.includes(t.id));
  const [team, setTeam] = useState(initialTeam && teams.some((t) => t.id === initialTeam) ? initialTeam : (teams[0]?.id ?? ""));
  const [person, setPerson] = useState("");
  const [busy, setBusy] = useState(false);
  if (!catalog) return null;
  const chosen = catalog.teams.find((t) => t.id === team);
  const items = (chosen?.roles ?? []).map((role) => ({ role }));
  const resources = [...new Set(items.flatMap((i) => catalog.roles.find((r) => r.id === i.role)?.resources ?? []))]
    .map((id) => catalog.resources.find((r) => r.id === id))
    .filter((r) => r !== undefined);
  const needs = approvalsNeeded(catalog, items);

  async function submit() {
    if (!chosen) return;
    setBusy(true);
    try {
      const req = await api.ask({ person, items, reason: `Joins ${chosen.name}` });
      notify(req.state === "granted" ? `${person} has everything ${chosen.name} needs.` : `Request #${req.number} is open; it waits for approval.`);
      go(`/requests/${req.number}`);
    } catch (err) {
      notify(err instanceof ApiError ? err.message : String(err), "error");
    } finally {
      setBusy(false);
    }
  }

  if (teams.length === 0) return <p className="empty">You lead no team, so there is nobody to onboard.</p>;
  return (
    <>
      <div className="page-head">
        <h1>Onboard someone</h1>
        <p className="muted">One request with everything the team gives a new member. Your request counts as your approval.</p>
      </div>
      <form
        className="form request-form"
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <div className="field">
          <label>Who</label>
          <PersonPicker value={person} onPick={setPerson} autoFocus />
        </div>
        <div className="field">
          <label htmlFor="team">Team</label>
          <select id="team" value={team} onChange={(e) => setTeam(e.target.value)}>
            {teams.map((t) => (
              <option key={t.id} value={t.id}>
                {t.name}
              </option>
            ))}
          </select>
        </div>
        {chosen && (
          <div className="field">
            <label>They get</label>
            {items.length === 0 ? (
              <p className="muted">This team gives new members nothing by default. Add roles to it in the catalog.</p>
            ) : (
              <ul className="list">
                {resources.map((r) => (
                  <li key={r.id}>
                    {r.name} <TierBadge tier={r.tier} /> {r.kind === "manual" && <span className="muted small">set up by hand</span>}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
        {needs.length > 0 && (
          <p className="muted small">Approved by {needs.map((n) => `${n.count > 1 ? `${n.count} of ` : ""}${n.approvers}`).join("; ")}.</p>
        )}
        <footer className="dialog-footer">
          <button type="submit" className="button primary" disabled={busy || !person || items.length === 0}>
            {busy ? "Opening…" : "Onboard"}
          </button>
        </footer>
      </form>
    </>
  );
}
