import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { EligibilityBadge, ProvenanceLabel, ScoreBadge } from "./Badges";

const RUN_ID = "12345678-1234-1234-1234-123456789abc";
const FP = "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789";

afterEach(cleanup);

describe("ScoreBadge", () => {
  it("renders score and rank", () => {
    render(<ScoreBadge score={87.25} rank={3} />);
    expect(screen.getByTestId("score-badge").textContent).toContain("87.3");
    expect(screen.getByTestId("score-badge").textContent).toContain("#3");
  });
});

describe("EligibilityBadge", () => {
  it("renders Ranked", () => {
    render(<EligibilityBadge isRanked />);
    expect(screen.getByTestId("eligibility-badge").textContent).toBe("Ranked");
  });

  it("renders Unranked with reason", () => {
    render(<EligibilityBadge isRanked={false} unrankedReason="too few observations" />);
    expect(screen.getByTestId("eligibility-badge").textContent).toContain("Unranked");
    expect(screen.getByTestId("eligibility-badge").textContent).toContain("too few observations");
  });
});

describe("ProvenanceLabel", () => {
  it("renders short run id, fingerprint, as_of and cutoff", () => {
    render(
      <ProvenanceLabel
        run_id={RUN_ID}
        run_status="succeeded"
        methodology_fingerprint={FP}
        as_of_date="2024-06-28"
        knowledge_cutoff="2024-06-29T00:00:00Z"
      />,
    );
    const label = screen.getByTestId("provenance-label");
    expect(label.textContent).toContain("12345678");
    expect(label.textContent).toContain("succeeded");
    expect(label.textContent).toContain("abcdef012345");
    expect(label.textContent).toContain("2024-06-28");
    expect(label.textContent).toContain("2024-06-29");
    expect(screen.getByTestId("provenance-run").getAttribute("title")).toBe(`run_id: ${RUN_ID}`);
  });

  it("renders nothing when all fields are empty", () => {
    const { container } = render(<ProvenanceLabel />);
    expect(container.firstChild).toBeNull();
  });
});
