import { safeHttpUrl } from "../lib/format";
import { Link, useParams } from "react-router-dom";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import type { MatchDetail as Detail, Requirement } from "../lib/types";
import { ErrorText } from "../components/fields";
import { HireBadge, JobChips, ScoreBadge, StatusActions } from "../components/MatchBits";
import { CoverLetterPanel } from "../components/CoverLetterPanel";
import { ResumePanel } from "../components/ResumePanel";

const STATUS_LABEL = { met: "Met", partial: "Partly", unknown: "Not in profile", unmet: "Not met" } as const;

/** What the posting asks for, checked against the profile; open ones can be answered right here. */
function Requirements({ matchId, reqs }: { matchId: number; reqs: Requirement[] }) {
  const qc = useQueryClient();
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const save = useMutation({
    mutationFn: () => api(`/api/matches/${matchId}/answers`, {
      json: { answers: Object.entries(answers).filter(([, a]) => a.trim()).map(([question, answer]) => ({ question, answer: answer.trim() })) },
    }),
    onSuccess: () => { setAnswers({}); qc.invalidateQueries({ queryKey: ["match", matchId] }); qc.invalidateQueries({ queryKey: ["matches"] }); },
  });
  const any = Object.values(answers).some((a) => a.trim());
  return (
    <>
      <h3>Requirements checked against your profile</h3>
      <table className="reqs">
        <tbody>
          {reqs.map((r) => (
            <tr key={r.requirement}>
              <td>
                {r.requirement} <span className="muted">· {r.importance === "must" ? "required" : "nice to have"}</span>
                {r.evidence && <div className="hint">{r.evidence}</div>}
                {r.question && r.status !== "met" && (
                  <label className="field" style={{ margin: "6px 0 0" }}>
                    <span className="hint">{r.question}</span>
                    <textarea rows={2} value={answers[r.question] ?? ""} onChange={(e) => setAnswers((a) => ({ ...a, [r.question]: e.target.value }))} />
                  </label>
                )}
              </td>
              <td><span className={`chip req-${r.status}`}>{STATUS_LABEL[r.status]}</span></td>
            </tr>
          ))}
        </tbody>
      </table>
      <ErrorText error={save.error} />
      {(any || save.isSuccess) && (
        <div className="actions">
          <button disabled={!any || save.isPending} onClick={() => save.mutate()}>{save.isPending ? "Saving…" : "Save answers & re-score"}</button>
        </div>
      )}
      {save.isSuccess && !any && <p className="ok">Saved. This job is being re-scored.</p>}
    </>
  );
}

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

        {d.recruiter_take && <p className="callout"><HireBadge verdict={d.hire_verdict} /> {d.recruiter_take}</p>}
        <h3>Why this score</h3>
        <ul className="reasons">{d.reasons.map((r) => <li key={r}>{r}</li>)}</ul>
        <p className="hint">
          Confidence {Math.round(d.confidence * 100)}%{d.confidence < 0.6 ? " — your profile doesn't say enough about some requirements" : ""}.
          {d.stale ? " Scored before your latest profile update." : ""}
        </p>

        {d.requirements?.length > 0 && <Requirements matchId={d.id} reqs={d.requirements} />}
        {d.gaps.length > 0 && (<><h3>Gaps</h3><ul className="reasons">{d.gaps.map((g) => <li key={g}>{g}</li>)}</ul></>)}
        {!d.requirements?.length && d.unknowns.length > 0 && (
          <>
            <h3>Things that would sharpen this score</h3>
            <ul className="reasons">{d.unknowns.map((u) => <li key={u.question}>{u.question}{u.why ? <span className="muted"> — {u.why}</span> : null}</li>)}</ul>
          </>
        )}
      </article>

      <ResumePanel matchId={d.id} />

      <CoverLetterPanel matchId={d.id} />

      <article className="card">
        <h3 style={{ marginTop: 0 }}>Job description</h3>
        <div className="description">{d.description || "No description was published for this posting."}</div>
      </article>
    </>
  );
}
