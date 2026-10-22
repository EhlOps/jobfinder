import { Navigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { useAuth } from "../auth/AuthProvider";
import { ErrorText } from "../components/fields";
import type { Schedule, ScheduleRow } from "../lib/types";

const when = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : "—");
const secs = (s: number | null) => (s === null ? "—" : s < 9.95 ? `${s.toFixed(1)}s` : `${Math.round(s)}s`);

function status(r: ScheduleRow): string {
  if (r.active.length) return r.active.map((t) => `${t.kind} (${t.status})`).join(", ");
  return r.skip || r.reason || "—";
}

export default function AdminSchedule() {
  const { user } = useAuth();
  const q = useQuery({
    queryKey: ["admin-schedule"], enabled: !!user?.is_admin, refetchInterval: 30_000,
    queryFn: () => api<Schedule>("/api/admin/schedule"),
  });
  if (!user?.is_admin) return <Navigate to="/" replace />;
  const s = q.data;

  return (
    <>
      <h2>Background schedule</h2>
      <p className="muted">
        A planner runs every few minutes and queues matching for the users who need it most, a few at a time, so the shared
        Claude subscription is never hit all at once.
      </p>
      <ErrorText error={q.error} />
      {s && (
        <div className="card">
          {s.backoff_until
            ? <p className="error">Paused until {when(s.backoff_until)} after a Claude rate-limit or auth error.</p>
            : <p className="ok">Running normally.</p>}
          <p className="muted">
            {s.in_flight} of {s.max_per_tick} slots in use · next planner run about {when(s.next_tick)}
          </p>
          <p className="muted">
            Job boards: {s.ingest.adaptive
              ? `adaptive, tick every ${s.ingest.tick_minutes} min`
              : `every ${s.ingest.interval_hours} h`}
            {s.ingest.tiers.map((t) => (
              <span key={t.tier ?? "none"}>
                {" · "}{t.tier === null ? "untiered" : `tier ${t.tier}`}: {t.boards} boards
                {s.ingest.adaptive ? ` (${t.due} due)` : ""}
              </span>
            ))}
          </p>
        </div>
      )}
      {s && (
        <div className="card" style={{ overflowX: "auto" }}>
          <table>
            <thead>
              <tr>
                <th>User</th><th>Last seen</th><th>Last run</th><th>Next due</th><th>Budget left</th><th>Why / status</th>
              </tr>
            </thead>
            <tbody>
              {s.users.map((r) => (
                <tr key={r.user_id}>
                  <td>{r.email}</td>
                  <td>{when(r.last_seen_at)}</td>
                  <td>
                    {when(r.last_run_at)}
                    {r.last_scored !== null && <span className="muted"> · {r.last_scored} scored{r.pending ? `, ${r.pending} pending` : ""}</span>}
                  </td>
                  <td>{when(r.next_due_at)}</td>
                  <td>{r.budget_left === null ? "no limit" : r.budget_left}</td>
                  <td>{r.reason && !r.active.length ? `${r.reason} → ` : ""}{status(r)}</td>
                </tr>
              ))}
              {s.users.length === 0 && <tr><td colSpan={6} className="muted">No active users yet.</td></tr>}
            </tbody>
          </table>
        </div>
      )}
      {s && (
        <div className="card" style={{ overflowX: "auto" }}>
          <h3>Queues (last 24h)</h3>
          <table>
            <thead>
              <tr>
                <th>Queue</th><th>Tasks</th><th>Wait p50 / p95</th><th>Run p95</th><th>Waits &gt;60s</th>
              </tr>
            </thead>
            <tbody>
              {s.lanes.map((l) => (
                <tr key={l.queue}>
                  <td>{l.queue}</td>
                  <td>{l.tasks}</td>
                  <td>{secs(l.wait_p50)} / {secs(l.wait_p95)}</td>
                  <td>{secs(l.run_p95)}</td>
                  <td>{l.slow_waits}</td>
                </tr>
              ))}
              {s.lanes.length === 0 && <tr><td colSpan={5} className="muted">No tasks claimed in the last day.</td></tr>}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
