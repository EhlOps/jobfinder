import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import type { Dossier, DossierItem, Interview as I } from "../lib/types";
import { ErrorText } from "../components/fields";
import { useTask } from "../lib/useTask";
import { AiErrorNotice } from "../components/AiErrorNotice";

const DIMENSIONS: [string, string][] = [
  ["skills", "Skills depth"], ["impact", "Impact evidence"], ["scope", "Scope & seniority"],
  ["logistics", "Logistics"], ["motivation", "Motivation"],
];

function Items({ title, items }: { title: string; items?: DossierItem[] }) {
  if (!items?.length) return null;
  return (
    <>
      <h4>{title}</h4>
      <ul className="reasons">
        {items.map((it) => (
          <li key={it.label} style={{ opacity: it.confidence < 0.5 ? 0.7 : 1 }}>
            <strong>{it.label}</strong> — {it.detail}
            {it.confidence < 0.5 && <span className="muted"> (unconfirmed)</span>}
          </li>
        ))}
      </ul>
    </>
  );
}

export function RecruiterView({ dossier }: { dossier: Dossier }) {
  return (
    <div className="card">
      <h3 style={{ marginTop: 0 }}>How a recruiter reads you</h3>
      {dossier.headline && <p><strong>{dossier.headline}</strong>{dossier.years_experience ? ` · ${dossier.years_experience}` : ""}</p>}
      {dossier.hire_view && <p className="callout">{dossier.hire_view}</p>}
      <Items title="Skills" items={dossier.skills} />
      <Items title="Experience" items={dossier.experience} />
      <Items title="Logistics" items={dossier.logistics} />
      {!!dossier.strengths?.length && (<><h4>Strongest evidence</h4><ul className="reasons">{dossier.strengths.map((s) => <li key={s}>{s}</li>)}</ul></>)}
      {!!dossier.concerns?.length && (<><h4>What would make a recruiter hesitate</h4><ul className="reasons">{dossier.concerns.map((s) => <li key={s}>{s}</li>)}</ul></>)}
    </div>
  );
}

/** "Strengthen my profile": the recruiter-style audit, one question at a time. Also used as the last onboarding step. */
export default function Interview({ onFinish }: { onFinish?: () => void }) {
  const qc = useQueryClient();
  const [taskId, setTaskId] = useState<number | null>(null);
  const [text, setText] = useState("");
  const view = useQuery({ queryKey: ["interview"], queryFn: () => api<I>("/api/profile/interview") });
  const task = useTask<unknown>(taskId);
  const refresh = useMutation({ mutationFn: () => api<{ task_id: number }>("/api/profile/interview/refresh", { method: "POST" }), onSuccess: (r) => setTaskId(r.task_id) });
  const send = useMutation({
    mutationFn: (a: { question: string; answer: string; skip: boolean }) =>
      api("/api/profile/interview/answers", { json: { answers: [a] } }),
    onSuccess: () => { setText(""); qc.invalidateQueries({ queryKey: ["interview"] }); qc.invalidateQueries({ queryKey: ["facts"] }); qc.invalidateQueries({ queryKey: ["profile"] }); },
  });

  // First visit (or the profile changed since the last audit): run one.
  const needsAudit = view.data && (!view.data.audited || (view.data.stale && view.data.questions.length === 0));
  useEffect(() => { if (needsAudit && taskId === null && !refresh.isPending) refresh.mutate(); }, [needsAudit]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (task.data?.status === "done") qc.invalidateQueries({ queryKey: ["interview"] }); }, [task.data?.status, qc]);
  // After answering, the audit is re-run in the background: poll until fresh questions appear.
  useEffect(() => {
    if (!view.data?.stale) return;
    const t = setInterval(() => qc.invalidateQueries({ queryKey: ["interview"] }), 4000);
    return () => clearInterval(t);
  }, [view.data?.stale, qc]);

  if (!view.data) return <p className="muted">Loading…</p>;
  const d = view.data;
  if (task.data?.status === "failed")
    return <div className="card"><AiErrorNotice error={task.data.error} /><div className="actions">{onFinish && <button onClick={onFinish}>Skip for now</button>}</div></div>;
  if (!d.audited)
    return <div className="card"><h2>Reading your profile like a recruiter…</h2><p className="muted">This takes a minute. We look for what a hiring manager would still want to know.</p><ErrorText error={refresh.error} /></div>;

  const q = d.questions[0];
  return (
    <>
      {!onFinish && <h2>Strengthen my profile</h2>}
      <div className="card">
        <div className="readiness">
          <strong style={{ fontSize: "2rem" }}>{d.readiness}</strong>
          <span className="muted"> / 100 recruiter-ready</span>
        </div>
        <div className="dims">
          {DIMENSIONS.map(([k, label]) => (
            <div key={k} className="dim">
              <span>{label}</span>
              <div className="bar"><div style={{ width: `${d.dimensions[k] ?? 0}%` }} /></div>
            </div>
          ))}
        </div>
      </div>

      {q ? (
        <form className="card" onSubmit={(e) => { e.preventDefault(); if (text.trim()) send.mutate({ question: q.question, answer: text.trim(), skip: false }); }}>
          <p className="hint" style={{ margin: 0 }}>{d.questions.length} question{d.questions.length === 1 ? "" : "s"} left · {q.dimension}</p>
          <label className="field">
            <span className="field-label">{q.question}</span>
            {q.why && <span className="hint">{q.why}</span>}
            <textarea rows={4} value={text} onChange={(e) => setText(e.target.value)} placeholder="Specifics help most: numbers, tools, your part in it" />
          </label>
          <ErrorText error={send.error} />
          <div className="actions">
            <button type="button" className="secondary" disabled={send.isPending} onClick={() => send.mutate({ question: q.question, answer: "", skip: true })}>Skip</button>
            <button disabled={!text.trim() || send.isPending}>{send.isPending ? "Saving…" : "Save & next"}</button>
          </div>
        </form>
      ) : (
        <div className="card">
          <p>{d.stale ? "Updating your profile with your answers…" : "No open questions right now. Come back after new matches arrive — we'll have more to ask."}</p>
          {!d.stale && <button className="secondary" disabled={refresh.isPending} onClick={() => refresh.mutate()}>Re-check my profile</button>}
        </div>
      )}
      {onFinish ? (
        <div className="actions"><button onClick={onFinish}>{d.questions.length ? "Finish for now" : "Finish"}</button></div>
      ) : (
        <RecruiterView dossier={d.dossier} />
      )}
    </>
  );
}
