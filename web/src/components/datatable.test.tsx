import type { LegacyColumnDef } from "@tanstack/react-table/legacy";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { PaginationState, SortingState } from "./controls";
import { DataTable } from "./DataTable";

interface Row {
  name: string;
  score: number;
}

const columns: LegacyColumnDef<Row>[] = [
  { accessorKey: "name", header: "Name" },
  { accessorKey: "score", header: "Score" },
];

const data: Row[] = [
  { name: "Alice", score: 0.4 },
  { name: "Bob", score: 0.9 },
];

afterEach(cleanup);

describe("DataTable", () => {
  it("renders rows with table semantics", () => {
    render(<DataTable columns={columns} data={data} caption="Test table" />);
    expect(screen.getByRole("table")).toBeTruthy();
    expect(screen.getAllByRole("columnheader")).toHaveLength(2);
    expect(screen.getByText("Alice")).toBeTruthy();
    expect(screen.getByText("Bob")).toBeTruthy();
    expect(screen.getAllByRole("cell").length).toBe(4);
  });

  it("sorts client-side when not manual", () => {
    render(<DataTable columns={columns} data={data} />);
    const scoreHeader = screen.getByRole("button", { name: /Score/ });
    // Numeric columns auto-sort descending on first click: Bob (0.9) first.
    fireEvent.click(scoreHeader);
    const cells = screen.getAllByRole("cell").map((c) => c.textContent);
    expect(cells[0]).toBe("Bob");
    expect(cells[2]).toBe("Alice");
  });

  it("calls onSortingChange in controlled (server-side) mode", () => {
    const onSortingChange = vi.fn();
    const sorting: SortingState = [];
    render(
      <DataTable
        columns={columns}
        data={data}
        sorting={sorting}
        onSortingChange={onSortingChange}
        manualSorting
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /Score/ }));
    expect(onSortingChange).toHaveBeenCalledTimes(1);
    const next = onSortingChange.mock.calls[0]?.[0] as SortingState;
    expect(next).toEqual([{ id: "score", desc: true }]);
  });

  it("calls onPaginationChange from the pagination controls", () => {
    const onPaginationChange = vi.fn();
    const pagination: PaginationState = { pageIndex: 0, pageSize: 2 };
    render(
      <DataTable
        columns={columns}
        data={data}
        pagination={pagination}
        onPaginationChange={onPaginationChange}
        manualPagination
        pageCount={3}
        rowCount={6}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(onPaginationChange).toHaveBeenCalledWith({ pageIndex: 1, pageSize: 2 });
    expect(screen.getByText(/of 6/)).toBeTruthy();
  });

  it("renders the empty state", () => {
    render(<DataTable columns={columns} data={[]} emptyTitle="No analysts" />);
    expect(screen.getByText("No analysts")).toBeTruthy();
  });

  it("renders the error state with retry", () => {
    const onRetry = vi.fn();
    render(<DataTable columns={columns} data={[]} error={new Error("boom")} onRetry={onRetry} />);
    expect(screen.getByRole("alert")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("renders a loading skeleton", () => {
    render(<DataTable columns={columns} data={data} loading />);
    expect(screen.getByRole("status")).toBeTruthy();
    expect(screen.queryByText("Alice")).toBeNull();
  });
});
