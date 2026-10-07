import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import App from "./App";
import { NOT_SIGNED_IN, USER, mockApi, renderApp } from "./test/helpers";

describe("routing and the session", () => {
  it("sends a signed-out visitor to the sign-in page", async () => {
    mockApi(NOT_SIGNED_IN);
    renderApp(<App />, { route: "/settings" });
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
  });

  it("keeps the reset-password and email-link pages reachable without a session", async () => {
    mockApi(NOT_SIGNED_IN);
    renderApp(<App />, { route: "/reset-password" });
    expect(await screen.findByText(/missing or has already been used/)).toBeInTheDocument();
  });

  it("shows the app shell for a signed-in user, with AI setup only for admins", async () => {
    const routes = { "GET /api/questions": () => ({ questions: [] }), "GET /api/profile": () => ({ status: {}, background: {}, version: 1 }) };
    mockApi({ ...routes, "GET /api/auth/me": () => USER });
    const { unmount } = renderApp(<App />, { route: "/profile" });
    expect(await screen.findByText(USER.email)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "AI setup" })).not.toBeInTheDocument();
    unmount();
    mockApi({ ...routes, "GET /api/auth/me": () => ({ ...USER, is_admin: true }) });
    renderApp(<App />, { route: "/profile" });
    expect(await screen.findByRole("link", { name: "AI setup" })).toBeInTheDocument();
  });
});
