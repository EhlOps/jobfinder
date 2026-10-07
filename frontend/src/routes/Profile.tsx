import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import type { Fact, Profile as P } from "../lib/types";
import StatusStep from "./onboarding/StatusStep";
import BackgroundStep from "./onboarding/BackgroundStep";
import SourcesStep from "./onboarding/SourcesStep";
import { ErrorText } from "../components/fields";

const TABS = [["prefs", "Job search"], ["background", "Background"], ["sources", "Resume & links"], ["answers", "Your answers"]] as const;
type Tab = (typeof TABS)[number][0];

function Answers() {
  const qc = useQueryClient();
  const facts = useQuery({ queryKey: ["facts"], queryFn: () => api<Fact[]>("/api/profile/facts") });
  const remove = useMutation({
    mutationFn: (id: number) => api<void>(`/api/profile/facts/${id}`, { method: "DELETE" }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["facts"] }); qc.invalidateQueries({ queryKey: ["profile"] }); },
  });
  return (
    <div className="card">
      <h3 style={{ marginTop: 0 }}>What you've told us</h3>
      <p className="muted">These answers shape your matches and cover letters. Delete one if it's wrong or out of date.</p>
      {facts.data?.length === 0 && <p className="muted">Nothing yet. Answers from onboarding and from the questions we email you appear here.</p>}
      <ul className="items">
        {facts.data?.map((f) => (
          <li key={f.id} style={{ alignItems: "flex-start" }}>
            <span><strong>{f.question}</strong><br />{f.answer}</span>
            <button type="button" className="link" onClick={() => remove.mutate(f.id)}>Delete</button>
          </li>
        ))}
      </ul>
      <ErrorText error={remove.error} />
    </div>
  );
}

export default function Profile() {
  const qc = useQueryClient();
  const [tab, setTab] = useState<Tab>("prefs");
  const [saved, setSaved] = useState<string | null>(null);
  const profile = useQuery({ queryKey: ["profile"], queryFn: () => api<P>("/api/profile"), staleTime: 0 });
  if (!profile.data) return <p className="muted">Loading…</p>;

  const done = (what: string) => () => {
    qc.invalidateQueries({ queryKey: ["profile"] });
    setSaved(`${what} saved. Click “Find new matches” on the matches page to refresh your scores.`);
  };

  return (
    <>
      <h2>Your profile</h2>
      <ul className="tabs">
        {TABS.map(([key, label]) => (
          <li key={key}><button type="button" className={tab === key ? "tab on" : "tab"} onClick={() => { setTab(key); setSaved(null); }}>{label}</button></li>
        ))}
      </ul>
      {saved && <p className="ok">{saved}</p>}
      {tab === "prefs" && <StatusStep key={profile.dataUpdatedAt} initial={profile.data.status} onDone={done("Preferences")} submitLabel="Save preferences" />}
      {tab === "background" && <BackgroundStep key={profile.dataUpdatedAt} initial={profile.data.background} onDone={done("Background")} submitLabel="Save background" />}
      {tab === "sources" && <SourcesStep />}
      {tab === "answers" && <Answers />}
    </>
  );
}
