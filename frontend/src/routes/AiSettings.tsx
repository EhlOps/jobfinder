import { useEffect, useState } from "react";
import { Navigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { useAuth } from "../auth/AuthProvider";
import { ErrorText } from "../components/fields";
import { useTask } from "../lib/useTask";
import type { AiCheckResult, AiStatus } from "../lib/types";

export default function AiSettings() {
  const { user } = useAuth();
  const qc = useQueryClient();
  const [token, setToken] = useState("");
  const [taskId, setTaskId] = useState<number | null>(null);

  const status = useQuery({ queryKey: ["ai-status"], queryFn: () => api<AiStatus>("/api/ai/status") });
  const check = useTask<AiCheckResult>(taskId);
  const checking = taskId !== null && check.data?.status !== "done" && check.data?.status !== "failed";
  const result = check.data?.status === "done" ? check.data.result : null;

  useEffect(() => {
    if (check.data?.status === "done" || check.data?.status === "failed") qc.invalidateQueries({ queryKey: ["ai-status"] });
  }, [check.data?.status, qc]);

  const save = useMutation({
    mutationFn: () => api<{ task_id: number }>("/api/admin/ai/token", { method: "PUT", json: { token } }),
    onSuccess: (r) => { setToken(""); setTaskId(r.task_id); qc.invalidateQueries({ queryKey: ["ai-status"] }); },
  });
  const test = useMutation({
    mutationFn: () => api<{ task_id: number }>("/api/admin/ai/check", { method: "POST" }),
    onSuccess: (r) => setTaskId(r.task_id),
  });
  const disconnect = useMutation({
    mutationFn: () => api<void>("/api/admin/ai/token", { method: "DELETE" }),
    onSuccess: () => { setTaskId(null); qc.invalidateQueries({ queryKey: ["ai-status"] }); },
  });

  if (!user?.is_admin) return <Navigate to="/" replace />;

  const s = status.data;
  const lastFailed = s?.last_call && !s.last_call.ok;
  let badge: { cls: string; text: string };
  if (!s) badge = { cls: "muted", text: "Checking…" };
  else if (!s.configured) badge = { cls: "error", text: "Not connected" };
  else if (result?.ok || (s.last_call?.ok && !lastFailed)) badge = { cls: "ok", text: "Connected" };
  else if (lastFailed) badge = { cls: "error", text: `Last call failed (${s.last_call?.error_kind ?? "error"})` };
  else badge = { cls: "muted", text: "Token saved — run Test connection" };

  return (
    <>
      <h2>AI setup</h2>
      <p className="muted">
        Everything smart in JobFinder (reading your resume, matching, cover letters) runs through Claude Code on your Claude
        subscription. Connect it once here — you never need to open the worker container.
      </p>

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Status: <span className={badge.cls}>{badge.text}</span></h3>
        {s?.source === "env" && <p className="hint">Using the token from the server's environment (CLAUDE_CODE_OAUTH_TOKEN). A token saved here takes priority.</p>}
        {checking && <p className="muted">Testing the connection…</p>}
        {result?.ok && <p className="ok">Claude responded — you're all set.</p>}
        {result && !result.ok && <p className="error">Test failed ({result.error_kind}): {result.error}</p>}
        {check.data?.status === "failed" && <p className="error">Test failed: {check.data.error}</p>}
        <div className="actions" style={{ justifyContent: "flex-start" }}>
          <button className="secondary" disabled={test.isPending || checking || !s?.configured} onClick={() => test.mutate()}>Test connection</button>
          {s?.source === "file" && <button className="secondary" disabled={disconnect.isPending} onClick={() => disconnect.mutate()}>Disconnect</button>}
        </div>
        <ErrorText error={test.error || disconnect.error} />
      </div>

      <form className="card" onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
        <h3 style={{ marginTop: 0 }}>{s?.configured ? "Replace the token" : "Connect Claude"}</h3>
        <ol>
          <li>On a computer where Claude Code is installed and you're signed in, run: <code>claude setup-token</code></li>
          <li>It opens your browser to approve access, then prints a long token. Copy it.</li>
          <li>Paste it below and save. We test it straight away.</li>
        </ol>
        <label className="field">
          <span className="field-label">Token</span>
          <input type="password" autoComplete="off" spellCheck={false} placeholder="paste the token here" value={token} onChange={(e) => setToken(e.target.value)} />
          <span className="hint">Stored only on the server's private credentials volume. It's never shown again or sent to the browser, and everyone using this app shares your subscription's usage limits.</span>
        </label>
        <ErrorText error={save.error} />
        <button disabled={save.isPending || !token.trim()}>{save.isPending ? "Saving…" : "Save & test"}</button>
      </form>
    </>
  );
}
