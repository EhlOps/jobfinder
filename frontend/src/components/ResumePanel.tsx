import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "../lib/api";
import { useTask } from "../lib/useTask";
import type { ResumeEducation, ResumeExperience, ResumeProject, ResumeState, TailoredResume } from "../lib/types";
import { AiErrorNotice } from "./AiErrorNotice";
import { ErrorText } from "./fields";

const lines = (s: string) => s.split("\n").map((l) => l.trim()).filter(Boolean);
const commas = (s: string) => s.split(",").map((l) => l.trim()).filter(Boolean);

function Field({ label, value, onChange }: { label: string; value: string; onChange: (v: string) => void }) {
  return <input value={value} aria-label={label} placeholder={label} onChange={(e) => onChange(e.target.value)} />;
}

/** A list edited as text. The raw text stays local while typing so spaces, commas and blank lines aren't eaten. */
function ListField({ label, items, sep, onChange }: { label: string; items: string[]; sep: "\n" | ", "; onChange: (v: string[]) => void }) {
  const [text, setText] = useState(items.join(sep));
  const parse = sep === "\n" ? lines : commas;
  return (
    <textarea
      aria-label={label} placeholder={label} rows={sep === "\n" ? 4 : 2} value={text}
      onChange={(e) => { setText(e.target.value); onChange(parse(e.target.value)); }}
    />
  );
}

function update<T>(arr: T[], i: number, patch: Partial<T>): T[] {
  return arr.map((x, j) => (j === i ? { ...x, ...patch } : x));
}

function ResumeEditor({ value, onChange, disabled }: { value: TailoredResume; onChange: (r: TailoredResume) => void; disabled: boolean }) {
  const set = (patch: Partial<TailoredResume>) => onChange({ ...value, ...patch });
  const c = value.contact;
  const setContact = (patch: Partial<typeof c>) => set({ contact: { ...c, ...patch } });
  const exp = (i: number, p: Partial<ResumeExperience>) => set({ experience: update(value.experience, i, p) });
  const proj = (i: number, p: Partial<ResumeProject>) => set({ projects: update(value.projects, i, p) });
  const edu = (i: number, p: Partial<ResumeEducation>) => set({ education: update(value.education, i, p) });
  return (
    <fieldset disabled={disabled} style={{ border: 0, padding: 0, margin: 0 }}>
      <div className="resume-section">
        <h4 style={{ margin: "0 0 4px" }}>Contact</h4>
        <div className="resume-grid">
          <Field label="Name" value={c.name} onChange={(v) => setContact({ name: v })} />
          <Field label="Email" value={c.email} onChange={(v) => setContact({ email: v })} />
          <Field label="Phone" value={c.phone} onChange={(v) => setContact({ phone: v })} />
          <Field label="Location" value={c.location} onChange={(v) => setContact({ location: v })} />
        </div>
        <ListField label="Links (one per line)" items={c.links} sep={"\n"} onChange={(links) => setContact({ links })} />
      </div>
      <div className="resume-section">
        <h4 style={{ margin: "0 0 4px" }}>Summary</h4>
        <textarea aria-label="Summary" rows={3} value={value.summary} onChange={(e) => set({ summary: e.target.value })} />
      </div>
      <div className="resume-section">
        <h4 style={{ margin: "0 0 4px" }}>Skills</h4>
        <ListField label="Skills (comma separated)" items={value.skills} sep=", " onChange={(skills) => set({ skills })} />
      </div>
      {value.experience.length > 0 && <h4 className="resume-section" style={{ marginBottom: 0 }}>Experience</h4>}
      {value.experience.map((e, i) => (
        <div key={i} className="resume-grid">
          <Field label={`Company ${i + 1}`} value={e.company} onChange={(v) => exp(i, { company: v })} />
          <Field label={`Title ${i + 1}`} value={e.title} onChange={(v) => exp(i, { title: v })} />
          <Field label={`Start ${i + 1}`} value={e.start} onChange={(v) => exp(i, { start: v })} />
          <Field label={`End ${i + 1}`} value={e.end} onChange={(v) => exp(i, { end: v })} />
          <Field label={`Location ${i + 1}`} value={e.location} onChange={(v) => exp(i, { location: v })} />
          <Field label={`Role summary ${i + 1}`} value={e.summary} onChange={(v) => exp(i, { summary: v })} />
          <ListField label={`Bullets ${i + 1} (one per line)`} items={e.bullets} sep={"\n"} onChange={(bullets) => exp(i, { bullets })} />
          <ListField label={`Technologies ${i + 1} (comma separated)`} items={e.technologies} sep=", " onChange={(technologies) => exp(i, { technologies })} />
        </div>
      ))}
      {value.projects.length > 0 && <h4 className="resume-section" style={{ marginBottom: 0 }}>Projects</h4>}
      {value.projects.map((p, i) => (
        <div key={i} className="resume-grid">
          <Field label={`Project name ${i + 1}`} value={p.name} onChange={(v) => proj(i, { name: v })} />
          <Field label={`Project URL ${i + 1}`} value={p.url} onChange={(v) => proj(i, { url: v })} />
          <textarea aria-label={`Project description ${i + 1}`} placeholder="Description" rows={3} value={p.description} onChange={(e) => proj(i, { description: e.target.value })} />
          <ListField label={`Project technologies ${i + 1} (comma separated)`} items={p.technologies} sep=", " onChange={(technologies) => proj(i, { technologies })} />
        </div>
      ))}
      {value.education.length > 0 && <h4 className="resume-section" style={{ marginBottom: 0 }}>Education</h4>}
      {value.education.map((e, i) => (
        <div key={i} className="resume-grid">
          <Field label={`School ${i + 1}`} value={e.school} onChange={(v) => edu(i, { school: v })} />
          <Field label={`Degree ${i + 1}`} value={e.degree} onChange={(v) => edu(i, { degree: v })} />
          <Field label={`Field ${i + 1}`} value={e.field} onChange={(v) => edu(i, { field: v })} />
          <Field label={`Education start ${i + 1}`} value={e.start} onChange={(v) => edu(i, { start: v })} />
          <Field label={`Education end ${i + 1}`} value={e.end} onChange={(v) => edu(i, { end: v })} />
          <Field label={`GPA ${i + 1}`} value={e.gpa} onChange={(v) => edu(i, { gpa: v })} />
        </div>
      ))}
    </fieldset>
  );
}

