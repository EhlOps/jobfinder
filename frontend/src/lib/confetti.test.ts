import { createElement } from "react";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { StatusActions } from "../components/MatchBits";
import { mockApi, renderApp } from "../test/helpers";
import { confetti } from "./confetti";

const fakeCtx = { clearRect: vi.fn(), save: vi.fn(), restore: vi.fn(), translate: vi.fn(), rotate: vi.fn(), fillRect: vi.fn() };

function setReducedMotion(reduce: boolean) {
  vi.stubGlobal("matchMedia", (q: string) => ({ matches: reduce && q.includes("reduce"), media: q }));
}

let getContext: ReturnType<typeof vi.fn>;

beforeEach(() => {
  getContext = vi.fn(() => fakeCtx);
  HTMLCanvasElement.prototype.getContext = getContext as never;
  vi.stubGlobal("requestAnimationFrame", vi.fn());
  setReducedMotion(false);
});

afterEach(() => {
  document.querySelectorAll("canvas").forEach((c) => c.remove());
  vi.unstubAllGlobals();
});

describe("confetti", () => {
  it("draws a canvas overlay", () => {
    confetti();
    expect(getContext).toHaveBeenCalled();
    expect(document.querySelector("canvas.confetti-canvas")).not.toBeNull();
  });

  it("is skipped when reduced motion is set", () => {
    setReducedMotion(true);
    confetti();
    expect(getContext).not.toHaveBeenCalled();
    expect(document.querySelector("canvas")).toBeNull();
  });

  it("does not throw without canvas support", () => {
    getContext.mockReturnValue(null);
    expect(() => confetti()).not.toThrow();
    expect(document.querySelector("canvas")).toBeNull();
  });
});

describe("StatusActions confetti", () => {
  const routes = { "PATCH /api/matches/1": () => ({ id: 1 }) };

  it("fires when a match is marked applied", async () => {
    mockApi(routes);
    renderApp(createElement(StatusActions, { match: { id: 1, status: "new" } }), { auth: false });
    fireEvent.click(screen.getByRole("button", { name: "Mark applied" }));
    await waitFor(() => expect(document.querySelector("canvas.confetti-canvas")).not.toBeNull());
  });

  it("does not fire for saved or dismissed", async () => {
    const calls = mockApi(routes);
    renderApp(createElement(StatusActions, { match: { id: 1, status: "new" } }), { auth: false });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(calls.some((c) => c.key === "PATCH /api/matches/1")).toBe(true));
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    await waitFor(() => expect(calls.filter((c) => c.key === "PATCH /api/matches/1")).toHaveLength(2));
    expect(document.querySelector("canvas")).toBeNull();
  });

  it("does not fire when applied under reduced motion", async () => {
    setReducedMotion(true);
    const calls = mockApi(routes);
    renderApp(createElement(StatusActions, { match: { id: 1, status: "new" } }), { auth: false });
    fireEvent.click(screen.getByRole("button", { name: "Mark applied" }));
    await waitFor(() => expect(calls.some((c) => c.key === "PATCH /api/matches/1")).toBe(true));
    await new Promise((r) => setTimeout(r, 20));
    expect(document.querySelector("canvas")).toBeNull();
  });
});
