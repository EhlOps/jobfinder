import { fireEvent, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import AdminCoverage from "./AdminCoverage";
import { mockApi, renderApp, USER } from "../test/helpers";

const ADMIN = { "GET /api/auth/me": () => ({ ...USER, is_admin: true }) };
const row = {
  ats: "greenhouse", slug: "x", tier: 1, score: 80, pinned: null, board_status: "ok", gap: false, job_count: 3,
  last_success_at: null, recipe_state: null, last_error: null, resolve_error: null, last_resolved_at: null,
  domain: null, careers_url: null,
};
const coverage = {
  companies: [
    { ...row, company_id: 1, name: "BrokenCo", board_status: "disabled", gap: true, resolve_error: "HTTP 404",
      careers_url: "javascript:alert(1)" },
    { ...row, company_id: 2, name: "FineCo", tier: 2, score: 55, careers_url: "https://fine.example/careers" },
  ],
  targets: [
    { id: 7, name: "WantedCo", domain: "wanted.com", source: "admin", score: 100, last_resolved_at: null,
      last_error: "no job board we can read", careers_url: "https://wanted.com/careers" },
  ],
};

describe("admin coverage", () => {
  it("shows gaps first, then everything coveted, and never links a script URL", async () => {
    mockApi({ ...ADMIN, "GET /api/admin/coverage": () => coverage });
    renderApp(<AdminCoverage />);
    expect(await screen.findByText("BrokenCo")).toBeInTheDocument();
    expect(screen.getByText("WantedCo")).toBeInTheDocument();
    expect(screen.getByText("HTTP 404")).toBeInTheDocument();
    expect(screen.queryByText("FineCo")).not.toBeInTheDocument(); // not a gap
    expect(screen.getByText("BrokenCo").closest("a")).toBeNull(); // javascript: never becomes an href
    expect(screen.getByText("WantedCo").closest("a")).toHaveAttribute("href", "https://wanted.com/careers");
    for (const a of document.querySelectorAll("a")) expect(a.getAttribute("href")).not.toMatch(/^javascript:/i);

    fireEvent.click(screen.getByText(/All coveted/));
    expect(await screen.findByText("FineCo")).toBeInTheDocument();
    expect(screen.queryByText("WantedCo")).not.toBeInTheDocument();
  });

  it("pins, unpins and resolves", async () => {
    const calls = mockApi({
      ...ADMIN,
      "GET /api/admin/coverage": () => coverage,
      "POST /api/admin/coverage/pin": () => ({ company_id: 1 }),
      "POST /api/admin/coverage/unpin": () => ({ company_id: 1 }),
      "POST /api/admin/coverage/1/resolve": () => ({ task_id: 5 }),
      "POST /api/admin/coverage/targets/7/resolve": () => ({ task_id: 6 }),
    });
    renderApp(<AdminCoverage />);
    await screen.findByText("BrokenCo");
    fireEvent.click(screen.getByText("Pin"));
    await waitFor(() => expect(calls.find((c) => c.key === "POST /api/admin/coverage/pin")?.body).toEqual({ company_id: 1 }));
    await waitFor(() => expect(screen.getByText("Not coveted")).not.toBeDisabled());
    fireEvent.click(screen.getByText("Not coveted"));
    await waitFor(() =>
      expect(calls.find((c) => c.key === "POST /api/admin/coverage/unpin")?.body).toEqual({ company_id: 1, exclude: true }),
    );
    await waitFor(() => expect(screen.getAllByText("Try to resolve now")[0]).not.toBeDisabled());
    const [targetBtn, companyBtn] = screen.getAllByText("Try to resolve now");
    fireEvent.click(targetBtn);
    await waitFor(() => expect(calls.some((c) => c.key === "POST /api/admin/coverage/targets/7/resolve")).toBe(true));
    await waitFor(() => expect(companyBtn).not.toBeDisabled());
    fireEvent.click(companyBtn);
    await waitFor(() => expect(calls.some((c) => c.key === "POST /api/admin/coverage/1/resolve")).toBe(true));
  });

  it("adds an employer and shows the server's validation error", async () => {
    const calls = mockApi({
      ...ADMIN,
      "GET /api/admin/coverage": () => coverage,
      "POST /api/admin/coverage/pin": (body) =>
        (body as { domain: string }).domain.includes("/")
          ? { status: 422, json: { detail: "Enter the employer's bare domain, like acme.com (no https://, path or port)." } }
          : { target_id: 8 },
    });
    renderApp(<AdminCoverage />);
    await screen.findByText("BrokenCo");
    fireEvent.change(screen.getByLabelText("Employer name"), { target: { value: "Acme" } });
    fireEvent.change(screen.getByLabelText("Domain"), { target: { value: "https://acme.com/careers" } });
    fireEvent.click(screen.getByText("Add"));
    expect(await screen.findByText(/bare domain/)).toBeInTheDocument();
    expect(screen.getByLabelText("Domain")).toHaveValue("https://acme.com/careers"); // kept so it can be fixed

    fireEvent.change(screen.getByLabelText("Domain"), { target: { value: "acme.com" } });
    fireEvent.click(screen.getByText("Add"));
    await waitFor(() => expect(screen.getByLabelText("Domain")).toHaveValue(""));
    const posts = calls.filter((c) => c.key === "POST /api/admin/coverage/pin").map((c) => c.body);
    expect(posts).toEqual([{ name: "Acme", domain: "https://acme.com/careers" }, { name: "Acme", domain: "acme.com" }]);
  });

  it("sends non-admins away", async () => {
    const calls = mockApi({ "GET /api/auth/me": () => USER });
    renderApp(<AdminCoverage />);
    await waitFor(() => expect(calls.some((c) => c.key === "GET /api/auth/me")).toBe(true));
    expect(calls.some((c) => c.key === "GET /api/admin/coverage")).toBe(false);
  });
});
