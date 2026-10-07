import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import Login from "./Login";
import { NOT_SIGNED_IN, USER, mockApi, renderApp } from "../test/helpers";

describe("sign-in page", () => {
  it("has no sign-up path, only sign in and an email-me-a-link option", async () => {
    mockApi(NOT_SIGNED_IN);
    renderApp(<Login />, { route: "/login" });
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.queryByText(/sign up/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /first time here, or forgot your password\? email me a link/i })).toBeInTheDocument();
  });

  it("signs in with the typed credentials", async () => {
    const calls = mockApi({ ...NOT_SIGNED_IN, "POST /api/auth/login": () => USER });
    renderApp(<Login />, { route: "/login" });
    await userEvent.type(await screen.findByLabelText("Email"), "sam@example.com");
    await userEvent.type(screen.getByLabelText("Password"), "correct-horse-battery");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(calls.find((c) => c.key === "POST /api/auth/login")?.body).toEqual({ email: "sam@example.com", password: "correct-horse-battery" }));
  });

  it("shows the server's error for a failed sign-in", async () => {
    mockApi({ ...NOT_SIGNED_IN, "POST /api/auth/login": () => ({ status: 401, json: { detail: "Invalid email or password" } }) });
    renderApp(<Login />, { route: "/login" });
    await userEvent.type(await screen.findByLabelText("Email"), "a@b.com");
    await userEvent.type(screen.getByLabelText("Password"), "nope");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("Invalid email or password")).toBeInTheDocument();
  });

  it("requests an emailed link and shows the generic confirmation", async () => {
    const calls = mockApi({
      ...NOT_SIGNED_IN,
      "POST /api/auth/password-reset/request": () => ({ detail: "If that address has access, we've emailed a link. It expires in 60 minutes." }),
    });
    renderApp(<Login />, { route: "/login" });
    await userEvent.click(await screen.findByRole("button", { name: /email me a link/i }));
    expect(screen.queryByLabelText("Password")).not.toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Email"), "new@x.com");
    await userEvent.click(screen.getByRole("button", { name: "Send link" }));
    expect(await screen.findByRole("status")).toHaveTextContent("If that address has access");
    expect(calls.find((c) => c.key === "POST /api/auth/password-reset/request")?.body).toEqual({ email: "new@x.com" });
  });
});
