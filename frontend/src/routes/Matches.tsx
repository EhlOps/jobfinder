import { safeHttpUrl } from "../lib/format";
import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { useTask } from "../lib/useTask";
import type { MatchList, MatchStatus, Profile } from "../lib/types";
import { AiErrorNotice } from "../components/AiErrorNotice";
import { ErrorText } from "../components/fields";
import { HireBadge, JobChips, ScoreBadge, StatusActions } from "../components/MatchBits";

// Students and recent graduates see internships and new-grad roles first (they can untick the box).
const EARLY_STAGES = ["student", "final_year", "recent_grad", "new_grad"];

const TABS: [MatchStatus, string][] = [["new", "To review"], ["saved", "Saved"], ["applied", "Applied"], ["dismissed", "Dismissed"]];

function useDebounced<T>(value: T, ms = 300): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

export default function Matches() {
  const qc = useQueryClient();
  const [params, setParams] = useSearchParams();
  const [status, setStatus] = useState<MatchStatus>("new");
  const [minScore, setMinScore] = useState(50);
  const [workplace, setWorkplace] = useState("");
  const [sort, setSort] = useState<"score" | "recent">("score");
  const [search, setSearch] = useState("");
  const [earlyChoice, setEarlyChoice] = useState<boolean | null>(null); // null = follow the default for this person
  const q = useDebounced(search);
  // The wizard sends us here with ?task=<id> so we can show the first matches as they arrive.
  const [taskId, setTaskId] = useState<number | null>(params.get("task") ? Number(params.get("task")) : null);

  const task = useTask<{ scored?: number }>(taskId);
  const running = taskId !== null && task.data?.status !== "done" && task.data?.status !== "failed";

  // The default filter comes from the profile, so hold the first list fetch until it is known (or has failed).
  const profile = useQuery({ queryKey: ["profile"], staleTime: 5 * 60_000, retry: false, queryFn: () => api<Profile>("/api/profile") });
  const stage = profile.data?.career_stage;
  const early = earlyChoice ?? (stage ? EARLY_STAGES.includes(stage) : false);

  const list = useQuery({
    queryKey: ["matches", status, minScore, workplace, sort, q, early],
    placeholderData: keepPreviousData,
    enabled: profile.isSuccess || profile.isError,
    refetchInterval: running ? 10_000 : false, // partial progress is saved per job, so show it as it lands
    queryFn: () => {
      const p = new URLSearchParams({ status, min_score: String(minScore), sort });
      if (workplace) p.set("workplace", workplace);
      if (early) p.set("early_career", "true");
      if (q.trim()) p.set("q", q.trim());
      return api<MatchList>(`/api/matches?${p}`);
    },
  });

  useEffect(() => {
    if (task.data?.status === "done" || task.data?.status === "failed") qc.invalidateQueries({ queryKey: ["matches"] });
  }, [task.data?.status, qc]);

  const refresh = useMutation({
    mutationFn: () => api<{ task_id: number }>("/api/matches/refresh", { method: "POST" }),
    onSuccess: (r) => { setTaskId(r.task_id); setParams({}, { replace: true }); },
  });

  const questions = useQuery({ queryKey: ["questions"], staleTime: 60_000, queryFn: () => api<{ questions: unknown[] }>("/api/questions") });
  const openQuestions = questions.data?.questions.length ?? 0;
  const data = list.data;
  const summary = data?.summary ?? {};
  const failedAuth = task.data?.status === "failed" ? task.data.error : null;

  return (
    <>
      <div className="head-row">
        <h2>Your matches</h2>
        <button disabled={running || refresh.isPending} onClick={() => refresh.mutate()}>
          {running ? "Finding matches…" : "Find new matches"}
        </button>
      </div>

      {running && (
        <p className="muted">
          Scoring jobs against your profile. This takes a few minutes; results appear here as they're ready.
        </p>
      )}
      {failedAuth && <div className="card"><AiErrorNotice error={failedAuth} /></div>}
      <ErrorText error={refresh.error} />
      {!running && summary.pending ? (
        <p className="hint">
          {summary.pending} more promising jobs are waiting — we score a limited number each day to stay within your Claude usage. <Link to="/settings">Change your daily limit</Link>
        </p>
      ) : null}

      {openQuestions > 0 && (
        <div className="notice info">
          <strong>{openQuestions} question{openQuestions === 1 ? "" : "s"} could sharpen your matches.</strong> <Link to="/questions">Answer them</Link>
        </div>
      )}

      <ul className="tabs">
        {TABS.map(([key, label]) => (
          <li key={key}>
            <button type="button" className={status === key ? "tab on" : "tab"} onClick={() => setStatus(key)}>
              {label} <span className="count">{data?.counts?.[key] ?? 0}</span>
            </button>
          </li>
        ))}
      </ul>

      <div className="filters">
        <input type="search" placeholder="Search title or company" value={search} onChange={(e) => setSearch(e.target.value)} />
        <select value={minScore} onChange={(e) => setMinScore(Number(e.target.value))} aria-label="Minimum score">
          <option value={0}>Any score</option>
          <option value={50}>Score 50+</option>
          <option value={65}>Score 65+ (good)</option>
          <option value={80}>Score 80+ (strong)</option>
        </select>
        <select value={workplace} onChange={(e) => setWorkplace(e.target.value)} aria-label="Workplace">
          <option value="">Any workplace</option>
          <option value="remote">Remote</option>
          <option value="hybrid">Hybrid</option>
          <option value="onsite">On-site</option>
        </select>
        <select value={sort} onChange={(e) => setSort(e.target.value as "score" | "recent")} aria-label="Sort">
          <option value="score">Best match first</option>
          <option value="recent">Newest first</option>
        </select>
        <label className="check">
          <input type="checkbox" checked={early} onChange={(e) => setEarlyChoice(e.target.checked)} /> New grad &amp; internships only
        </label>
      </div>

      {list.isPending && <p className="muted">Loading…</p>}
      <ErrorText error={list.error} />
      {data && data.items.length === 0 && !running && (
        <div className="card">
          <h3 style={{ marginTop: 0 }}>{status === "new" ? "Nothing to review yet" : "Nothing here"}</h3>
          <p className="muted">
            {data.total === 0 && !q.trim() && !workplace && Object.values(data.counts).every((n) => n === 0)
              ? "Click “Find new matches” and we'll look through thousands of openings for ones that fit you."
              : "No matches for these filters. Try lowering the minimum score."}
          </p>
        </div>
      )}

      {data?.items.map((m) => (
        <article key={m.id} className={`match${m.job.is_active ? "" : " inactive"}`}>
          <ScoreBadge score={m.score} verdict={m.verdict} />
          <div className="match-body">
            <h3><Link to={`/matches/${m.id}`}>{m.job.title}</Link></h3>
            <div className="company">{m.job.company}</div>
            <JobChips job={m.job} />
            <HireBadge verdict={m.hire_verdict} />
            {m.confidence < 0.6 && <span className="chip warn">Low confidence — we'd like to ask you a few things</span>}
            {m.stale && <span className="chip">Scored before your last profile update</span>}
            {m.has_cover_letter && <span className="chip good">Cover letter drafted</span>}
            <ul className="reasons">{m.reasons.slice(0, 3).map((r) => <li key={r}>{r}</li>)}</ul>
            {m.gaps.length > 0 && <p className="gaps"><strong>Gaps:</strong> {m.gaps.slice(0, 2).join(" · ")}</p>}
            <div className="actions-inline">
              <StatusActions match={m} />
              <Link to={`/matches/${m.id}`} className="plain">Details</Link>
              {safeHttpUrl(m.job.url) && <a href={safeHttpUrl(m.job.url)!} target="_blank" rel="noreferrer noopener" className="plain">Apply ↗</a>}
            </div>
          </div>
        </article>
      ))}
      {data && data.total > data.items.length && (
        <p className="hint">Showing the top {data.items.length} of {data.total}. Narrow the filters to see others.</p>
      )}
    </>
  );
}
