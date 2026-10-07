import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import Matches from "./Matches";
import { mockApi, renderApp } from "../test/helpers";
import type { Match } from "../lib/types";

const match = (id: number, title: string, url: string, score = 90): Match => ({
  id, status: "new", score, confidence: 0.9, verdict: "strong", reasons: ["Python fit"], gaps: ["Kubernetes"],
  stale: false, has_cover_letter: false, scored_at: "2026-10-03T00:00:00Z",
  job: { id, title, company: "Acme", location: "Boston, MA", workplace_type: "hybrid", seniority: null, salary_min: null,
    salary_max: null, salary_currency: null, url, posted_at: null, is_active: true },
});

const list = (items: Match[]) => ({
  "GET /api/matches": () => ({ items, total: items.length, counts: { new: items.length, saved: 0, applied: 0, dismissed: 0 }, summary: {} }),
  "GET /api/questions": () => ({ questions: [] }),
});

describe("matches feed", () => {
  it("lists matches with score, reasons and gaps", async () => {
    mockApi(list([match(1, "Backend Engineer", "https://acme.example/jobs/1")]));
    renderApp(<Matches />, { auth: false });
    expect(await screen.findByText("Backend Engineer")).toBeInTheDocument();
    expect(screen.getByText("Python fit")).toBeInTheDocument();
    expect(screen.getByText(/Kubernetes/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Details" })).toHaveAttribute("href", "/matches/1");
  });

  it("only offers Apply for http(s) links, never javascript:", async () => {
    mockApi(list([
      match(1, "Good Job", "https://acme.example/apply"),
      match(2, "Evil Job", "javascript:alert(document.cookie)", 80),
      match(3, "Empty Job", "", 70),
    ]));
    renderApp(<Matches />, { auth: false });
    await screen.findByText("Evil Job");
    const apply = screen.getAllByRole("link", { name: /Apply/ });
    expect(apply).toHaveLength(1);
    expect(apply[0]).toHaveAttribute("href", "https://acme.example/apply");
    expect(apply[0]).toHaveAttribute("rel", expect.stringContaining("noopener"));
    expect(document.querySelector('a[href^="javascript:"]')).toBeNull();
  });

  it("shows an empty state", async () => {
    mockApi(list([]));
    renderApp(<Matches />, { auth: false });
    expect(await screen.findByText(/no matches|nothing/i)).toBeInTheDocument();
  });
});
