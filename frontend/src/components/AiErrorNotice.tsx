import { Link } from "react-router-dom";
import { useAuth } from "../auth/AuthProvider";
import { friendlyTaskError, isAuthError } from "../lib/useTask";

/** Shown when an AI step fails. A "not connected" failure tells admins where to fix it. */
export function AiErrorNotice({ error }: { error: string | null }) {
  const { user } = useAuth();
  if (!isAuthError(error)) return <p className="error">{friendlyTaskError(error)}</p>;
  return (
    <div className="notice">
      <p className="error"><strong>Claude isn't connected yet.</strong></p>
      {user?.is_admin ? (
        <p>You're the admin, so you can fix this in a minute: <Link to="/settings/ai">Connect Claude</Link>, then come back and try again.</p>
      ) : (
        <p>Please ask the site admin to connect Claude (it's a one-time setup), then try again.</p>
      )}
    </div>
  );
}
