import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { Route, Routes } from "react-router-dom";
import QuestionsByLink from "./QuestionsByLink";
import { mockApi, renderApp } from "../test/helpers";

const page = <Routes><Route path="/q/:token" element={<QuestionsByLink />} /></Routes>;
const Q = { id: 7, question: "Have you used Kafka?", why: "The job asks for it", jobs: [] };

describe("answering questions from the email link (no login)", () => {
  it("loads by token, submits answers, and thanks the user", async () => {
    const calls = mockApi({
      "GET /api/q/tok123": () => ({ questions: [Q] }),
      "POST /api/q/tok123/answers": () => ({ answered: 1, skipped: 0, remaining: 0, rescoring: true }),
    });
    renderApp(page, { route: "/q/tok123", auth: false });
    await userEvent.type(await screen.findByPlaceholderText("A sentence or two is plenty"), "Yes, at my internship");
    await userEvent.click(screen.getByRole("button", { name: "Send my answers" }));
    expect(await screen.findByText("Thanks!")).toBeInTheDocument();
    await waitFor(() => expect(calls.find((c) => c.key === "POST /api/q/tok123/answers")?.body).toEqual({ answers: [{ id: 7, answer: "Yes, at my internship", skip: false }] }));
  });

  it("explains an expired link and offers login", async () => {
    mockApi({ "GET /api/q/old": () => ({ status: 410, json: { detail: "This link has expired" } }) });
    renderApp(page, { route: "/q/old", auth: false });
    expect(await screen.findByText("This link has expired")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Log in" })).toBeInTheDocument();
  });

  it("says so when nothing is left to answer", async () => {
    mockApi({ "GET /api/q/done": () => ({ questions: [] }) });
    renderApp(page, { route: "/q/done", auth: false });
    expect(await screen.findByText("All done")).toBeInTheDocument();
  });
});
