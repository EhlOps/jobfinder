import { fireEvent, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import JobLinks from "./JobLinks";
import { mockApi, renderApp, USER } from "../test/helpers";

const link = (over: Record<string, unknown>) => ({
  id: 1, url: "https://boards.greenhouse.io/acme/jobs/1", status: "resolved", company_id: 7, company: "Acme",
  ats: "greenhouse", error: null, job_count: 2, pasted_job_id: 10, created_at: new Date().toISOString(), resolved_at: null,
  ...over,
});

describe("add a job", () => {
  it("adds a link and lists the company's other roles", async () => {
    let links: unknown[] = [];
    const calls = mockApi({
      "GET /api/auth/me": () => USER,
      "GET /api/job-links": () => links,
      "POST /api/job-links": () => {
        links = [link({})];
        return { status: 201, json: links[0] };
      },
      // wrapped in {json}: a bare object with a "status" key would be read as the HTTP status
      "GET /api/job-links/1": () => ({ json: {
        ...link({}),
        jobs: [
          { id: 10, title: "Robotics Engineer", location: "Boston", url: "https://x/10", posted_at: null, match_id: 5, score: 88 },
          { id: 11, title: "Evil Role", location: "", url: "javascript:alert(1)", posted_at: null, match_id: null, score: null },
        ],
      } }),
    });
    renderApp(<JobLinks />);
    expect(await screen.findByText(/No links yet/)).toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText(/greenhouse/), { target: { value: "https://boards.greenhouse.io/acme/jobs/1" } });
    fireEvent.click(screen.getByText("Find their jobs"));
    expect(await screen.findByText("Robotics Engineer")).toBeInTheDocument();
    expect(calls.find((c) => c.key === "POST /api/job-links")?.body).toEqual({ url: "https://boards.greenhouse.io/acme/jobs/1" });
    expect(screen.getByText("88% match")).toBeInTheDocument();
    expect(screen.getByText(/the one you pasted/)).toBeInTheDocument();
    expect(screen.getByText("2 open roles")).toBeInTheDocument();
    expect(document.querySelector('a[href^="javascript:"]')).toBeNull();
  });

  it("explains links it can't read and shows API errors", async () => {
    mockApi({
      "GET /api/auth/me": () => USER,
      "GET /api/job-links": () => [link({ status: "unresolved", company: null, company_id: null, error: "We can't read this site's job board yet." })],
      "POST /api/job-links": () => ({ status: 429, json: { detail: "You can add up to 20 job links a day. Try again tomorrow." } }),
    });
    renderApp(<JobLinks />);
    expect(await screen.findByText(/can't read this site/)).toBeInTheDocument();
    expect(screen.getByText("Couldn't read this one")).toBeInTheDocument();
    expect(screen.queryByText(/See .* roles/)).toBeNull();
    fireEvent.change(screen.getByPlaceholderText(/greenhouse/), { target: { value: "https://example.com/j" } });
    fireEvent.click(screen.getByText("Find their jobs"));
    await waitFor(() => expect(screen.getByText(/20 job links a day/)).toBeInTheDocument());
  });
});
