import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import type { Settings as S } from "../lib/types";
import { ErrorText } from "../components/fields";

const hourLabel = (h: number) => `${h % 12 === 0 ? 12 : h % 12}:00 ${h < 12 ? "AM" : "PM"}`;

function timezones(current: string): string[] {
  const supported = (Intl as unknown as { supportedValuesOf?: (k: string) => string[] }).supportedValuesOf?.("timeZone") ?? [];
  const list = supported.length ? supported : ["UTC", "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles", "Europe/London", "Europe/Berlin", "Asia/Kolkata", "Asia/Tokyo", "Australia/Sydney"];
  return list.includes(current) ? list : [current, ...list];
}

export default function Settings() {
  const qc = useQueryClient();
  const loaded = useQuery({ queryKey: ["settings"], queryFn: () => api<S>("/api/settings") });
  const [form, setForm] = useState<S | null>(null);
  const [saved, setSaved] = useState(false);
  const [preview, setPreview] = useState<string | null>(null);

  useEffect(() => { if (loaded.data && !form) setForm(loaded.data); }, [loaded.data, form]);

  const save = useMutation({
    mutationFn: (s: S) => api<S>("/api/settings", { method: "PUT", json: s }),
    onSuccess: (s) => { setForm(s); setSaved(true); qc.setQueryData(["settings"], s); setTimeout(() => setSaved(false), 2500); },
  });
  const sendPreview = useMutation({
    mutationFn: () => api<{ to: string; matches: number; questions: number }>("/api/settings/digest/preview", { method: "POST" }),
    onMutate: () => setPreview(null),
    onSuccess: (r) => setPreview(`Sent to ${r.to}: ${r.matches} match${r.matches === 1 ? "" : "es"} and ${r.questions} question${r.questions === 1 ? "" : "s"}.`),
  });

  if (!form) return <p className="muted">Loading…</p>;
  const set = <K extends keyof S>(k: K, v: S[K]) => { setForm({ ...form, [k]: v }); setSaved(false); };

  return (
    <>
      <h2>Settings</h2>
      <p className="muted">One email a day, at the time you choose, and only when there's something to say.</p>
      <form className="card" onSubmit={(e) => { e.preventDefault(); save.mutate(form); }}>
        <label className="check" style={{ margin: "6px 0" }}>
          <input type="checkbox" checked={form.digest_enabled} onChange={(e) => set("digest_enabled", e.target.checked)} />
          Daily digest of new job matches (good fits, 65+)
        </label>
        <label className="check" style={{ margin: "6px 0" }}>
          <input type="checkbox" checked={form.question_emails_enabled} onChange={(e) => set("question_emails_enabled", e.target.checked)} />
          Questions when a promising job needs more detail from me
        </label>
        <div className="row" style={{ marginTop: 12 }}>
          <label className="field">
            <span className="field-label">Send at</span>
            <select value={form.digest_hour} onChange={(e) => set("digest_hour", Number(e.target.value))}>
              {Array.from({ length: 24 }, (_, h) => <option key={h} value={h}>{hourLabel(h)}</option>)}
            </select>
          </label>
          <label className="field">
            <span className="field-label">Your timezone</span>
            <select value={form.timezone} onChange={(e) => set("timezone", e.target.value)}>
              {timezones(form.timezone).map((z) => <option key={z}>{z}</option>)}
            </select>
          </label>
        </div>
        <h3 style={{ marginBottom: 4 }}>Match scoring</h3>
        <label className="check" style={{ margin: "6px 0" }}>
          <input type="checkbox" checked={form.match_budget_enabled} onChange={(e) => set("match_budget_enabled", e.target.checked)} />
          Limit how many jobs Claude scores per day
        </label>
        {form.match_budget_enabled ? (
          <label className="field">
            <span className="field-label">Jobs per day</span>
            <input type="number" min={1} max={1000} value={form.match_budget}
              onChange={(e) => set("match_budget", Math.max(1, Math.min(1000, Number(e.target.value) || 1)))} />
          </label>
        ) : (
          <p className="hint">Every promising job is scored on each run. This can use a lot of your Claude usage and take a while.</p>
        )}
        <ErrorText error={save.error} />
        <div className="actions" style={{ justifyContent: "flex-start" }}>
          <button disabled={save.isPending}>{save.isPending ? "Saving…" : saved ? "Saved ✓" : "Save settings"}</button>
        </div>
      </form>

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Preview</h3>
        <p className="muted">Email yourself what today's message would look like. Nothing is marked as sent.</p>
        <button className="secondary" type="button" disabled={sendPreview.isPending} onClick={() => sendPreview.mutate()}>
          {sendPreview.isPending ? "Sending…" : "Send me a preview"}
        </button>
        {preview && <p className="ok">{preview}</p>}
        <ErrorText error={sendPreview.error} />
      </div>
    </>
  );
}
