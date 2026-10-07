import { useState, type FormEvent } from "react";
import { Navigate, useSearchParams } from "react-router-dom";
import { useAuth } from "../auth/AuthProvider";
import { ErrorText } from "../components/fields";

export default function Login() {
  const { user, login, requestLink } = useAuth();
  const [params] = useSearchParams();
  const next = params.get("next");
  // Only follow same-site paths so a crafted link can't redirect somewhere else.
  const target = next && next.startsWith("/") && !next.startsWith("//") ? next : "/";
  const [mode, setMode] = useState<"login" | "link">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  if (user) return <Navigate to={target} replace />;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setNotice("");
    try {
      if (mode === "login") await login(email, password);
      else setNotice(await requestLink(email));
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="auth">
      <h1>JobFinder</h1>
      <p className="muted">Tell us about yourself once; we find and rank jobs, draft cover letters, and email you a daily digest.</p>
      <form onSubmit={submit} className="card">
        <h2>{mode === "login" ? "Sign in" : "Email me a link"}</h2>
        {mode === "link" && <p className="muted">Enter your email and we'll send a link to set or reset your password. Access is by invitation only.</p>}
        <label className="field"><span className="field-label">Email</span>
          <input type="email" required autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} />
        </label>
        {mode === "login" && (
          <label className="field"><span className="field-label">Password</span>
            <input type="password" required autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} />
          </label>
        )}
        <ErrorText error={error} />
        {notice && <p role="status">{notice}</p>}
        <button disabled={busy}>{busy ? "…" : mode === "login" ? "Sign in" : "Send link"}</button>
        <button type="button" className="link" onClick={() => { setMode(mode === "login" ? "link" : "login"); setError(null); setNotice(""); }}>
          {mode === "login" ? "First time here, or forgot your password? Email me a link" : "Back to sign in"}
        </button>
      </form>
    </main>
  );
}
