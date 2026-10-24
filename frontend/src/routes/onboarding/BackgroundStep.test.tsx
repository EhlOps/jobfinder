import { act, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import BackgroundStep from "./BackgroundStep";
import { mockApi, renderApp, USER } from "../../test/helpers";
import { emptyBackground } from "../../lib/types";

const QUICK = "This usually takes under a minute.";
const SLOW = "This is taking longer than usual. We'll keep trying; you can leave this page.";

function mockExtract(status: () => string) {
  return mockApi({
    "GET /api/auth/me": () => USER,
    "POST /api/onboarding/extract": () => ({ task_id: 1 }),
    "GET /api/tasks/1": () => {
      const s = status();
      return { json: { id: 1, kind: "extract_profile", status: s, result: s === "done" ? { ...emptyBackground, name: "Sam Lee" } : null, error: null } };
    },
  });
}

describe("background step", () => {
  beforeEach(() => vi.useFakeTimers({ shouldAdvanceTime: true }));
  afterEach(() => vi.useRealTimers());

  it("says it's taking longer once extraction runs past 3 minutes", async () => {
    const calls = mockExtract(() => "running");
    renderApp(<BackgroundStep initial={{}} onDone={() => {}} />);
    expect(await screen.findByText(QUICK)).toBeInTheDocument();
    await act(() => vi.advanceTimersByTimeAsync(2 * 60_000));
    expect(screen.getByText(QUICK)).toBeInTheDocument();
    await act(() => vi.advanceTimersByTimeAsync(60_000 + 100));
    expect(screen.getByText(SLOW)).toBeInTheDocument();
    expect(screen.queryByText(QUICK)).not.toBeInTheDocument();
    expect(calls.filter((c) => c.key === "POST /api/onboarding/extract")).toHaveLength(1);
    expect(calls.filter((c) => c.key === "GET /api/tasks/1").length).toBeGreaterThan(1);
  });

  it("shows the form once the task is done", async () => {
    let status = "running";
    mockExtract(() => status);
    renderApp(<BackgroundStep initial={{}} onDone={() => {}} />);
    expect(await screen.findByText(QUICK)).toBeInTheDocument();
    status = "done";
    await act(() => vi.advanceTimersByTimeAsync(2000));
    expect(await screen.findByRole("heading", { name: "Is this right?" })).toBeInTheDocument();
    expect(screen.getByDisplayValue("Sam Lee")).toBeInTheDocument();
  });
});
