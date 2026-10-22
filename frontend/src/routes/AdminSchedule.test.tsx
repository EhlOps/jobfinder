import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import AdminSchedule from "./AdminSchedule";
import { mockApi, renderApp, USER } from "../test/helpers";

const schedule = {
  backoff_until: null, in_flight: 1, max_per_tick: 3, next_tick: "2026-10-08T12:15:00Z",
  users: [
    { user_id: 1, email: "ann@example.com", last_seen_at: null, last_run_at: "2026-10-08T08:00:00Z", last_scored: 4, pending: 2,
      budget_left: 21, last_planned_at: null, next_due_at: "2026-10-08T11:00:00Z", reason: "3 new jobs", skip: "",
      active: [{ kind: "match_user", status: "queued", run_after: "2026-10-08T12:02:00Z" }] },
    { user_id: 2, email: "bob@example.com", last_seen_at: null, last_run_at: null, last_scored: null, pending: null,
      budget_left: null, last_planned_at: null, next_due_at: null, reason: "", skip: "budget exhausted", active: [] },
  ],
  ingest: { adaptive: true, tick_minutes: 15, interval_hours: 6,
    tiers: [{ tier: 1, boards: 12, due: 3 }, { tier: null, boards: 600, due: 40 }] },
  lanes: [
    { queue: "ai", tasks: 31, wait_p50: 2.25, wait_p95: 74.4, run_p50: 11, run_p95: 236.2, slow_waits: 3 },
    { queue: "io", tasks: 2111, wait_p50: 0.1, wait_p95: 1.5, run_p50: null, run_p95: null, slow_waits: 0 },
  ],
};

describe("admin schedule", () => {
  it("shows each user's last run, next run and what is queued", async () => {
    mockApi({ "GET /api/auth/me": () => ({ ...USER, is_admin: true }), "GET /api/admin/schedule": () => schedule });
    renderApp(<AdminSchedule />);
    expect(await screen.findByText("ann@example.com")).toBeInTheDocument();
    expect(screen.getByText(/4 scored, 2 pending/)).toBeInTheDocument();
    expect(screen.getByText(/match_user \(queued\)/)).toBeInTheDocument();
    expect(screen.getByText("budget exhausted")).toBeInTheDocument();
    expect(screen.getByText("no limit")).toBeInTheDocument();
    expect(screen.getByText(/1 of 3 slots in use/)).toBeInTheDocument();
    expect(screen.getByText(/adaptive, tick every 15 min/)).toBeInTheDocument();
    expect(screen.getByText(/tier 1: 12 boards \(3 due\)/)).toBeInTheDocument();
    expect(screen.getByText(/untiered: 600 boards \(40 due\)/)).toBeInTheDocument();
  });

  it("shows each queue's waits and runs over the last day", async () => {
    mockApi({ "GET /api/auth/me": () => ({ ...USER, is_admin: true }), "GET /api/admin/schedule": () => schedule });
    renderApp(<AdminSchedule />);
    expect(await screen.findByText("Queues (last 24h)")).toBeInTheDocument();
    const ai = screen.getByText("ai").closest("tr")!;
    expect(ai).toHaveTextContent("31");
    expect(ai).toHaveTextContent("2.3s / 74s");
    expect(ai).toHaveTextContent("236s");
    const io = screen.getByText("io").closest("tr")!;
    expect(io).toHaveTextContent("0.1s / 1.5s");
    expect(io).toHaveTextContent("—");
  });

  it("says when planning is paused by a rate limit", async () => {
    mockApi({
      "GET /api/auth/me": () => ({ ...USER, is_admin: true }),
      "GET /api/admin/schedule": () => ({ ...schedule, backoff_until: "2026-10-08T12:30:00Z" }),
    });
    renderApp(<AdminSchedule />);
    expect(await screen.findByText(/Paused until/)).toBeInTheDocument();
  });
});