export function ResumePanel({ matchId }: { matchId: number }) {
  const qc = useQueryClient();
  const base = `/api/matches/${matchId}/resume`;
  const key = ["resume", matchId];
  const state = useQuery({ queryKey: key, queryFn: () => api<ResumeState>(base) });
  const resume = state.data?.resume ?? null;

  const [draft, setDraft] = useState<TailoredResume | null>(null); // null = not editing: show the saved resume
  const [editorKey, setEditorKey] = useState(0); // remounts the editor when its content is replaced from outside
  const [localTask, setLocalTask] = useState<number | null>(null);
  const [failedError, setFailedError] = useState<string | null>(null);
  const handled = useRef<number | null>(null);

  // Same pattern as the cover letter: watch the task we started, else one the server says is running.
  const activeTask = localTask ?? state.data?.pending_task_id ?? null;
  const task = useTask<unknown>(activeTask);
  const status = task.data?.status;
  const working = activeTask !== null && status !== "done" && status !== "failed";

  useEffect(() => {
    if (activeTask === null || handled.current === activeTask) return;
    if (status === "done") {
      handled.current = activeTask;
      // Keep showing "working" until the new resume has loaded, so the Generate button doesn't flash.
      qc.invalidateQueries({ queryKey: key }).then(() => {
        setLocalTask(null);
        setDraft(null);
        setEditorKey((k) => k + 1);
      });
    } else if (status === "failed") {
      handled.current = activeTask;
      setLocalTask(null);
      setFailedError(task.data?.error ?? "Something went wrong.");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTask, status]);

  const content = draft ?? resume?.content ?? null;
  const dirty = draft !== null;

  const generate = useMutation({
    mutationFn: (force: boolean) => api<{ task_id: number }>(base, { json: { force } }),
    onSuccess: (r) => { setFailedError(null); setLocalTask(r.task_id); },
    onError: (e) => { if (e instanceof ApiError && e.status === 409) qc.invalidateQueries({ queryKey: key }); },
  });
  const start = () => {
    if (resume?.edited && !window.confirm("You've edited this resume. Regenerating replaces your changes. Continue?")) return;
    if (dirty && !window.confirm("You have unsaved changes. Regenerating discards them. Continue?")) return;
    generate.mutate(!!resume?.edited);
  };
  const save = useMutation({
    mutationFn: (sent: TailoredResume) => api(base, { method: "PUT", json: { content: sent } }),
    onSuccess: async (_r, sent) => {
      await qc.invalidateQueries({ queryKey: key });
      // Only clear the draft if nothing was typed while saving.
      setDraft((d) => {
        if (d !== sent) return d;
        setEditorKey((k) => k + 1);
        return null;
      });
    },
  });
  const remove = useMutation({
    mutationFn: () => api<void>(base, { method: "DELETE" }),
    onSuccess: () => { setDraft(null); setEditorKey((k) => k + 1); qc.invalidateQueries({ queryKey: key }); },
  });

  const cov = resume?.coverage;

  return (
    <article className="card">
      <h3 style={{ marginTop: 0 }}>Tailored resume</h3>

      {state.isLoading && <p className="muted">Loading…</p>}

      {!resume && !working && state.data && (
        <>
          <p className="muted">
            We'll rewrite your resume for this role using only your real background, nothing invented, and show which of the posting's keywords it covers.
          </p>
          <button onClick={start} disabled={generate.isPending}>Generate resume</button>
        </>
      )}

      {working && (
        <p className="muted">Tailoring your resume… this usually takes about a minute. You can leave this page; it'll be here when you come back.</p>
      )}
      {failedError && !working && <AiErrorNotice error={failedError} />}
      <ErrorText error={generate.error instanceof ApiError && generate.error.status === 409 ? null : generate.error} />

      {resume && cov && content && (
        <>
          <div className="letter-meta">
            <span className="chip">{cov.percent}% keyword coverage</span>
            {resume.edited && <span className="chip good">Edited by you</span>}
          </div>
          {cov.missing.length > 0 ? (
            <p>
              <strong>Missing keywords:</strong>{" "}
              {cov.missing.map((m, i) => <span key={`${m}-${i}`} className="chip warn" style={{ marginRight: 4 }}>{m}</span>)}
              <span className="hint"> Add them only where they're true for you.</span>
            </p>
          ) : (
            <p className="muted">Every keyword from the posting is covered.</p>
          )}
          {cov.loose.length > 0 && (
            <p className="hint">
              Only a variant spelling appears for: {cov.loose.join(", ")}. Some applicant tracking systems ({cov.ats}) match the exact wording.
            </p>
          )}
          {cov.notes.length > 0 && <ul className="reasons">{cov.notes.map((n, i) => <li key={i}>{n}</li>)}</ul>}

          <ResumeEditor key={editorKey} value={content} onChange={setDraft} disabled={working} />

          <p className="hint">Always read it through before sending: AI drafts can overstate or miss details, and only you know what's true.</p>
          <div className="actions-inline">
            <button disabled={!dirty || save.isPending || working} onClick={() => content && save.mutate(content)}>
              {save.isPending ? "Saving…" : dirty ? "Save changes" : "Saved"}
            </button>
            {dirty && <button className="secondary" onClick={() => { setDraft(null); setEditorKey((k) => k + 1); }}>Discard changes</button>}
            <a className="button-link" href={`${base}/download?format=docx`} download>Download .docx</a>
            <a className="button-link" href={`${base}/download?format=pdf`} download>Download .pdf</a>
            <button className="secondary" disabled={working || generate.isPending} onClick={start}>
              {resume.edited ? "Regenerate (replaces your edits)" : "Regenerate"}
            </button>
            <button className="secondary" disabled={remove.isPending || working} onClick={() => window.confirm("Delete this resume?") && remove.mutate()}>Delete</button>
          </div>
          {dirty && <p className="hint">Downloads use the saved version, so save your changes first.</p>}
          <ErrorText error={save.error || remove.error} />
        </>
      )}
    </article>
  );
}
