import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import SourcesStep from "./SourcesStep";
import { mockApi, renderApp, USER } from "../../test/helpers";
import type { DocumentOut, LinkOut } from "../../lib/types";

const base = {
  "GET /api/auth/me": () => USER,
  "GET /api/ai/status": () => ({ configured: true, source: "env", last_call: null }),
};

const doc = (over: Partial<DocumentOut> = {}): DocumentOut => ({
  id: "d1", status: "ready", kind: "resume", filename: "resume.pdf", size: 1000, chars: 4200, tags: {}, created_at: "2026-10-20T00:00:00Z", duplicate: false, ...over,
});
const link = (over: Partial<LinkOut> = {}): LinkOut => ({
  id: "l1", kind: "github", url: "https://github.com/sam", status: "ready", fetched: true, fetch_error: null, ...over,
});
const task = (id: number, status: string, error: string | null = null) => ({ json: { id, kind: "parse_document", status, result: null, error } });

async function uploadFile() {
  const input = document.querySelector('input[type="file"]') as HTMLInputElement;
  await userEvent.upload(input, new File(["hello"], "resume.pdf", { type: "application/pdf" }));
}

const analyze = () => screen.getByRole("button", { name: "Analyze my background" });

describe("sources step", () => {
  it("shows an upload as processing, polls its task, then shows it ready", async () => {
    let docs: DocumentOut[] = [];
    let polls = 0;
    const calls = mockApi({
      ...base,
      "GET /api/documents": () => docs,
      "GET /api/links": () => [],
      "POST /api/documents": () => {
        docs = [doc({ status: "processing", chars: 0 })];
        return { status: 202, json: { document: docs[0], task_id: 5 } };
      },
      "GET /api/tasks/5": () => {
        polls += 1;
        if (polls < 2) return task(5, "running");
        docs = [doc()];
        return task(5, "done");
      },
    });
    renderApp(<SourcesStep onDone={() => {}} />);
    await screen.findByRole("button", { name: "Analyze my background" });
    await uploadFile();
    expect(await screen.findByText("Processing…")).toBeInTheDocument();
    expect(analyze()).toBeDisabled();
    expect(await screen.findByText("(4,200 characters read)", {}, { timeout: 4000 })).toBeInTheDocument();
    expect(screen.queryByText("Processing…")).not.toBeInTheDocument();
    expect(analyze()).toBeEnabled();
    expect(polls).toBe(2);
    expect(calls.filter((c) => c.key === "GET /api/documents").length).toBeGreaterThanOrEqual(3);
  });

  it("shows a friendly error when the parse task fails", async () => {
    let docs: DocumentOut[] = [];
    mockApi({
      ...base,
      "GET /api/documents": () => docs,
      "GET /api/links": () => [],
      "POST /api/documents": () => {
        docs = [doc({ status: "processing", chars: 0 })];
        return { status: 202, json: { document: docs[0], task_id: 6 } };
      },
      "GET /api/tasks/6": () => {
        docs = [doc({ status: "error", chars: 0 })];
        return task(6, "failed", "rate_limited: slow down");
      },
    });
    renderApp(<SourcesStep onDone={() => {}} />);
    await screen.findByRole("button", { name: "Analyze my background" });
    await uploadFile();
    expect(await screen.findByText(/The AI is busy or at its usage limit/)).toBeInTheDocument();
    expect(await screen.findByText("Couldn't read this file")).toBeInTheDocument();
    expect(analyze()).toBeDisabled();
  });

  it("adds a link as processing, then shows it read", async () => {
    let links: LinkOut[] = [];
    let polls = 0;
    const calls = mockApi({
      ...base,
      "GET /api/documents": () => [],
      "GET /api/links": () => links,
      "POST /api/links": () => {
        links = [link({ status: "processing", fetched: false })];
        return { status: 202, json: { link: links[0], task_id: 7 } };
      },
      "GET /api/tasks/7": () => {
        polls += 1;
        if (polls < 2) return task(7, "queued");
        links = [link()];
        return task(7, "done");
      },
    });
    renderApp(<SourcesStep onDone={() => {}} />);
    await userEvent.type(await screen.findByPlaceholderText("https://…"), "https://github.com/sam");
    await userEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(await screen.findByText("Processing…")).toBeInTheDocument();
    expect(calls.find((c) => c.key === "POST /api/links")?.body).toEqual({ kind: "github", url: "https://github.com/sam" });
    expect(await screen.findByText("read ✓", {}, { timeout: 4000 })).toBeInTheDocument();
    expect(analyze()).toBeEnabled();
  });

  it("refreshes the list straight away when there is no task to poll", async () => {
    let docs: DocumentOut[] = [];
    const calls = mockApi({
      ...base,
      "GET /api/documents": () => docs,
      "GET /api/links": () => [],
      "POST /api/documents": () => {
        docs = [doc({ duplicate: true })];
        return { status: 202, json: { document: docs[0], task_id: null } };
      },
    });
    renderApp(<SourcesStep onDone={() => {}} />);
    await screen.findByRole("button", { name: "Analyze my background" });
    await uploadFile();
    expect(await screen.findByText("(4,200 characters read)")).toBeInTheDocument();
    expect(calls.some((c) => c.key.startsWith("GET /api/tasks/"))).toBe(false);
    expect(analyze()).toBeEnabled();
  });

  it("polls the list while an item is processing with no task in hand (after a reload)", async () => {
    let gets = 0;
    mockApi({
      ...base,
      "GET /api/documents": () => {
        gets += 1;
        return gets < 2 ? [doc({ status: "processing", chars: 0 })] : [doc()];
      },
      "GET /api/links": () => [],
    });
    renderApp(<SourcesStep onDone={() => {}} />);
    expect(await screen.findByText("Processing…")).toBeInTheDocument();
    expect(analyze()).toBeDisabled();
    expect(await screen.findByText("(4,200 characters read)", {}, { timeout: 4000 })).toBeInTheDocument();
    expect(analyze()).toBeEnabled();
    // Once nothing is processing, the list stops polling.
    const settled = gets;
    await new Promise((r) => setTimeout(r, 1800));
    expect(gets).toBe(settled);
  });

  it("falls back to polling the list when the task can't be read", async () => {
    let gets = 0;
    mockApi({
      ...base,
      "GET /api/documents": () => {
        gets += 1;
        return gets < 3 ? [doc({ status: "processing", chars: 0 })] : [doc()];
      },
      "GET /api/links": () => [],
      "POST /api/documents": () => ({ status: 202, json: { document: doc({ status: "processing", chars: 0 }), task_id: 8 } }),
      // GET /api/tasks/8 is unmocked: a 404, as for a pruned task.
    });
    renderApp(<SourcesStep onDone={() => {}} />);
    await screen.findByText("Processing…");
    await uploadFile();
    expect(await screen.findByText("(4,200 characters read)", {}, { timeout: 4000 })).toBeInTheDocument();
  });

  it("keeps analyze disabled while the only documents are processing", async () => {
    mockApi({
      ...base,
      "GET /api/documents": () => [doc({ status: "processing", chars: 0 }), doc({ id: "d2", status: "processing", chars: 900 })],
      "GET /api/links": () => [link({ status: "processing", fetched: false })],
    });
    renderApp(<SourcesStep onDone={() => {}} />);
    await waitFor(() => expect(screen.getAllByText("Processing…")).toHaveLength(3));
    expect(analyze()).toBeDisabled();
    expect(screen.getByText("Add a resume or a readable link to continue.")).toBeInTheDocument();
  });

  it("keeps analyze disabled while a second document is still processing", async () => {
    mockApi({
      ...base,
      "GET /api/documents": () => [doc(), doc({ id: "d2", status: "processing", chars: 0 })],
      "GET /api/links": () => [],
    });
    renderApp(<SourcesStep onDone={() => {}} />);
    await screen.findByText("Still reading your files and links…");
    expect(analyze()).toBeDisabled();
  });
});
