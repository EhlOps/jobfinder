import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation } from "@tanstack/react-query";
import type { AnswerResult, QuestionItem } from "../lib/types";
import { ErrorText } from "./fields";

/** Answer-or-skip form shared by the logged-in inbox and the no-login email link page. */
export function QuestionForm({
  questions,
  submit,
  onDone,
  linkJobs = true,
}: {
  questions: QuestionItem[];
  submit: (answers: { id: number; answer: string; skip: boolean }[]) => Promise<AnswerResult>;
  onDone?: (r: AnswerResult) => void;
  linkJobs?: boolean;
}) {
  const [text, setText] = useState<Record<number, string>>({});
  const [skipped, setSkipped] = useState<Record<number, boolean>>({});
  const [result, setResult] = useState<AnswerResult | null>(null);

  const send = useMutation({
    mutationFn: () =>
      submit(
        questions
          .map((q) => ({ id: q.id, answer: (text[q.id] ?? "").trim(), skip: !!skipped[q.id] }))
          .filter((a) => a.answer || a.skip),
      ),
    onSuccess: (r) => { setResult(r); onDone?.(r); },
  });

  if (result) {
    return (
      <div className="card">
        <h3 style={{ marginTop: 0 }}>Thanks!</h3>
        <p>
          {result.answered > 0
            ? `Saved ${result.answered} answer${result.answered === 1 ? "" : "s"}. We'll re-score the affected jobs in the background.`
            : "Nothing to save this time."}
          {result.remaining > 0 ? ` ${result.remaining} question${result.remaining === 1 ? " is" : "s are"} still open.` : ""}
        </p>
      </div>
    );
  }

  const filled = questions.some((q) => (text[q.id] ?? "").trim() || skipped[q.id]);
  return (
    <form onSubmit={(e) => { e.preventDefault(); send.mutate(); }}>
      {questions.map((q) => (
        <div key={q.id} className="card">
          <label className="field" style={{ margin: 0 }}>
            <span className="field-label">{q.question}</span>
            {q.why && <span className="hint">{q.why}</span>}
            <textarea
              rows={2} value={text[q.id] ?? ""} disabled={!!skipped[q.id]} placeholder="A sentence or two is plenty"
              onChange={(e) => setText((t) => ({ ...t, [q.id]: e.target.value }))}
            />
          </label>
          {q.jobs.length > 0 && (
            <p className="hint" style={{ margin: "8px 0 0" }}>
              Came up for:{" "}
              {q.jobs.map((j, i) => (
                <span key={j.match_id}>
                  {i > 0 && ", "}
                  {linkJobs ? <Link to={`/matches/${j.match_id}`}>{j.company} — {j.title}</Link> : `${j.company} — ${j.title}`}
                </span>
              ))}
            </p>
          )}
          <label className="check" style={{ marginTop: 6 }}>
            <input
              type="checkbox" checked={!!skipped[q.id]}
              onChange={(e) => setSkipped((s) => ({ ...s, [q.id]: e.target.checked }))}
            />
            Skip — not applicable / I'd rather not say
          </label>
        </div>
      ))}
      <ErrorText error={send.error} />
      <button disabled={!filled || send.isPending}>{send.isPending ? "Saving…" : "Send my answers"}</button>
    </form>
  );
}
