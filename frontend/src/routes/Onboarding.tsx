import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import type { Profile } from "../lib/types";
import StatusStep from "./onboarding/StatusStep";
import SourcesStep from "./onboarding/SourcesStep";
import BackgroundStep from "./onboarding/BackgroundStep";
import FollowupsStep from "./onboarding/FollowupsStep";

const STEPS = ["Your search", "Resume & links", "Your background", "Follow-ups"];

export default function Onboarding() {
  const [step, setStep] = useState(0);
  const nav = useNavigate();
  const qc = useQueryClient();
  const profile = useQuery({ queryKey: ["profile"], queryFn: () => api<Profile>("/api/profile"), staleTime: 0, gcTime: 0 });

  if (!profile.data) return <p className="muted">Loading…</p>;
  const next = () => { qc.invalidateQueries({ queryKey: ["profile"] }); setStep((s) => s + 1); };
  // Kick off matching right away; the matches page shows the first results as they arrive.
  const finish = async () => {
    try {
      const r = await api<{ task_id: number }>("/api/matches/refresh", { method: "POST" });
      nav(`/?task=${r.task_id}`);
    } catch {
      nav("/");
    }
  };
  const back = () => setStep((s) => Math.max(0, s - 1));

  return (
    <>
      <ol className="steps">
        {STEPS.map((label, i) => (
          <li key={label} className={i === step ? "active" : i < step ? "done" : ""}>{label}</li>
        ))}
      </ol>
      {step === 0 && <StatusStep initial={profile.data.status} onDone={next} />}
      {step === 1 && <SourcesStep onDone={next} onBack={back} />}
      {step === 2 && <BackgroundStep initial={profile.data.background} onDone={next} onBack={back} />}
      {step === 3 && <FollowupsStep onDone={finish} onBack={back} />}
    </>
  );
}
