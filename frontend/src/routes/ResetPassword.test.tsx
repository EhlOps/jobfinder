import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import ResetPassword from "./ResetPassword";
import { NOT_SIGNED_IN, USER, mockApi, renderApp } from "../test/helpers";

afterEach(() => window.history.replaceState(null, "", "/"));

describe("set / reset password page", () => {
  it("reads the token from the URL fragment, clears it from the address bar and submits it", async () => {
    window.history.replaceState(null, "", "/reset-password#token=abc.def");
    const calls = mockApi({ ...NOT_SIGNED_IN, "POST /api/auth/password-reset/confirm": () => USER });
    renderApp(<ResetPassword />, { route: "/reset-password" });
    expect(await screen.findByRole("heading", { name: "Choose a password" })).toBeInTheDocument();
    expect(window.location.hash).toBe("");
    await userEvent.type(screen.getByLabelText(/^New password/), "a-brand-new-password");
    await userEvent.type(screen.getByLabelText("Confirm password"), "a-brand-new-password");
    await userEvent.click(screen.getByRole("button", { name: /save password and sign in/i }));
    await waitFor(() => {
      const body = calls.find((c) => c.key === "POST /api/auth/password-reset/confirm")?.body as Record<string, string>;
      expect(body).toMatchObject({ token: "abc.def", password: "a-brand-new-password" });
      expect(typeof body.timezone).toBe("string");
    });
  });

  it("refuses mismatched passwords without calling the server", async () => {
    window.history.replaceState(null, "", "/reset-password#token=abc.def");
    const calls = mockApi(NOT_SIGNED_IN);
    renderApp(<ResetPassword />, { route: "/reset-password" });
    await userEvent.type(await screen.findByLabelText(/^New password/), "a-brand-new-password");
    await userEvent.type(screen.getByLabelText("Confirm password"), "a-different-password");
    await userEvent.click(screen.getByRole("button", { name: /save password/i }));
    expect(await screen.findByText("The passwords don't match")).toBeInTheDocument();
    expect(calls.some((c) => c.key.includes("confirm"))).toBe(false);
  });

  it("shows the server's message for an expired or used link", async () => {
    window.history.replaceState(null, "", "/reset-password#token=old");
    mockApi({ ...NOT_SIGNED_IN, "POST /api/auth/password-reset/confirm": () => ({ status: 400, json: { detail: "This link is invalid or has expired. Request a new one from the sign-in page." } }) });
    renderApp(<ResetPassword />, { route: "/reset-password" });
    await userEvent.type(await screen.findByLabelText(/^New password/), "a-brand-new-password");
    await userEvent.type(screen.getByLabelText("Confirm password"), "a-brand-new-password");
    await userEvent.click(screen.getByRole("button", { name: /save password/i }));
    expect(await screen.findByText(/invalid or has expired/)).toBeInTheDocument();
  });

  it("without a token points back to sign-in", async () => {
    mockApi(NOT_SIGNED_IN);
    renderApp(<ResetPassword />, { route: "/reset-password" });
    expect(await screen.findByText(/missing or has already been used/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /request a new link/i })).toHaveAttribute("href", "/login");
  });
});
