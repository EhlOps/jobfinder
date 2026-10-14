import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { safeHttpUrl, timeAgo } from "../lib/format";
import { ErrorText } from "../components/fields";
import type { JobLink, JobLinkDetail } from "../lib/types";

const STATUS: Record<JobLink["status"], { label: string; cls: string }> = {
  resolving: { label: "Reading their job board…", cls: "chip" },
  resolved: { label: "Board added", cls: "chip good" },
  unresolved: { label: "Couldn't read this one", cls: "chip warn" },
};

function Roles({ link }: { link: JobLink }) {
  const q = useQuery({
    queryKey: ["job-link", link.id, link.status],
    queryFn: () => api<JobLinkDetail>(`/api/job-links/${link.id}`),
  });
  if (q.isPending) return <p className="muted">Loading roles…</p>;
  if (q.error) return <ErrorText error={q.error} />;
  const jobs = q.data.jobs;
  if (jobs.length === 0) return <p className="muted">No open roles on this board right now.</p>;
  return (
    <ul className="items">
      {jobs.map((j) => {
        const href = safeHttpUrl(j.url);
        return (
          <li key={j.id}>
            <span>
              {j.match_id ? <Link to={`/matches/${j.match_id}`}>{j.title}</Link> : j.title}
              {j.id === link.pasted_job_id && <span className="chip good"> the one you pasted</span>}
              {j.location && <span className="muted"> · {j.location}</span>}
            </span>
            <span className="actions-inline" style={{ marginTop: 0 }}>
              {j.score !== null ? <span className="chip">{j.score}% match</span> : <span className="hint">not scored yet</span>}
              {href && <a href={href} target="_blank" rel="noreferrer noopener" className="plain">Open ↗</a>}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

export default function JobLinks() {
  const qc = useQueryClient();
  const [url, setUrl] = useState("");
  const [open, setOpen] = useState<number | null>(null);
  const list = useQuery({
    queryKey: ["job-links"],
    queryFn: () => api<JobLink[]>("/api/job-links"),
    refetchInterval: (query) => (query.state.data?.some((l) => l.status === "resolving") ? 3000 : false),
  });
  const add = useMutation({
    mutationFn: (u: string) => api<JobLink>("/api/job-links", { json: { url: u } }),
    onSuccess: (link) => {
      setUrl("");
      if (link.status === "resolved") setOpen(link.id);
      qc.invalidateQueries({ queryKey: ["job-links"] });
    },
  });
  const remove = useMutation({
    mutationFn: (id: number) => api<void>(`/api/job-links/${id}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["job-links"] }),
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (url.trim()) add.mutate(url.trim());
  };

  return (
    <>
      <h2>Add a job</h2>
      <p className="muted">
        Paste a link to any job you like. We'll find the company's job board, pull in every role they have open,
        and match them to you. The board is shared, so everyone using JobFinder benefits.
      </p>
      <form className="card row" onSubmit={submit}>
        <label className="field">
          <span className="field-label">Job link</span>
          <input type="url" inputMode="url" placeholder="https://boards.greenhouse.io/acme/jobs/123" value={url}
                 maxLength={2048} onChange={(e) => setUrl(e.target.value)} />
        </label>
        <div style={{ flex: "0 0 auto", marginBottom: 12 }}>
          <button type="submit" disabled={add.isPending || !url.trim()}>{add.isPending ? "Adding…" : "Find their jobs"}</button>
        </div>
      </form>
      <ErrorText error={add.error} />
      <ErrorText error={list.error} />
      <ErrorText error={remove.error} />

      {list.data?.map((l) => {
        const href = safeHttpUrl(l.url);
        const s = STATUS[l.status];
        return (
          <article key={l.id} className="match">
            <div className="match-body">
              <h3>{l.company ?? "Unknown company"}</h3>
              <div className="company" style={{ overflowWrap: "anywhere" }}>
                {href ? <a href={href} target="_blank" rel="noreferrer noopener">{l.url}</a> : l.url}
                {timeAgo(l.created_at) && <span> · added {timeAgo(l.created_at)}</span>}
              </div>
              <span className={s.cls}>{s.label}</span>
              {l.status === "resolved" && <span className="chip">{l.job_count} open roles</span>}
              {l.error && <p className={l.status === "unresolved" ? "error" : "hint"}>{l.error}</p>}
              <div className="actions-inline">
                {l.status === "resolved" && (
                  <button type="button" className="toggle" onClick={() => setOpen(open === l.id ? null : l.id)}>
                    {open === l.id ? "Hide roles" : `See ${l.job_count} roles`}
                  </button>
                )}
                <button type="button" className="link" disabled={remove.isPending} onClick={() => remove.mutate(l.id)}>Remove</button>
              </div>
              {open === l.id && <Roles link={l} />}
            </div>
          </article>
        );
      })}
      {list.data?.length === 0 && <p className="hint">No links yet. Links from Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Recruitee, BambooHR and Workday work today.</p>}
    </>
  );
}
