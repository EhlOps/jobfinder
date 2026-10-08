import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { confetti } from "../lib/confetti";
import { HIRE_LABEL, SENIORITY_LABEL, VERDICT_LABEL, WORKPLACE_LABEL, formatSalary, timeAgo } from "../lib/format";
import type { Match, MatchJob, MatchStatus } from "../lib/types";

export function ScoreBadge({ score, verdict }: { score: number; verdict: Match["verdict"] }) {
  return (
    <div className={`score ${verdict}`} title={VERDICT_LABEL[verdict]}>
      <strong>{score}</strong>
      <span>{VERDICT_LABEL[verdict]}</span>
    </div>
  );
}

export function JobChips({ job }: { job: MatchJob }) {
  const salary = formatSalary(job.salary_min, job.salary_max, job.salary_currency);
  const chips = [
    job.location && job.location.length < 60 ? job.location : job.location ? `${job.location.slice(0, 56)}…` : null,
    job.workplace_type ? WORKPLACE_LABEL[job.workplace_type] : null,
    job.seniority ? SENIORITY_LABEL[job.seniority] : null,
    salary,
    timeAgo(job.posted_at) ? `Posted ${timeAgo(job.posted_at)}` : null,
    job.is_active ? null : "Closed",
  ].filter(Boolean) as string[];
  return (
    <div className="chips">
      {chips.map((c) => (
        <span key={c} className={`chip${c === "Closed" ? " closed" : ""}`}>{c}</span>
      ))}
    </div>
  );
}

/** Save / Applied / Dismiss buttons. Clicking the active state again moves the match back to "new". */
export function StatusActions({ match }: { match: Pick<Match, "id" | "status"> }) {
  const qc = useQueryClient();
  const set = useMutation({
    mutationFn: (status: MatchStatus) => api<Match>(`/api/matches/${match.id}`, { method: "PATCH", json: { status } }),
    onSuccess: (_data, status) => {
      if (status === "applied") confetti();
      qc.invalidateQueries({ queryKey: ["matches"] });
      qc.invalidateQueries({ queryKey: ["match", match.id] });
    },
  });
  const toggle = (target: MatchStatus) => set.mutate(match.status === target ? "new" : target);
  const btn = (target: MatchStatus, label: string, activeLabel: string) => (
    <button
      type="button"
      className={match.status === target ? "toggle on" : "toggle"}
      disabled={set.isPending}
      onClick={() => toggle(target)}
    >
      {match.status === target ? activeLabel : label}
    </button>
  );
  return (
    <div className="actions-inline">
      {btn("saved", "Save", "Saved ✓")}
      {btn("applied", "Mark applied", "Applied ✓")}
      {btn("dismissed", "Dismiss", "Dismissed — undo")}
    </div>
  );
}

export function HireBadge({ verdict }: { verdict?: string }) {
  if (!verdict) return null;
  return <span className={`chip${verdict === "yes" ? " good" : verdict === "no" ? " closed" : " warn"}`}>{HIRE_LABEL[verdict]}</span>;
}
