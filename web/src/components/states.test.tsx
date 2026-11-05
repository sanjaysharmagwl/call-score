import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { EmptyState, ErrorState, LoadingState } from "./states";

afterEach(cleanup);

describe("states", () => {
  it("LoadingState exposes a status role", () => {
    render(<LoadingState label="Fetching" />);
    expect(screen.getByRole("status")).toBeTruthy();
    expect(screen.getByText("Fetching")).toBeTruthy();
  });

  it("ErrorState exposes an alert role and retry", () => {
    render(<ErrorState error={new Error("nope")} />);
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain("nope");
  });

  it("EmptyState renders title and description", () => {
    render(<EmptyState title="No runs" description="Seed the database first." />);
    expect(screen.getByText("No runs")).toBeTruthy();
    expect(screen.getByText("Seed the database first.")).toBeTruthy();
  });
});
