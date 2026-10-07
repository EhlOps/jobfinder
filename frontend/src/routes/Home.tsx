import { Navigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import type { Profile } from "../lib/types";
import Matches from "./Matches";

export default function Home() {
  const profile = useQuery({ queryKey: ["profile"], queryFn: () => api<Profile>("/api/profile") });
  if (!profile.data) return <p className="muted">Loading…</p>;
  // New users haven't told us anything yet: start the wizard.
  if (Object.keys(profile.data.status).length === 0) return <Navigate to="/onboarding" replace />;
  return <Matches />;
}
