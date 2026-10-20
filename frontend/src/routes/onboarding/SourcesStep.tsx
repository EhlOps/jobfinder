import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { friendlyTaskError, useTask } from "../../lib/useTask";
import type { DocumentAccepted, DocumentOut, LinkAccepted, LinkOut, SourceStatus } from "../../lib/types";
import { ErrorText } from "../../components/fields";
import { AiBanner } from "../../components/AiBanner";

const LINK_KINDS: [string, string][] = [["github", "GitHub"], ["portfolio", "Portfolio / website"], ["linkedin", "LinkedIn"], ["x", "X / Twitter"], ["other", "Other"]];

type ListKey = "documents" | "links";
interface Pending { taskId: number; list: ListKey }

// Older servers (and sync mode) may omit status; anything without one is final.
const statusOf = (item: { status?: SourceStatus }): SourceStatus => item.status ?? "ready";
const pollWhileProcessing = (watching: boolean, items: { status?: SourceStatus }[] | undefined) =>
  !watching && items?.some((i) => statusOf(i) === "processing") ? 1500 : false;

/** Watches one parse/fetch task and reports back exactly once when it finishes. Renders nothing. */
function TaskWatcher({ taskId, onFinish }: { taskId: number; onFinish: (error: string | null) => void }) {
  const task = useTask(taskId);
  const status = task.data?.status;
  const handled = useRef(false);
  useEffect(() => {
    // A task we can't read (pruned, 404) is handed back to the list's own polling.
    if (handled.current || (status !== "done" && status !== "failed" && !task.isError)) return;
    handled.current = true;
    onFinish(status === "failed" ? friendlyTaskError(task.data?.error ?? null) : null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, task.isError]);
  return null;
}

export default function SourcesStep({ onDone, onBack }: { onDone?: () => void; onBack?: () => void }) {
  const qc = useQueryClient();
  // Tasks started from this page. Items still processing with no task in hand (e.g. after a reload)
  // are caught by polling the list itself while anything in it is processing.
  const [pending, setPending] = useState<Pending[]>([]);
  const [docError, setDocError] = useState<string | null>(null);
  const [linkError, setLinkError] = useState<string | null>(null);
  const watching = (list: ListKey) => pending.some((p) => p.list === list);
  const docs = useQuery({
    queryKey: ["documents"],
    queryFn: () => api<DocumentOut[]>("/api/documents"),
    refetchInterval: (q) => pollWhileProcessing(watching("documents"), q.state.data),
  });
  const links = useQuery({
    queryKey: ["links"],
    queryFn: () => api<LinkOut[]>("/api/links"),
    refetchInterval: (q) => pollWhileProcessing(watching("links"), q.state.data),
  });
  const fileRef = useRef<HTMLInputElement>(null);
  const [kind, setKind] = useState("github");
  const [url, setUrl] = useState("");

  // A null task id means the item is already final (sync mode or a duplicate): just refresh the list.
  // The ledger can hand back the same task for a repeat submission, so watch each id once.
  const track = (list: ListKey, taskId: number | null | undefined) => {
    if (taskId != null) setPending((ps) => (ps.some((p) => p.taskId === taskId) ? ps : [...ps, { taskId, list }]));
    qc.invalidateQueries({ queryKey: [list] });
  };
  const finish = (p: Pending, error: string | null) => {
    setPending((ps) => ps.filter((x) => x.taskId !== p.taskId));
    if (error) (p.list === "documents" ? setDocError : setLinkError)(error);
    qc.invalidateQueries({ queryKey: [p.list] });
  };

  const upload = useMutation({
    mutationFn: (file: File) => { const f = new FormData(); f.append("file", file); f.append("kind", "resume"); return api<DocumentAccepted>("/api/documents", { form: f }); },
    onMutate: () => setDocError(null),
    onSuccess: (r) => track("documents", r.task_id),
  });
  const rmDoc = useMutation({ mutationFn: (id: string) => api<void>(`/api/documents/${id}`, { method: "DELETE" }), onSuccess: () => { setDocError(null); qc.invalidateQueries({ queryKey: ["documents"] }); } });
  const addLink = useMutation({
    mutationFn: () => api<LinkAccepted>("/api/links", { json: { kind, url } }),
    onMutate: () => setLinkError(null),
    onSuccess: (r) => { setUrl(""); track("links", r.task_id); },
  });
  const rmLink = useMutation({ mutationFn: (id: string) => api<void>(`/api/links/${id}`, { method: "DELETE" }), onSuccess: () => { setLinkError(null); qc.invalidateQueries({ queryKey: ["links"] }); } });

  // Only finished, non-empty content counts: a document still processing has nothing to analyze yet.
  const readable = (docs.data?.some((d) => statusOf(d) === "ready" && d.chars > 0) ?? false)
    || (links.data?.some((l) => statusOf(l) === "ready" && l.fetched) ?? false);

  return (
    <div className="card">
      {pending.map((p) => <TaskWatcher key={p.taskId} taskId={p.taskId} onFinish={(e) => finish(p, e)} />)}
      <AiBanner />
      <h2>Your resume and links</h2>
      <p className="muted">We read these to build your profile. Nothing is shared outside the app.</p>

      <h3>Resume / documents</h3>
      <p className="hint">PDF, DOCX, TXT or MD, up to 10MB. Tip: LinkedIn blocks scraping, so upload your LinkedIn "Save to PDF" export instead.</p>
      <input ref={fileRef} type="file" accept=".pdf,.docx,.txt,.md" onChange={(e) => { const f = e.target.files?.[0]; if (f) upload.mutate(f); if (fileRef.current) fileRef.current.value = ""; }} />
      {upload.isPending && <p className="muted">Uploading…</p>}
      <ErrorText error={upload.error} />
      {docError && <p className="error">Couldn't read that file: {docError}</p>}
      <ul className="items">
        {docs.data?.map((d) => (
          <li key={d.id}>
            <span>{d.filename}{" "}
              {d.status === "processing" ? <span className="muted">Processing…</span>
                : d.status === "error" ? <span className="error">Couldn't read this file</span>
                : <span className="muted">({d.chars.toLocaleString()} characters read)</span>}
            </span>
            <button type="button" className="link" onClick={() => rmDoc.mutate(d.id)}>Remove</button>
          </li>
        ))}
      </ul>

      <h3>Links</h3>
      <form className="row" onSubmit={(e) => { e.preventDefault(); addLink.mutate(); }}>
        <select value={kind} onChange={(e) => setKind(e.target.value)}>{LINK_KINDS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select>
        <input type="url" required placeholder="https://…" value={url} onChange={(e) => setUrl(e.target.value)} style={{ flex: 3 }} />
        <button disabled={addLink.isPending}>{addLink.isPending ? "Adding…" : "Add"}</button>
      </form>
      <ErrorText error={addLink.error} />
      {linkError && <p className="error">Couldn't read that link: {linkError}</p>}
      <ul className="items">
        {links.data?.map((l) => (
          <li key={l.id}>
            <span>{l.kind}: {l.url}{" "}
              {l.status === "processing" ? <span className="muted">Processing…</span>
                : l.fetched ? <span className="ok">read ✓</span>
                : l.fetch_error ? <span className="error">couldn't read: {l.fetch_error}</span>
                : l.status === "error" ? <span className="error">couldn't read this link</span>
                : <span className="muted">saved, not read</span>}
            </span>
            <button type="button" className="link" onClick={() => rmLink.mutate(l.id)}>Remove</button>
          </li>
        ))}
      </ul>

      {onDone && (
        <>
          <div className="actions">
            {onBack && <button type="button" className="secondary" onClick={onBack}>Back</button>}
            <button type="button" disabled={!readable} onClick={onDone}>Analyze my background</button>
          </div>
          {!readable && <p className="hint">Add a resume or a readable link to continue.</p>}
        </>
      )}
    </div>
  );
}
