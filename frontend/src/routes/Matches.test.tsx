import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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

const empty = { items: [], total: 0, counts: { new: 0, saved: 0, applied: 0, dismissed: 0 }, summary: {} };

const list = (items: Match[]) => ({
  "GET /api/profile": () => ({ json: { status: {}, background: {}, version: 1, career_stage: null } }),
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

  it("offers new-grad & internship roles first to students, and lets them switch it off", async () => {
    const urls: string[] = [];
    mockApi({
      "GET /api/profile": () => ({ json: { status: {}, background: {}, version: 1, career_stage: "final_year" } }),
      "GET /api/matches": (_b, url) => { urls.push(url); return empty; },
      "GET /api/questions": () => ({ questions: [] }),
    });
    renderApp(<Matches />, { auth: false });
    const box = await screen.findByRole("checkbox", { name: /new grad/i });
    await waitFor(() => expect(box).toBeChecked());
    // the default is known before the first list fetch: exactly one request, already filtered
    await waitFor(() => expect(urls.length).toBeGreaterThan(0));
    expect(urls).toHaveLength(1);
    expect(urls[0]).toContain("early_career=true");
    urls.length = 0;
    await userEvent.click(box);
    expect(box).not.toBeChecked();
    await waitFor(() => expect(urls.length).toBeGreaterThan(0));
    expect(urls.every((u) => !u.includes("early_career"))).toBe(true);
  });

  it("does not filter for people who are not students", async () => {
    const urls: string[] = [];
    mockApi({
      "GET /api/profile": () => ({ json: { status: {}, background: {}, version: 1, career_stage: null } }),
      "GET /api/matches": (_b, url) => { urls.push(url); return empty; },
      "GET /api/questions": () => ({ questions: [] }),
    });
    renderApp(<Matches />, { auth: false });
    expect(await screen.findByRole("checkbox", { name: /new grad/i })).not.toBeChecked();
    await waitFor(() => expect(urls.length).toBeGreaterThan(0));
    expect(urls.every((u) => !u.includes("early_career"))).toBe(true);
  });

  it("still loads matches, unfiltered, when the profile cannot be fetched", async () => {
    mockApi({
      "GET /api/profile": () => ({ status: 500, json: { detail: "boom" } }),
      "GET /api/matches": () => ({ ...empty, items: [match(1, "Backend Engineer", "https://acme.example/jobs/1")], total: 1 }),
      "GET /api/questions": () => ({ questions: [] }),
    });
    renderApp(<Matches />, { auth: false });
    expect(await screen.findByText("Backend Engineer")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /new grad/i })).not.toBeChecked();
  });
});
