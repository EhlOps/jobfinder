import { fireEvent, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import AdminSources from "./AdminSources";
import { mockApi, renderApp, USER } from "../test/helpers";

const base = {
  ats: "greenhouse", origin: "seed", enabled: true, consecutive_failures: 0, last_success_at: null, last_fetched_at: null,
  last_error: null, disabled_reason: null, validated_at: null, job_count: 3,
};
const rows = [
  { ...base, id: 1, name: "BrokenCo", slug: "broken", enabled: false, consecutive_failures: 5, last_error: "HTTP 404", disabled_reason: "5 consecutive failures" },
  { ...base, id: 2, name: "FineCo", slug: "fine" },
];

describe("admin sources", () => {
  it("shows per-board health and re-enables a disabled board", async () => {
    const calls = mockApi({
      "GET /api/auth/me": () => ({ ...USER, is_admin: true }),
      "GET /api/admin/sources": () => rows,
      "POST /api/admin/sources/1/enable": () => ({ ...rows[0], enabled: true }),
    });
    renderApp(<AdminSources />);
    expect(await screen.findByText("BrokenCo")).toBeInTheDocument();
    expect(screen.getByText("HTTP 404")).toBeInTheDocument();
    expect(screen.getByText(/5 consecutive failures/)).toBeInTheDocument();
    expect(screen.getAllByText("Re-enable")).toHaveLength(1);
    fireEvent.click(screen.getByText("Re-enable"));
    await screen.findByText("FineCo");
    expect(calls.some((c) => c.key === "POST /api/admin/sources/1/enable")).toBe(true);
  });
});
