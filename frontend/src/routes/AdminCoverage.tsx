import { useState, type FormEvent } from "react";
import { Navigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { useAuth } from "../auth/AuthProvider";
import { ErrorText } from "../components/fields";
import { safeHttpUrl } from "../lib/format";
import type { BoardStatus, Coverage, CoverageRow, CoverageTarget } from "../lib/types";

const KEY = ["admin-coverage"];
const when = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : "—");
const STATUS_CLASS: Record<BoardStatus, string> = { ok: "ok", failing: "error", disabled: "error", none: "muted" };
type Tab = "gaps" | "all";
type PinBody = { company_id: number } | { name: string; domain: string };
type UnpinBody = { company_id: number; exclude?: boolean } | { target_id: number };

function Careers({ url, label }: { url: string | null; label: string }) {
  const href = safeHttpUrl(url);
  return href ? <a href={href} target="_blank" rel="noreferrer noopener">{label}</a> : <span>{label}</span>;
}

export default function AdminCoverage() {
  const { user } = useAuth();
  const qc = useQueryClient();
  const [tab, setTab] = useState<Tab>("gaps");
  const [name, setName] = useState("");
  const [domain, setDomain] = useState("");
  const q = useQuery({
    queryKey: KEY, enabled: !!user?.is_admin, refetchInterval: 60_000,
    queryFn: () => api<Coverage>("/api/admin/coverage"),
  });
  const refresh = () => qc.invalidateQueries({ queryKey: KEY });
  const pin = useMutation({
    mutationFn: (body: PinBody) => api("/api/admin/coverage/pin", { json: body }),
    onSuccess: refresh,
  });
  const add = useMutation({
    mutationFn: (body: PinBody) => api("/api/admin/coverage/pin", { json: body }),
    onSuccess: () => { setName(""); setDomain(""); return refresh(); },
  });
  const unpin = useMutation({
    mutationFn: (body: UnpinBody) => api("/api/admin/coverage/unpin", { json: body }),
    onSuccess: refresh,
  });
  const resolve = useMutation({
    mutationFn: (path: string) => api(path, { method: "POST" }),
    onSuccess: refresh,
  });
  const rescore = useMutation({
    mutationFn: () => api("/api/admin/coverage/rescore", { method: "POST" }),
    onSuccess: refresh,
  });
  if (!user?.is_admin) return <Navigate to="/" replace />;

  const data = q.data;
  const gaps = data?.companies.filter((r) => r.gap) ?? [];
  const rows = tab === "gaps" ? gaps : data?.companies ?? [];
  const busy = pin.isPending || unpin.isPending || resolve.isPending;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    add.mutate({ name: name.trim(), domain: domain.trim() });
  };

  const pinButtons = (r: CoverageRow) => (
    <>
      {r.pinned === true && (
        <button className="secondary" disabled={busy} onClick={() => unpin.mutate({ company_id: r.company_id })}>Unpin</button>
      )}
      {r.pinned === null && (
        <>
          <button className="secondary" disabled={busy} onClick={() => pin.mutate({ company_id: r.company_id })}>Pin</button>{" "}
          <button className="link" disabled={busy} onClick={() => unpin.mutate({ company_id: r.company_id, exclude: true })}>
            Not coveted
          </button>
        </>
      )}
      {r.pinned === false && (
        <button className="secondary" disabled={busy} onClick={() => unpin.mutate({ company_id: r.company_id })}>Restore</button>
      )}
    </>
  );

  const targetRow = (t: CoverageTarget) => (
    <tr key={`t${t.id}`}>
      <td><Careers url={t.careers_url} label={t.name} /> <span className="muted">{t.domain}</span></td>
      <td>—</td>
      <td><span className="chip">{t.source === "admin" ? "pinned" : t.source}</span></td>
      <td><span className="muted">no board yet</span></td>
      <td>—</td>
      <td>{when(t.last_resolved_at)}</td>
      <td>{t.last_error ?? "—"}</td>
      <td>
        <button disabled={busy} onClick={() => resolve.mutate(`/api/admin/coverage/targets/${t.id}/resolve`)}>Try to resolve now</button>{" "}
        <button className="link" disabled={busy} onClick={() => unpin.mutate({ target_id: t.id })}>Remove</button>
      </td>
    </tr>
  );

  return (
    <>
      <h2>Coverage</h2>
      <p className="muted">
        How sought-after each employer is (from its prestige, outside job lists, what users paste and save, and how its jobs
        match), and the coveted ones whose job board we can't read yet. Gaps are sent through the resolver every week.
      </p>
      <ErrorText error={q.error} />
      <ErrorText error={pin.error ?? unpin.error ?? resolve.error ?? rescore.error} />

      <form className="card" onSubmit={submit}>
        <h3>Add an employer</h3>
        <p className="muted">Pin an employer we don't have a board for. We look for its job board at its careers page.</p>
        <div className="filters">
          <input aria-label="Employer name" placeholder="Name, e.g. Acme Robotics" value={name} maxLength={200}
                 onChange={(e) => setName(e.target.value)} />
          <input aria-label="Domain" placeholder="Domain, e.g. acme.com" value={domain} maxLength={255}
                 onChange={(e) => setDomain(e.target.value)} />
          <button type="submit" disabled={add.isPending || !name.trim() || !domain.trim()}>Add</button>
        </div>
        <ErrorText error={add.error} />
      </form>

      <ul className="tabs">
        <li>
          <button type="button" className={tab === "gaps" ? "tab on" : "tab"} onClick={() => setTab("gaps")}>
            Gaps <span className="count">{gaps.length + (data?.targets.length ?? 0)}</span>
          </button>
        </li>
        <li>
          <button type="button" className={tab === "all" ? "tab on" : "tab"} onClick={() => setTab("all")}>
            All coveted <span className="count">{data?.companies.length ?? 0}</span>
          </button>
        </li>
        <li style={{ marginLeft: "auto" }}>
          <button type="button" className="secondary" disabled={rescore.isPending} onClick={() => rescore.mutate()}>
            {rescore.isPending ? "Queuing…" : "Rescore now"}
          </button>
          {rescore.isSuccess && <span className="muted"> Queued; scores refresh in a few minutes.</span>}
        </li>
      </ul>

      {data && (
        <div className="card" style={{ overflowX: "auto" }}>
          <table>
            <thead>
              <tr>
                <th>Employer</th><th>Tier</th><th>Score</th><th>Board</th><th>Jobs</th><th>Last tried</th><th>Last error</th><th />
              </tr>
            </thead>
            <tbody>
              {tab === "gaps" && data.targets.map(targetRow)}
              {rows.map((r) => (
                <tr key={r.company_id}>
                  <td>
                    <Careers url={r.careers_url} label={r.name} /> <span className="muted">{r.ats}/{r.slug}</span>
                  </td>
                  <td>
                    {r.tier}
                    {r.pinned === true && <> <span className="chip">pinned</span></>}
                    {r.pinned === false && <> <span className="chip">not coveted</span></>}
                  </td>
                  <td>{r.score.toFixed(1)}</td>
                  <td>
                    <span className={STATUS_CLASS[r.board_status]}>{r.board_status}</span>
                    {r.recipe_state && <span className="muted"> · recipe {r.recipe_state}</span>}
                    <div className="muted">last success {when(r.last_success_at)}</div>
                  </td>
                  <td>{r.job_count}</td>
                  <td>{when(r.last_resolved_at)}</td>
                  <td>{r.resolve_error ?? r.last_error ?? "—"}</td>
                  <td>
                    {r.board_status !== "ok" && (
                      <button disabled={busy} onClick={() => resolve.mutate(`/api/admin/coverage/${r.company_id}/resolve`)}>
                        Try to resolve now
                      </button>
                    )}{" "}
                    {pinButtons(r)}
                  </td>
                </tr>
              ))}
              {rows.length === 0 && (tab === "all" || data.targets.length === 0) && (
                <tr><td colSpan={8} className="muted">{tab === "gaps" ? "No gaps: every coveted employer has a working board." : "No coveted employers yet."}</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
