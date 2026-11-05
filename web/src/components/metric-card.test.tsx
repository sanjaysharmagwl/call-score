import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { MetricStatCard, MetricValue } from "./MetricCard";

afterEach(cleanup);

describe("MetricValue", () => {
  it("renders value, CI, n_obs and peer percentile", () => {
    render(<MetricValue value={0.42} ciLow={0.3} ciHigh={0.54} nObs={37} peerPercentile={82} />);
    expect(screen.getByTestId("metric-value-score").textContent).toBe("0.42");
    expect(screen.getByTestId("metric-ci").textContent).toBe("[0.30, 0.54]");
    expect(screen.getByTestId("metric-n-obs").textContent).toBe("37");
    expect(screen.getByTestId("metric-peer-percentile").textContent).toBe("82%");
  });

  it("renders unavailable reason instead of a value", () => {
    render(<MetricValue value={null} nObs={3} unavailableReason="insufficient observations" />);
    expect(screen.getByTestId("metric-value-score").textContent).toBe("n/a");
    expect(screen.getByTestId("metric-unavailable-reason").textContent).toBe(
      "insufficient observations",
    );
    expect(screen.queryByTestId("metric-ci")).toBeNull();
  });

  it("renders std err when CI is absent", () => {
    render(<MetricValue value={1.5} stdErr={0.2} nObs={10} />);
    expect(screen.getByText("SE")).toBeTruthy();
    expect(screen.getByText("0.20")).toBeTruthy();
  });
});

describe("MetricStatCard", () => {
  it("wraps the metric in a titled card", () => {
    render(<MetricStatCard label="Timing skill" metric={{ value: -0.1, nObs: 12 }} />);
    expect(screen.getByText("Timing skill")).toBeTruthy();
    expect(screen.getByTestId("metric-value")).toBeTruthy();
  });
});
