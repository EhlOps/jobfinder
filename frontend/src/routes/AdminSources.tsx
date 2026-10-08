import { Navigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { useAuth } from "../auth/AuthProvider";
import { ErrorText } from "../components/fields";
import type { SourceRow } from "../lib/types";

const when = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : "—");

export default function AdminSources() {
  const { user } = useAuth();
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["admin-sources"], enabled: !!user?.is_admin, refetchInterval: 60_000,
    queryFn: () => api<SourceRow[]>("/api/admin/sources"),
  });
  const enable = useMutation({
    mutationFn: (id: number) => api<SourceRow>(`/api/admin/sources/${id}/enable`, { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin-sources"] }),
  });
  if (!user?.is_admin) return <Navigate to="/" replace />;
  const rows = q.data;

  return (
    <>
      <h2>Job sources</h2>
      <p className="muted">
        Health of each company job board. A board that keeps failing is switched off automatically; re-enable it once it is fixed.
      </p>
      <ErrorText error={q.error} />
      <ErrorText error={enable.error} />
      {rows && (
        <div className="card" style={{ overflowX: "auto" }}>
          <table>
            <thead>
              <tr>
                <th>Board</th><th>Origin</th><th>Status</th><th>Failures</th><th>Last success</th><th>Last fetch</th><th>Jobs</th><th>Last error</th><th />
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id}>
                  <td>{r.name} <span className="muted">{r.ats}/{r.slug}</span></td>
                  <td>{r.origin}</td>
                  <td>
                    {r.enabled ? <span className="ok">enabled</span> : <span className="error">disabled</span>}
                    {r.disabled_reason && <span className="muted"> · {r.disabled_reason}</span>}
                  </td>
                  <td>{r.consecutive_failures}</td>
                  <td>{when(r.last_success_at)}</td>
                  <td>{when(r.last_fetched_at)}</td>
                  <td>{r.job_count}</td>
                  <td>{r.last_error ?? "—"}</td>
                  <td>
                    {!r.enabled && (
                      <button disabled={enable.isPending} onClick={() => enable.mutate(r.id)}>Re-enable</button>
                    )}
                  </td>
                </tr>
              ))}
              {rows.length === 0 && <tr><td colSpan={9} className="muted">No sources yet.</td></tr>}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
