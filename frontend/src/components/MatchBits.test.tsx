import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { JobChips, SponsorBadge } from "./MatchBits";
import type { MatchJob } from "../lib/types";

const job: MatchJob = {
  id: 1, title: "Engineer", company: "Acme", location: "Boston, MA", workplace_type: null, seniority: null,
  salary_min: null, salary_max: null, salary_currency: null, url: "https://acme.example/1", posted_at: null, is_active: true,
};

describe("sponsorship badge", () => {
  it.each([
    ["sponsors", "Sponsors visas"], ["likely", "Likely sponsors"], ["refuses", "No sponsorship"], ["unlikely", "Unlikely to sponsor"],
  ] as const)("shows %s", (value, label) => {
    render(<SponsorBadge sponsorship={value} />);
    expect(screen.getByTestId("sponsor-badge")).toHaveTextContent(label);
  });

  it("shows nothing when unknown", () => {
    render(<SponsorBadge sponsorship={null} />);
    expect(screen.queryByTestId("sponsor-badge")).toBeNull();
  });

  it("appears with the job chips", () => {
    render(<JobChips job={{ ...job, sponsorship: "refuses" }} />);
    expect(screen.getByText("No sponsorship")).toBeInTheDocument();
  });
});
