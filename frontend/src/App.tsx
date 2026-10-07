import { Link, Navigate, NavLink, Outlet, Route, Routes, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "./lib/api";
import type { QuestionItem } from "./lib/types";
import { useAuth } from "./auth/AuthProvider";
import Login from "./routes/Login";
import ResetPassword from "./routes/ResetPassword";
import Home from "./routes/Home";
import Onboarding from "./routes/Onboarding";
import AiSettings from "./routes/AiSettings";
import MatchDetail from "./routes/MatchDetail";
import Questions from "./routes/Questions";
import QuestionsByLink from "./routes/QuestionsByLink";
import Settings from "./routes/Settings";
import Profile from "./routes/Profile";

function Shell() {
  const { user, loading, logout } = useAuth();
  const location = useLocation();
  const questions = useQuery({
    queryKey: ["questions"], enabled: !!user, staleTime: 60_000,
    queryFn: () => api<{ questions: QuestionItem[] }>("/api/questions"),
  });
  if (loading) return <p className="muted pad">Loading…</p>;
  if (!user) return <Navigate to={`/login?next=${encodeURIComponent(location.pathname + location.search)}`} replace />;
  const open = questions.data?.questions.length ?? 0;
  return (
    <>
      <header className="top">
        <Link to="/" className="brand">JobFinder</Link>
        <nav className="nav">
          <NavLink to="/" end>Matches</NavLink>
          <NavLink to="/questions">Questions{open > 0 && <span className="badge">{open}</span>}</NavLink>
          <NavLink to="/profile">Profile</NavLink>
          <NavLink to="/settings">Settings</NavLink>
          {user.is_admin && <NavLink to="/settings/ai">AI setup</NavLink>}
        </nav>
        <span className="muted">{user.email}</span>
        <button className="link" onClick={() => logout()}>Log out</button>
      </header>
      <main className="page"><Outlet /></main>
    </>
  );
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/reset-password" element={<ResetPassword />} />
      <Route path="/q/:token" element={<QuestionsByLink />} />
      <Route element={<Shell />}>
        <Route path="/" element={<Home />} />
        <Route path="/matches/:id" element={<MatchDetail />} />
        <Route path="/onboarding" element={<Onboarding />} />
        <Route path="/questions" element={<Questions />} />
        <Route path="/profile" element={<Profile />} />
        <Route path="/settings" element={<Settings />} />
        <Route path="/settings/ai" element={<AiSettings />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
