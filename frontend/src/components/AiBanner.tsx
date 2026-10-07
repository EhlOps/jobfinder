import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { useAuth } from "../auth/AuthProvider";
import type { AiStatus } from "../lib/types";

/** Warns before the user reaches an AI step if Claude isn't connected. */
export function AiBanner() {
  const { user } = useAuth();
  const status = useQuery({ queryKey: ["ai-status"], queryFn: () => api<AiStatus>("/api/ai/status") });
  const s = status.data;
  if (!s) return null;
  const failing = s.last_call && !s.last_call.ok && s.last_call.error_kind === "auth";
  if (s.configured && !failing) return null;
  return (
    <div className="notice">
      <strong>Claude isn't connected, so the analysis steps will fail.</strong>{" "}
      {user?.is_admin ? <Link to="/settings/ai">Connect Claude now</Link> : "Ask the site admin to connect it."}
    </div>
  );
}
