import { useQuery } from "@tanstack/react-query";
import { api } from "./api";
import type { TaskOut } from "./types";

/** Polls a background task until it finishes. Pass null to stay idle. */
export function useTask<R>(taskId: number | null) {
  return useQuery({
    queryKey: ["task", taskId],
    enabled: taskId !== null,
    queryFn: () => api<TaskOut<R>>(`/api/tasks/${taskId}`),
    refetchInterval: (q) => (q.state.data?.status === "done" || q.state.data?.status === "failed" ? false : 1500),
  });
}

/** True when the failure just means Claude isn't connected (so the fix is on the AI setup page). */
export const isAuthError = (error: string | null) => !!error && error.startsWith("auth:");

export function friendlyTaskError(error: string | null): string {
  if (!error) return "Something went wrong.";
  if (isAuthError(error)) return "Claude isn't connected yet.";
  if (error.startsWith("rate_limited:")) return "The AI is busy or at its usage limit. Try again in a bit.";
  return error;
}
