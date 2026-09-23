/**
 * components/ui/data-table.tsx — the shared TanStack table wrapper used by
 * the review/companies/events dashboard pages.
 */
import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, test } from "vitest";

import {
  type ColumnDef,
  DataTable,
  sortableHeader,
} from "@/components/ui/data-table";

interface Row {
  name: string;
  status: string;
}

const columns: ColumnDef<Row, unknown>[] = [
  { accessorKey: "name", header: sortableHeader<Row>("Name") },
  { accessorKey: "status", header: "Status" },
];

const rows: Row[] = [
  { name: "Micron", status: "review" },
  { name: "Acme", status: "sent" },
];

describe("DataTable", () => {
  test("renders one table row per data row with cell values", () => {
    render(<DataTable columns={columns} data={rows} />);
    expect(screen.getByText("Micron")).toBeInTheDocument();
    expect(screen.getByText("Acme")).toBeInTheDocument();
    expect(screen.getByText("sent")).toBeInTheDocument();
    // header row + 2 data rows
    expect(screen.getAllByRole("row")).toHaveLength(3);
  });

  test("shows the custom empty message when there is no data", () => {
    render(
      <DataTable columns={columns} data={[]} emptyMessage="Queue is empty." />
    );
    expect(screen.getByText("Queue is empty.")).toBeInTheDocument();
  });

  test("clicking a sortable header sorts rows ascending", () => {
    render(<DataTable columns={columns} data={rows} />);
    const before = screen.getAllByRole("row").slice(1); // skip header
    expect(within(before[0]).getByText("Micron")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Name" }));

    const after = screen.getAllByRole("row").slice(1);
    expect(within(after[0]).getByText("Acme")).toBeInTheDocument();
    expect(within(after[1]).getByText("Micron")).toBeInTheDocument();
  });

  test("initialSorting orders rows on first render", () => {
    render(
      <DataTable
        columns={columns}
        data={rows}
        initialSorting={[{ id: "name", desc: false }]}
      />
    );
    const bodyRows = screen.getAllByRole("row").slice(1);
    expect(within(bodyRows[0]).getByText("Acme")).toBeInTheDocument();
  });

  test("pagination footer reflects the row count", () => {
    render(<DataTable columns={columns} data={rows} />);
    expect(screen.getByText(/1–2 of 2/)).toBeInTheDocument();
  });
});
