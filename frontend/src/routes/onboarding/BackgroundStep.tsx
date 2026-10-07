import { useEffect, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { emptyBackground, type Background, type Education, type Experience, type Profile, type Project } from "../../lib/types";
import { ErrorText, Field, ListInput } from "../../components/fields";
import { useTask } from "../../lib/useTask";
import { AiErrorNotice } from "../../components/AiErrorNotice";

const KINDS = ["internship", "co-op", "full-time", "part-time", "research", "other"];

function Section<T>({ title, items, onChange, blank, render, addLabel }: {
  title: string; items: T[]; onChange: (v: T[]) => void; blank: T; addLabel: string;
  render: (item: T, update: (patch: Partial<T>) => void) => React.ReactNode;
}) {
  return (
    <>
      <h3>{title}</h3>
      {items.map((item, i) => (
        <div key={i} className="subcard">
          {render(item, (patch) => onChange(items.map((x, j) => (j === i ? { ...x, ...patch } : x))))}
          <button type="button" className="link" onClick={() => onChange(items.filter((_, j) => j !== i))}>Remove</button>
        </div>
      ))}
      <button type="button" className="secondary" onClick={() => onChange([...items, blank])}>+ {addLabel}</button>
    </>
  );
}

export default function BackgroundStep({ initial, onDone, onBack, submitLabel = "Looks good" }: { initial: Partial<Background>; onDone: () => void; onBack?: () => void; submitLabel?: string }) {
  const hasInitial = Object.keys(initial).length > 0;
  const [bg, setBg] = useState<Background>({ ...emptyBackground, ...initial });
  const [taskId, setTaskId] = useState<number | null>(null);
  const [extracted, setExtracted] = useState(hasInitial);
  const started = useRef(false);

  const start = useMutation({ mutationFn: () => api<{ task_id: number }>("/api/onboarding/extract", { method: "POST" }), onSuccess: (r) => setTaskId(r.task_id) });
  const task = useTask<Background>(taskId);

  useEffect(() => {
    if (!hasInitial && !started.current) { started.current = true; start.mutate(); }
  }, [hasInitial, start]);

  useEffect(() => {
    if (task.data?.status === "done" && task.data.result && !extracted) {
      setBg({ ...emptyBackground, ...task.data.result });
      setExtracted(true);
    }
  }, [task.data, extracted]);

  const save = useMutation({ mutationFn: () => api<Profile>("/api/profile/background", { method: "PUT", json: bg }), onSuccess: onDone });
  const reanalyze = () => { setExtracted(false); setTaskId(null); start.mutate(); };
  const set = <K extends keyof Background>(k: K, v: Background[K]) => setBg((p) => ({ ...p, [k]: v }));

  if (!extracted) {
    const failed = task.data?.status === "failed";
    return (
      <div className="card">
        <h2>Reading your background…</h2>
        {failed ? (
          <>
            <AiErrorNotice error={task.data?.error ?? null} />
            <div className="actions">{onBack && <button className="secondary" onClick={onBack}>Back</button>}<button onClick={reanalyze}>Try again</button></div>
          </>
        ) : (
          <p className="muted">This usually takes under a minute.</p>
        )}
        <ErrorText error={start.error} />
      </div>
    );
  }

  return (
    <form className="card" onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
      <h2>Is this right?</h2>
      <p className="muted">We pulled this from your documents. Fix anything wrong or missing — it drives your matches and cover letters.</p>
      <Field label="Your name" hint="Used to sign your cover letters"><input value={bg.name} onChange={(e) => set("name", e.target.value)} /></Field>
      <Field label="Summary"><textarea rows={3} value={bg.summary} onChange={(e) => set("summary", e.target.value)} /></Field>

      <Section<Education> title="Education" items={bg.education} onChange={(v) => set("education", v)} addLabel="Add education"
        blank={{ school: "", degree: "", field: "", start: "", end: "", gpa: "" }}
        render={(e, u) => (
          <>
            <div className="row">
              <Field label="School"><input value={e.school} onChange={(x) => u({ school: x.target.value })} /></Field>
              <Field label="Degree"><input value={e.degree} onChange={(x) => u({ degree: x.target.value })} /></Field>
              <Field label="Field"><input value={e.field} onChange={(x) => u({ field: x.target.value })} /></Field>
            </div>
            <div className="row">
              <Field label="Start"><input value={e.start} onChange={(x) => u({ start: x.target.value })} /></Field>
              <Field label="End / expected"><input value={e.end} onChange={(x) => u({ end: x.target.value })} /></Field>
              <Field label="GPA"><input value={e.gpa} onChange={(x) => u({ gpa: x.target.value })} /></Field>
            </div>
          </>
        )} />

      <Section<Experience> title="Experience (jobs, internships, co-ops)" items={bg.experience} onChange={(v) => set("experience", v)} addLabel="Add experience"
        blank={{ kind: "other", company: "", title: "", start: "", end: "", location: "", summary: "", bullets: [], technologies: [] }}
        render={(e, u) => (
          <>
            <div className="row">
              <Field label="Company"><input value={e.company} onChange={(x) => u({ company: x.target.value })} /></Field>
              <Field label="Title"><input value={e.title} onChange={(x) => u({ title: x.target.value })} /></Field>
              <Field label="Type"><select value={e.kind} onChange={(x) => u({ kind: x.target.value })}>{KINDS.map((k) => <option key={k}>{k}</option>)}</select></Field>
            </div>
            <div className="row">
              <Field label="Start"><input value={e.start} onChange={(x) => u({ start: x.target.value })} /></Field>
              <Field label="End"><input value={e.end} onChange={(x) => u({ end: x.target.value })} /></Field>
            </div>
            <Field label="What you did (one per line)">
              <textarea rows={4} defaultValue={e.bullets.join("\n")} onChange={(x) => u({ bullets: x.target.value.split("\n").map((s) => s.trim()).filter(Boolean) })} />
            </Field>
            <Field label="Technologies"><ListInput value={e.technologies} onChange={(v) => u({ technologies: v })} /></Field>
          </>
        )} />

      <Section<Project> title="Projects" items={bg.projects} onChange={(v) => set("projects", v)} addLabel="Add project"
        blank={{ name: "", description: "", technologies: [], url: "" }}
        render={(p, u) => (
          <>
            <div className="row">
              <Field label="Name"><input value={p.name} onChange={(x) => u({ name: x.target.value })} /></Field>
              <Field label="URL"><input value={p.url} onChange={(x) => u({ url: x.target.value })} /></Field>
            </div>
            <Field label="Description"><textarea rows={2} value={p.description} onChange={(x) => u({ description: x.target.value })} /></Field>
            <Field label="Technologies"><ListInput value={p.technologies} onChange={(v) => u({ technologies: v })} /></Field>
          </>
        )} />

      <h3>Skills</h3>
      <Field label="Comma separated"><ListInput value={bg.skills.map((s) => s.name)} onChange={(v) => set("skills", v.map((name) => bg.skills.find((s) => s.name === name) ?? { name, level: "" }))} /></Field>
      <Field label="Certifications"><ListInput value={bg.certifications} onChange={(v) => set("certifications", v)} /></Field>

      <ErrorText error={save.error} />
      <div className="actions">
        {onBack && <button type="button" className="secondary" onClick={onBack}>Back</button>}
        <button type="button" className="secondary" onClick={reanalyze}>Re-read my documents</button>
        <button disabled={save.isPending}>{save.isPending ? "Saving…" : submitLabel}</button>
      </div>
    </form>
  );
}
