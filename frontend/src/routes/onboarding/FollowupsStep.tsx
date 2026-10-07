import { useEffect, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "../../lib/api";
import type { FollowupQuestion } from "../../lib/types";
import { ErrorText } from "../../components/fields";
import { useTask } from "../../lib/useTask";
import { AiErrorNotice } from "../../components/AiErrorNotice";

export default function FollowupsStep({ onDone, onBack }: { onDone: () => void; onBack: () => void }) {
  const [taskId, setTaskId] = useState<number | null>(null);
  const [answers, setAnswers] = useState<Record<number, string>>({});
  const started = useRef(false);
  const start = useMutation({ mutationFn: () => api<{ task_id: number }>("/api/onboarding/followups", { method: "POST" }), onSuccess: (r) => setTaskId(r.task_id) });
  const task = useTask<{ questions: FollowupQuestion[] }>(taskId);

  useEffect(() => {
    if (!started.current) { started.current = true; start.mutate(); }
  }, [start]);

  const questions = task.data?.status === "done" ? task.data.result?.questions ?? [] : null;
  const save = useMutation({
    mutationFn: async () => {
      for (const [i, q] of (questions ?? []).entries()) {
        const answer = answers[i]?.trim();
        if (answer) await api("/api/profile/facts", { json: { question: q.question, answer, source: "followup" } });
      }
    },
    onSuccess: onDone,
  });

  if (task.data?.status === "failed")
    return (
      <div className="card">
        <AiErrorNotice error={task.data.error} />
        <div className="actions"><button className="secondary" onClick={onBack}>Back</button><button onClick={onDone}>Skip for now</button></div>
      </div>
    );
  if (!questions)
    return (
      <div className="card">
        <h2>Thinking of a few questions…</h2>
        <p className="muted">We look for gaps worth filling so your matches and cover letters are specific to you.</p>
        <ErrorText error={start.error} />
      </div>
    );

  return (
    <form className="card" onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
      <h2>A few quick questions</h2>
      <p className="muted">Answer what you can — skip anything that doesn't apply. Short answers are fine.</p>
      {questions.map((q, i) => (
        <label key={i} className="field">
          <span className="field-label">{q.question}</span>
          {q.why && <span className="hint">{q.why}</span>}
          <textarea rows={3} value={answers[i] ?? ""} onChange={(e) => setAnswers((a) => ({ ...a, [i]: e.target.value }))} />
        </label>
      ))}
      <ErrorText error={save.error} />
      <div className="actions">
        <button type="button" className="secondary" onClick={onBack}>Back</button>
        <button disabled={save.isPending}>{save.isPending ? "Saving…" : "Finish"}</button>
      </div>
    </form>
  );
}
