import { useEffect, useState, type FormEvent } from "react";
import { Link, Navigate } from "react-router-dom";
import { useAuth } from "../auth/AuthProvider";
import { ErrorText } from "../components/fields";

export default function ResetPassword() {
  const { user, confirmReset } = useAuth();
  // Read once, in an initializer: it runs before any effect, so React StrictMode's double effect in dev can't lose it.
  const [token] = useState(() => new URLSearchParams(window.location.hash.slice(1)).get("token") ?? "");
  const [password, setPassword] = useState("");
  const [again, setAgain] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  // The token travels in the URL fragment so it never reaches server logs; clear it from the address bar once read.
  useEffect(() => {
    if (window.location.hash) history.replaceState(null, "", window.location.pathname);
  }, []);

  if (user) return <Navigate to="/" replace />;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    if (password !== again) return setError(new Error("The passwords don't match"));
    setBusy(true);
    try {
      await confirmReset(token, password);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  if (!token) {
    return (
      <main className="auth">
        <h1>JobFinder</h1>
        <div className="card">
          <p>This link is missing or has already been used.</p>
          <Link to="/login">Back to sign in to request a new link</Link>
        </div>
      </main>
    );
  }

  return (
    <main className="auth">
      <h1>JobFinder</h1>
      <form onSubmit={submit} className="card">
        <h2>Choose a password</h2>
        <label className="field"><span className="field-label">New password</span>
          <input type="password" required minLength={10} maxLength={256} autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} />
          <span className="hint">At least 10 characters.</span>
        </label>
        <label className="field"><span className="field-label">Confirm password</span>
          <input type="password" required minLength={10} maxLength={256} autoComplete="new-password" value={again} onChange={(e) => setAgain(e.target.value)} />
        </label>
        <ErrorText error={error} />
        <button disabled={busy}>{busy ? "…" : "Save password and sign in"}</button>
        <Link to="/login" className="link">Request a new link</Link>
      </form>
    </main>
  );
}
