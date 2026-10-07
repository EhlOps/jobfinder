import { safeHttpUrl } from "../lib/format";
import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import type { MatchDetail as Detail } from "../lib/types";
import { ErrorText } from "../components/fields";
import { JobChips, ScoreBadge, StatusActions } from "../components/MatchBits";
import { CoverLetterPanel } from "../components/CoverLetterPanel";

export default function MatchDetail() {
  const { id } = useParams();
  const m = useQuery({ queryKey: ["match", Number(id)], queryFn: () => api<Detail>(`/api/matches/${id}`) });

  if (m.isLoading) return <p className="muted">Loading…</p>;
  if (m.error || !m.data) return <><p><Link to="/">← Back to matches</Link></p><ErrorText error={m.error ?? new Error("Not found")} /></>;
  const d = m.data;

  return (
    <>
      <p><Link to="/">← Back to matches</Link></p>
      <article className="card detail">
        <div className="match">
          <ScoreBadge score={d.score} verdict={d.verdict} />
          <div className="match-body">
            <h2 style={{ marginBottom: 2 }}>{d.job.title}</h2>
            <div className="company">{d.job.company}</div>
            <JobChips job={d.job} />
            <div className="actions-inline">
              <StatusActions match={d} />
              {safeHttpUrl(d.job.url) && <a href={safeHttpUrl(d.job.url)!} target="_blank" rel="noreferrer noopener" className="primary-link">Apply on company site ↗</a>}
            </div>
          </div>
        </div>

        <h3>Why this score</h3>
        <ul className="reasons">{d.reasons.map((r) => <li key={r}>{r}</li>)}</ul>
        <p className="hint">
          Confidence {Math.round(d.confidence * 100)}%{d.confidence < 0.6 ? " — your profile doesn't say enough about some requirements" : ""}.
          {d.stale ? " Scored before your latest profile update." : ""}
        </p>

        {d.gaps.length > 0 && (<><h3>Gaps</h3><ul className="reasons">{d.gaps.map((g) => <li key={g}>{g}</li>)}</ul></>)}
        {d.unknowns.length > 0 && (
          <>
            <h3>Things that would sharpen this score</h3>
            <ul className="reasons">{d.unknowns.map((u) => <li key={u.question}>{u.question}{u.why ? <span className="muted"> — {u.why}</span> : null}</li>)}</ul>
          </>
        )}
      </article>

      <CoverLetterPanel matchId={d.id} />

      <article className="card">
        <h3 style={{ marginTop: 0 }}>Job description</h3>
        <div className="description">{d.description || "No description was published for this posting."}</div>
      </article>
    </>
  );
}
