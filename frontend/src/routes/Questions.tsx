import { Link } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import type { AnswerResult, QuestionItem } from "../lib/types";
import { QuestionForm } from "../components/QuestionForm";
import { ErrorText } from "../components/fields";

export default function Questions() {
  const qc = useQueryClient();
  const list = useQuery({ queryKey: ["questions"], queryFn: () => api<{ questions: QuestionItem[] }>("/api/questions") });
  const questions = list.data?.questions ?? [];

  return (
    <>
      <h2>Questions for you</h2>
      <p className="muted">
        Some jobs look promising but we weren't sure about a few things. Your answers go into your profile and sharpen every
        future match.
      </p>
      {list.isLoading && <p className="muted">Loading…</p>}
      <ErrorText error={list.error} />
      {list.data && questions.length === 0 && (
        <div className="card">
          <h3 style={{ marginTop: 0 }}>You're all caught up</h3>
          <p className="muted">When a match needs more detail from you, the question will show up here (and in your daily email). <Link to="/">Back to matches</Link></p>
        </div>
      )}
      {questions.length > 0 && (
        <QuestionForm
          key={questions.map((q) => q.id).join(",")}
          questions={questions}
          submit={(answers) => api<AnswerResult>("/api/questions/answers", { json: { answers } })}
          onDone={() => { qc.invalidateQueries({ queryKey: ["questions"] }); qc.invalidateQueries({ queryKey: ["profile"] }); qc.invalidateQueries({ queryKey: ["facts"] }); }}
        />
      )}
    </>
  );
}
