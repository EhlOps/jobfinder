import type { ReactElement } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";
import { AuthProvider } from "../auth/AuthProvider";

export type Handler = (body: unknown, url: string) => { status?: number; json?: unknown } | unknown;

/** Stubs fetch. Keys are "METHOD /path" (query string ignored); every call is recorded in `calls`. */
export function mockApi(routes: Record<string, Handler>) {
  const calls: { key: string; body: unknown }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const key = `${init?.method ?? "GET"} ${url.split("?")[0]}`;
      const body = init?.body && typeof init.body === "string" ? JSON.parse(init.body) : undefined;
      calls.push({ key, body });
      const h = routes[key];
      if (!h) return new Response(JSON.stringify({ detail: `unmocked ${key}` }), { status: 404 });
      const out = h(body, url) as { status?: number; json?: unknown } | undefined;
      const wrapped = out && typeof out === "object" && ("status" in out || "json" in out) ? out : { json: out };
      return new Response(JSON.stringify(wrapped.json ?? null), {
        status: wrapped.status ?? 200,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
  return calls;
}

export function renderApp(ui: ReactElement, { route = "/", auth = true }: { route?: string; auth?: boolean } = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const tree = (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[route]}>{auth ? <AuthProvider>{ui}</AuthProvider> : ui}</MemoryRouter>
    </QueryClientProvider>
  );
  return render(tree);
}

export const USER = { id: 1, email: "sam@example.com", timezone: "UTC", digest_hour: 8, digest_enabled: true, is_admin: false };
export const NOT_SIGNED_IN = { "GET /api/auth/me": () => ({ status: 401, json: { detail: "Not authenticated" } }) };
