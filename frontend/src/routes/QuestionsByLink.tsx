import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ApiError, api } from "../lib/api";
import type { AnswerResult, QuestionItem } from "../lib/types";
import { QuestionForm } from "../components/QuestionForm";

/** Opened from the daily email: the signed link is the authorisation, so no login is needed. */
export default function QuestionsByLink() {
  const { token = "" } = useParams();
  const list = useQuery({
    queryKey: ["questions-link", token],
    retry: false,
    queryFn: () => api<{ questions: QuestionItem[] }>(`/api/q/${token}`),
  });
  const questions = list.data?.questions ?? [];

  return (
    <main className="auth" style={{ maxWidth: 640 }}>
      <h1>JobFinder</h1>
      {list.isLoading && <p className="muted">Loading…</p>}
      {list.error && (
        <div className="card">
          <p className="error">{list.error instanceof ApiError ? list.error.message : "Something went wrong."}</p>
          <p><Link to="/login?next=/questions">Log in</Link> to answer from the Questions page.</p>
        </div>
      )}
      {list.data && questions.length === 0 && (
        <div className="card">
          <h3 style={{ marginTop: 0 }}>All done</h3>
          <p className="muted">There's nothing left to answer. <Link to="/">Open your matches</Link></p>
        </div>
      )}
      {questions.length > 0 && (
        <>
          <h2>A few quick questions</h2>
          <p className="muted">One-line answers are fine. They help us find and rank jobs that really fit you.</p>
          <QuestionForm
            questions={questions}
            linkJobs={false}
            submit={(answers) => api<AnswerResult>(`/api/q/${token}/answers`, { json: { answers } })}
          />
        </>
      )}
    </main>
  );
}
