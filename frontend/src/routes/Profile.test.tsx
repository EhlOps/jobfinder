import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { focusManager } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";
import Profile from "./Profile";
import { mockApi, renderApp, USER } from "../test/helpers";
import { emptyBackground } from "../lib/types";

function mockProfile() {
  let background = {};
  const calls = mockApi({
    "GET /api/auth/me": () => USER,
    "GET /api/profile": () => ({ json: { status: {}, background, version: 1 } }),
    "POST /api/onboarding/extract": () => ({ task_id: 1 }),
    "GET /api/tasks/1": () => ({ json: { id: 1, kind: "extract_profile", status: "done", result: { ...emptyBackground, name: "Sam Lee" }, error: null } }),
    "PUT /api/profile/background": (body) => { background = body as object; return { json: { status: {}, background, version: 2 } }; },
  });
  return calls;
}

async function openBackground() {
  fireEvent.click(await screen.findByRole("button", { name: "Background" }));
  expect(await screen.findByRole("heading", { name: "Is this right?" })).toBeInTheDocument();
}

describe("profile background tab", () => {
  it("keeps the extracted form and edits when the profile refetches unchanged", async () => {
    const calls = mockProfile();
    renderApp(<Profile />);
    await openBackground();
    fireEvent.change(screen.getByDisplayValue("Sam Lee"), { target: { value: "Sam T. Lee" } });

    const fetches = calls.filter((c) => c.key === "GET /api/profile").length;
    act(() => { focusManager.setFocused(false); focusManager.setFocused(true); });
    await waitFor(() => expect(calls.filter((c) => c.key === "GET /api/profile").length).toBeGreaterThan(fetches));
    act(() => focusManager.setFocused(undefined));

    expect(screen.getByDisplayValue("Sam T. Lee")).toBeInTheDocument();
    expect(calls.filter((c) => c.key === "POST /api/onboarding/extract")).toHaveLength(1);
  });

  it("saves the form and shows the saved background", async () => {
    const calls = mockProfile();
    renderApp(<Profile />);
    await openBackground();
    fireEvent.click(screen.getByRole("button", { name: "Save background" }));

    expect(await screen.findByText(/Background saved/, {}, { timeout: 3000 })).toBeInTheDocument();
    expect(calls.find((c) => c.key === "PUT /api/profile/background")?.body).toMatchObject({ name: "Sam Lee" });
    await waitFor(() => expect(screen.queryByText(/Not saved yet/)).not.toBeInTheDocument());
    expect(screen.getByDisplayValue("Sam Lee")).toBeInTheDocument();
    expect(calls.filter((c) => c.key === "POST /api/onboarding/extract")).toHaveLength(1);
  });
});
