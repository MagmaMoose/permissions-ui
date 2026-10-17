import { useState } from "react";
import { api, ApiError } from "../api";
import { useSession } from "../App";
import { Loading, Person, useLoad } from "../components/ui";

export function Tasks() {
  const { notify } = useSession();
  const { data, error, reload } = useLoad(() => api.tasks(), []);
  const [busy, setBusy] = useState("");

  async function done(number: number, resource: string) {
    setBusy(`${number}/${resource}`);
    try {
      await api.done(number, resource);
      notify("Marked as set up.");
      reload();
    } catch (err) {
      notify(err instanceof ApiError ? err.message : String(err), "error");
    } finally {
      setBusy("");
    }
  }

  return (
    <>
      <div className="page-head">
        <h1>Tasks</h1>
        <p className="muted">Granted access that someone sets up by hand. Mark it done once it works.</p>
      </div>
      {!data ? (
        <Loading error={error} />
      ) : data.length === 0 ? (
        <p className="empty">Nothing to set up.</p>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>System</th>
              <th>For</th>
              <th>How</th>
              <th aria-label="Actions" />
            </tr>
          </thead>
          <tbody>
            {data.flatMap((t) =>
              t.resources.map((r) => (
                <tr key={`${t.request.number}/${r.id}`}>
                  <td className="name">
                    {r.name} <a href={`#/requests/${t.request.number}`}>#{t.request.number}</a>
                  </td>
                  <td>
                    <Person upn={t.request.person} />
                  </td>
                  <td>{r.instructions}</td>
                  <td className="actions-cell">
                    <button type="button" className="button small primary" disabled={busy === `${t.request.number}/${r.id}`} onClick={() => void done(t.request.number, r.id)}>
                      Done
                    </button>
                  </td>
                </tr>
              )),
            )}
          </tbody>
        </table>
      )}
    </>
  );
}
