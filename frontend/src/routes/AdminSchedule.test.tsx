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
