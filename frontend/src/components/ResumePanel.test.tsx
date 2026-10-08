import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { ResumePanel } from "./ResumePanel";
import { mockApi, renderApp, USER } from "../test/helpers";
import type { Resume } from "../lib/types";

const RESUME: Resume = {
  content: {
    contact: { name: "Sam Lee", email: "sam@example.com", phone: "", location: "Boston", links: [] },
    summary: "Backend engineer.",
    skills: ["Python", "SQL"],
    experience: [{ kind: "internship", company: "Acme", title: "Intern", start: "2025", end: "2025", location: "", summary: "", bullets: ["Built APIs"], technologies: ["Python"] }],
    projects: [],
    education: [{ school: "NEU", degree: "BS", field: "CS", start: "", end: "2026", gpa: "" }],
  },
  edited: false,
  coverage: { percent: 67, covered: ["Python"], missing: ["Kubernetes", "Terraform"], loose: [], ats: "greenhouse", notes: [] },
  generated_at: "2026-10-03T00:00:00Z",
  updated_at: "2026-10-03T00:00:00Z",
};

const base = "/api/matches/7/resume";
const auth = { "GET /api/auth/me": () => USER };

describe("resume panel", () => {
  it("generates via a polled task, then shows the resume", async () => {
    let generated = false;
    const calls = mockApi({
      ...auth,
      [`GET ${base}`]: () => ({ resume: generated ? RESUME : null, pending_task_id: null }),
      [`POST ${base}`]: () => ({ status: 202, json: { task_id: 5 } }),
      "GET /api/tasks/5": () => {
        generated = true;
        return { json: { id: 5, kind: "resume_tailor", status: "done", result: {}, error: null } };
      },
    });
    renderApp(<ResumePanel matchId={7} />);
    await userEvent.click(await screen.findByRole("button", { name: "Generate resume" }));
    expect(await screen.findByText("67% keyword coverage")).toBeInTheDocument();
    expect(calls.find((c) => c.key === `POST ${base}`)?.body).toEqual({ force: false });
    expect(screen.getByText(/read it through before sending/)).toBeInTheDocument();
  });

  it("renders the missing keywords", async () => {
    mockApi({ ...auth, [`GET ${base}`]: () => ({ resume: RESUME, pending_task_id: null }) });
    renderApp(<ResumePanel matchId={7} />);
    expect(await screen.findByText("Kubernetes")).toBeInTheDocument();
    expect(screen.getByText("Terraform")).toBeInTheDocument();
  });

  it("saves edits as the complete resume", async () => {
    const calls = mockApi({
      ...auth,
      [`GET ${base}`]: () => ({ resume: RESUME, pending_task_id: null }),
      [`PUT ${base}`]: () => RESUME,
    });
    renderApp(<ResumePanel matchId={7} />);
    const summary = await screen.findByLabelText("Summary");
    await userEvent.clear(summary);
    await userEvent.type(summary, "Platform engineer.");
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(calls.some((c) => c.key === `PUT ${base}`)).toBe(true));
    const body = calls.find((c) => c.key === `PUT ${base}`)?.body as { content: Resume["content"] };
    expect(body.content.summary).toBe("Platform engineer.");
    expect(body.content.experience[0].bullets).toEqual(["Built APIs"]);
    expect(body.content.skills).toEqual(["Python", "SQL"]);
  });

  it("links to the docx and pdf downloads", async () => {
    mockApi({ ...auth, [`GET ${base}`]: () => ({ resume: RESUME, pending_task_id: null }) });
    renderApp(<ResumePanel matchId={7} />);
    expect(await screen.findByRole("link", { name: "Download .docx" })).toHaveAttribute("href", `${base}/download?format=docx`);
    expect(screen.getByRole("link", { name: "Download .pdf" })).toHaveAttribute("href", `${base}/download?format=pdf`);
  });
});
