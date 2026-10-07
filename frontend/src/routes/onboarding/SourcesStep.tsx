import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import type { DocumentOut, LinkOut } from "../../lib/types";
import { ErrorText } from "../../components/fields";
import { AiBanner } from "../../components/AiBanner";

const LINK_KINDS: [string, string][] = [["github", "GitHub"], ["portfolio", "Portfolio / website"], ["linkedin", "LinkedIn"], ["x", "X / Twitter"], ["other", "Other"]];

export default function SourcesStep({ onDone, onBack }: { onDone?: () => void; onBack?: () => void }) {
  const qc = useQueryClient();
  const docs = useQuery({ queryKey: ["documents"], queryFn: () => api<DocumentOut[]>("/api/documents") });
  const links = useQuery({ queryKey: ["links"], queryFn: () => api<LinkOut[]>("/api/links") });
  const fileRef = useRef<HTMLInputElement>(null);
  const [kind, setKind] = useState("github");
  const [url, setUrl] = useState("");

  const upload = useMutation({
    mutationFn: (file: File) => { const f = new FormData(); f.append("file", file); f.append("kind", "resume"); return api<DocumentOut>("/api/documents", { form: f }); },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["documents"] }),
  });
  const rmDoc = useMutation({ mutationFn: (id: string) => api<void>(`/api/documents/${id}`, { method: "DELETE" }), onSuccess: () => qc.invalidateQueries({ queryKey: ["documents"] }) });
  const addLink = useMutation({
    mutationFn: () => api<LinkOut>("/api/links", { json: { kind, url } }),
    onSuccess: () => { setUrl(""); qc.invalidateQueries({ queryKey: ["links"] }); },
  });
  const rmLink = useMutation({ mutationFn: (id: string) => api<void>(`/api/links/${id}`, { method: "DELETE" }), onSuccess: () => qc.invalidateQueries({ queryKey: ["links"] }) });

  const readable = (docs.data?.some((d) => d.chars > 0) ?? false) || (links.data?.some((l) => l.fetched) ?? false);

  return (
    <div className="card">
      <AiBanner />
      <h2>Your resume and links</h2>
      <p className="muted">We read these to build your profile. Nothing is shared outside the app.</p>

      <h3>Resume / documents</h3>
      <p className="hint">PDF, DOCX, TXT or MD, up to 10MB. Tip: LinkedIn blocks scraping, so upload your LinkedIn "Save to PDF" export instead.</p>
      <input ref={fileRef} type="file" accept=".pdf,.docx,.txt,.md" onChange={(e) => { const f = e.target.files?.[0]; if (f) upload.mutate(f); if (fileRef.current) fileRef.current.value = ""; }} />
      {upload.isPending && <p className="muted">Uploading…</p>}
      <ErrorText error={upload.error} />
      <ul className="items">
        {docs.data?.map((d) => (
          <li key={d.id}>
            <span>{d.filename} <span className="muted">({d.chars.toLocaleString()} characters read)</span></span>
            <button type="button" className="link" onClick={() => rmDoc.mutate(d.id)}>Remove</button>
          </li>
        ))}
      </ul>

      <h3>Links</h3>
      <form className="row" onSubmit={(e) => { e.preventDefault(); addLink.mutate(); }}>
        <select value={kind} onChange={(e) => setKind(e.target.value)}>{LINK_KINDS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select>
        <input type="url" required placeholder="https://…" value={url} onChange={(e) => setUrl(e.target.value)} style={{ flex: 3 }} />
        <button disabled={addLink.isPending}>{addLink.isPending ? "Fetching…" : "Add"}</button>
      </form>
      <ErrorText error={addLink.error} />
      <ul className="items">
        {links.data?.map((l) => (
          <li key={l.id}>
            <span>{l.kind}: {l.url}{" "}
              {l.fetched ? <span className="ok">read ✓</span> : l.fetch_error ? <span className="error">couldn't read: {l.fetch_error}</span> : <span className="muted">saved, not read</span>}
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
