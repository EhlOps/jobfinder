import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { emptyStatus, type Profile, type Status } from "../../lib/types";
import { CheckGroup, ErrorText, Field, ListInput, TriState } from "../../components/fields";

const SENIORITY: [string, string][] = [["intern", "Internship"], ["new_grad", "New grad"], ["junior", "Junior"], ["mid", "Mid-level"], ["senior", "Senior"], ["staff", "Staff+"]];
const SIZES: [string, string][] = [["startup", "Startup (<200)"], ["mid", "Mid-size"], ["large", "Large / big tech"]];

export default function StatusStep({ initial, onDone, submitLabel = "Save & continue" }: { initial: Partial<Status>; onDone: () => void; submitLabel?: string }) {
  const [s, setS] = useState<Status>({ ...emptyStatus, ...initial });
  const set = <K extends keyof Status>(k: K, v: Status[K]) => setS((p) => ({ ...p, [k]: v }));
  const save = useMutation({ mutationFn: () => api<Profile>("/api/profile/status", { method: "PUT", json: s }), onSuccess: onDone });
  const num = (v: string) => (v === "" ? null : Number(v));

  return (
    <form className="card" onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
      <h2>Where are you in your search?</h2>
      <Field label="Are you a new grad (or about to be)?"><TriState value={s.is_new_grad} onChange={(v) => set("is_new_grad", v)} /></Field>
      <div className="row">
        <Field label="Graduation date"><input type="month" value={s.graduation_date ?? ""} onChange={(e) => set("graduation_date", e.target.value || null)} /></Field>
        <Field label="Need a job by" hint="Roughly is fine"><input type="date" value={s.need_job_by ?? ""} onChange={(e) => set("need_job_by", e.target.value || null)} /></Field>
      </div>
      <Field label="Roles you're targeting" hint="Comma separated, e.g. Backend engineer, ML engineer"><ListInput value={s.target_roles} onChange={(v) => set("target_roles", v)} /></Field>
      <Field label="Level"><CheckGroup options={SENIORITY} value={s.seniority} onChange={(v) => set("seniority", v)} /></Field>
      <Field label="Where would you live?" hint="Cities, regions or countries, comma separated"><ListInput value={s.target_locations} onChange={(v) => set("target_locations", v)} /></Field>
      <div className="row">
        <Field label="Work style">
          <select value={s.remote_preference ?? ""} onChange={(e) => set("remote_preference", e.target.value || null)}>
            <option value="">No preference</option><option value="remote">Remote</option><option value="hybrid">Hybrid</option><option value="onsite">On-site</option>
          </select>
        </Field>
        <Field label="Willing to relocate?"><TriState value={s.willing_to_relocate} onChange={(v) => set("willing_to_relocate", v)} /></Field>
        <Field label="Need visa sponsorship?"><TriState value={s.needs_visa_sponsorship} onChange={(v) => set("needs_visa_sponsorship", v)} /></Field>
      </div>
      <div className="row">
        <Field label="Minimum salary (USD)" hint="Jobs paying less are hidden when pay is listed"><input type="number" min={0} step={1000} value={s.salary_min ?? ""} onChange={(e) => set("salary_min", num(e.target.value))} /></Field>
        <Field label="Target salary (USD)"><input type="number" min={0} step={1000} value={s.salary_target ?? ""} onChange={(e) => set("salary_target", num(e.target.value))} /></Field>
      </div>
      <Field label="How prestigious should the company be?">
        <select value={s.prestige_preference ?? ""} onChange={(e) => set("prestige_preference", e.target.value ? Number(e.target.value) : null)}>
          <option value="">No preference</option>
          <option value="1">1 – Don't care, just good work</option>
          <option value="2">2 – Solid company</option>
          <option value="3">3 – Well-known</option>
          <option value="4">4 – Highly selective</option>
          <option value="5">5 – Top-tier only</option>
        </select>
      </Field>
      <Field label="Company size"><CheckGroup options={SIZES} value={s.company_sizes} onChange={(v) => set("company_sizes", v)} /></Field>
      <Field label="Industries you like" hint="Comma separated, e.g. fintech, healthcare, dev tools"><ListInput value={s.industries} onChange={(v) => set("industries", v)} /></Field>
      <Field label="Anything else we should know?"><textarea rows={3} value={s.notes ?? ""} onChange={(e) => set("notes", e.target.value || null)} /></Field>
      <ErrorText error={save.error} />
      <button disabled={save.isPending}>{save.isPending ? "Saving…" : submitLabel}</button>
    </form>
  );
}
