import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "../lib/api";
import { useTask } from "../lib/useTask";
import type { CoverLetterState, Tone } from "../lib/types";
import { AiErrorNotice } from "./AiErrorNotice";
import { ErrorText } from "./fields";

const TONES: [Tone, string][] = [
  ["professional", "Professional"],
  ["warm", "Warm"],
  ["concise", "Concise (short)"],
  ["enthusiastic", "Enthusiastic"],
];

export function CoverLetterPanel({ matchId }: { matchId: number }) {
  const qc = useQueryClient();
  const base = `/api/matches/${matchId}/cover-letter`;
  const key = ["letter", matchId];
  const state = useQuery({ queryKey: key, queryFn: () => api<CoverLetterState>(base) });
  const letter = state.data?.letter ?? null;

  const [tone, setTone] = useState<Tone>("professional");
  const [notes, setNotes] = useState("");
  const [draftText, setDraftText] = useState<string | null>(null); // null = not editing: show the saved text
  const [copied, setCopied] = useState(false);
  const [localTask, setLocalTask] = useState<number | null>(null);
  const [failedError, setFailedError] = useState<string | null>(null);
  const handled = useRef<number | null>(null);

  // The draft being watched: the one we just started, else one the server says is still running
  // (e.g. after a page reload). Derived rather than copied into state so the two can't fight.
  const activeTask = localTask ?? state.data?.pending_task_id ?? null;
  const task = useTask<{ words: number }>(activeTask);
  const status = task.data?.status;
  const writing = activeTask !== null && status !== "done" && status !== "failed";

  // React to each finished task exactly once.
  useEffect(() => {
    if (activeTask === null || handled.current === activeTask) return;
    if (status === "done") {
      handled.current = activeTask;
      setLocalTask(null);
      setDraftText(null); // a fresh draft replaces whatever was being edited
      qc.invalidateQueries({ queryKey: key });
      qc.invalidateQueries({ queryKey: ["matches"] });
    } else if (status === "failed") {
      handled.current = activeTask;
      setLocalTask(null);
      setFailedError(task.data?.error ?? "Something went wrong.");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTask, status]);

  const text = draftText ?? letter?.content ?? "";
  const dirty = letter !== null && draftText !== null && draftText !== letter.content;

  const draft = useMutation({
    mutationFn: (force: boolean) => api<{ task_id: number }>(base, { json: { tone, notes, force } }),
    onSuccess: (r) => { setFailedError(null); setLocalTask(r.task_id); },
  });
  const startDraft = () => {
    if (letter?.edited && !window.confirm("You've edited this letter. Redrafting replaces your changes. Continue?")) return;
    draft.mutate(!!letter?.edited);
  };
  const save = useMutation({
    mutationFn: () => api(base, { method: "PUT", json: { content: text } }),
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: key });
      setDraftText(null);
    },
  });
  const remove = useMutation({
    mutationFn: () => api<void>(base, { method: "DELETE" }),
    onSuccess: () => { setDraftText(null); qc.invalidateQueries({ queryKey: key }); qc.invalidateQueries({ queryKey: ["matches"] }); },
  });
  const copy = async () => {
    await navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  const options = (
    <div className="letter-options">
      <select value={tone} onChange={(e) => setTone(e.target.value as Tone)} aria-label="Tone" disabled={writing}>
        {TONES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
      </select>
      <input
        value={notes} maxLength={500} disabled={writing} onChange={(e) => setNotes(e.target.value)}
        placeholder="Anything to emphasize? (optional, e.g. “mention my open-source work”)"
      />
    </div>
  );

  return (
    <article className="card">
      <h3 style={{ marginTop: 0 }}>Cover letter</h3>

      {state.isLoading && <p className="muted">Loading…</p>}

      {!letter && !writing && state.data && (
        <>
          <p className="muted">
            We'll draft a letter from your real background and answers — nothing invented — tailored to this role. You can edit it afterwards.
          </p>
          {options}
          <button onClick={startDraft} disabled={draft.isPending}>Draft cover letter</button>
        </>
      )}

      {writing && (
        <p className="muted">Writing your letter… this usually takes about a minute. You can leave this page; it'll be here when you come back.</p>
      )}
      {failedError && !writing && <AiErrorNotice error={failedError} />}
      <ErrorText error={draft.error instanceof ApiError && draft.error.status === 409 ? null : draft.error} />

      {letter && (
        <>
          <div className="letter-meta">
            <span className="chip">{letter.words} words</span>
            <span className="chip">{TONES.find(([v]) => v === letter.tone)?.[1] ?? letter.tone}</span>
            {letter.edited && <span className="chip good">Edited by you</span>}
            {letter.stale && <span className="chip warn">Written before your latest profile update</span>}
          </div>
          <textarea
            className="letter-editor" value={text} onChange={(e) => setDraftText(e.target.value)} disabled={writing}
            aria-label="Cover letter text" maxLength={10000}
          />
          <p className="hint">Always read it through before sending: AI drafts can overstate or miss details, and only you know what's true.</p>
          <div className="actions-inline">
            <button disabled={!dirty || save.isPending || writing} onClick={() => save.mutate()}>
              {save.isPending ? "Saving…" : dirty ? "Save changes" : "Saved"}
            </button>
            <button className="secondary" onClick={copy}>{copied ? "Copied ✓" : "Copy"}</button>
            <a className="button-link" href={`${base}/download`} download>Download .docx</a>
            <button className="secondary" disabled={remove.isPending || writing} onClick={() => window.confirm("Delete this letter?") && remove.mutate()}>Delete</button>
          </div>
          <ErrorText error={save.error || remove.error} />

          <h4 style={{ marginBottom: 4 }}>Redraft</h4>
          {options}
          <button className="secondary" onClick={startDraft} disabled={writing || draft.isPending}>
            {letter.edited ? "Redraft (replaces your edits)" : "Redraft"}
          </button>
        </>
      )}
    </article>
  );
}
