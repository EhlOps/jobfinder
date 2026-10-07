import { createContext, useContext, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "../lib/api";
import type { User } from "../lib/types";

interface AuthCtx {
  user: User | null;
  loading: boolean;
  login: (email: string, password: string) => Promise<void>;
  requestLink: (email: string) => Promise<string>;
  confirmReset: (token: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
}

const Ctx = createContext<AuthCtx | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const me = useQuery({
    queryKey: ["me"],
    retry: false,
    queryFn: async () => {
      try {
        return await api<User>("/api/auth/me");
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) return null;
        throw e;
      }
    },
  });
  const set = (u: User | null) => qc.setQueryData(["me"], u);
  const login = useMutation({ mutationFn: (v: { email: string; password: string }) => api<User>("/api/auth/login", { json: v }), onSuccess: set });
  const confirm = useMutation({
    mutationFn: (v: { token: string; password: string }) =>
      api<User>("/api/auth/password-reset/confirm", { json: { ...v, timezone: Intl.DateTimeFormat().resolvedOptions().timeZone } }),
    onSuccess: (u) => { qc.removeQueries({ predicate: (q) => q.queryKey[0] !== "me" }); set(u); },
  });
  const logout = useMutation({
    mutationFn: () => api<void>("/api/auth/logout", { method: "POST" }),
    onSuccess: () => { set(null); qc.removeQueries({ predicate: (q) => q.queryKey[0] !== "me" }); },
  });

  return (
    <Ctx.Provider
      value={{
        user: me.data ?? null,
        loading: me.isLoading,
        login: async (email, password) => { await login.mutateAsync({ email, password }); },
        requestLink: async (email) => (await api<{ detail: string }>("/api/auth/password-reset/request", { json: { email } })).detail,
        confirmReset: async (token, password) => { await confirm.mutateAsync({ token, password }); },
        logout: async () => { await logout.mutateAsync(); },
      }}
    >
      {children}
    </Ctx.Provider>
  );
}

export function useAuth(): AuthCtx {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAuth outside AuthProvider");
  return v;
}
